import argparse
import json
import math
from pathlib import Path
import sys

from . import Client, KaggleLLMError, best
from .client import _prepare_schema
from .jsonutil import loads


MAX_CONSECUTIVE_TRANSIENT_ERRORS = 3


def emit(value):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


def _options(parser):
    parser.add_argument('--model', help='Exact provider/model ID (including unlisted models) or a known bare slug; '
                        'default: the model chosen by `kaggle-llm best`')
    parser.add_argument('--system')
    parser.add_argument('--schema', type=Path, help='JSON Schema file; validated locally')
    parser.add_argument('--schema-mode', choices=['prompt', 'native'], default='prompt')
    parser.add_argument('--max-tokens', type=int)
    parser.add_argument('--temperature', type=float, help='Omitted by default; support varies by model')
    parser.add_argument('--reasoning', choices=['none', 'minimal', 'low', 'medium', 'high'])


def main(argv=None):
    parser = argparse.ArgumentParser(description='Call Kaggle Model Proxy from scripts (no notebook upload).')
    parser.add_argument('--env-file', type=Path, help='Default: ~/.config/kaggle-llm/credentials.env')
    parser.add_argument('--timeout', type=float, default=120, help='HTTP timeout in seconds (default 120)')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('auth', help='Refresh protected credentials and local model catalog')
    sub.add_parser('models', help='Show the curated local model list (not exhaustive; JSON)')
    best_parser = sub.add_parser('best', help='Show the preferred callable model (cached 24h; probes on refresh)')
    best_parser.add_argument('--refresh', action='store_true', help='Ignore the cache and re-probe')
    prompt = sub.add_parser('prompt', help='Single call; JSON envelope on stdout by default')
    source = prompt.add_mutually_exclusive_group(required=True)
    source.add_argument('-p', '--prompt')
    source.add_argument('--file', type=Path, help='UTF-8 prompt file')
    source.add_argument('--stdin', action='store_true')
    prompt.add_argument('--text', action='store_true', help='Print only generated text')
    _options(prompt)
    batch = sub.add_parser('batch', help='Sequential JSONL input/output; one result or error per row')
    batch.add_argument('input', help='JSONL path or - for stdin; rows contain prompt and optional id')
    _options(batch)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('--timeout must be a positive finite number')
    if getattr(args, 'max_tokens', None) is not None and args.max_tokens <= 0:
        parser.error('--max-tokens must be a positive integer')
    if getattr(args, 'temperature', None) is not None and (
            not math.isfinite(args.temperature) or not 0 <= args.temperature <= 2):
        parser.error('--temperature must be between 0 and 2')
    schema = None
    if getattr(args, 'schema', None) is not None:
        try:
            schema = _prepare_schema(loads(args.schema.read_text(encoding='utf-8')))
        except (ValueError, OSError) as exc:
            parser.error(f'Invalid --schema: {exc}')
    try:
        with Client(env_file=args.env_file, timeout=args.timeout) as client:
            if args.command in ('auth', 'models'):
                emit(client.credentials.status(refresh=args.command == 'auth'))
                return 0
            if args.command == 'best':
                emit(best.select(client, refresh=args.refresh))
                return 0
            if args.model is None:
                # Resolve once so every row of a batch uses the same model.
                args.model = client.best_model()
            options = {key: getattr(args, key) for key in (
                'model', 'system', 'schema_mode', 'max_tokens', 'temperature', 'reasoning')}
            options['schema'] = schema
            if args.command == 'prompt':
                text = args.prompt if args.prompt is not None else (
                    args.file.read_text(encoding='utf-8') if args.file else sys.stdin.read())
                result = client.prompt(text, **options)
                if args.text:
                    print(result['text'])
                else:
                    emit(result)
                return 0
            failures = 0
            consecutive_transient_errors = 0
            stream = sys.stdin if args.input == '-' else open(args.input, encoding='utf-8')
            try:
                for number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    row_id = None
                    try:
                        row = loads(line)
                        if not isinstance(row, dict) or set(row) - {'id', 'prompt'}:
                            raise ValueError('Each row must be an object with prompt and optional id')
                        row_id = row.get('id')
                        if row_id is not None and type(row_id) not in (str, int):
                            raise ValueError('id must be a string or integer')
                        result = client.prompt(row.get('prompt'), **options)
                        consecutive_transient_errors = 0
                        emit({'line': number, 'id': row_id, 'ok': True, 'result': result})
                    except (KaggleLLMError, ValueError) as exc:
                        failures += 1
                        emit({'line': number, 'id': row_id, 'ok': False, 'error': str(exc)})
                        transient = isinstance(exc, KaggleLLMError) and exc.batch_transient
                        consecutive_transient_errors = consecutive_transient_errors + 1 if transient else 0
                        if ((isinstance(exc, KaggleLLMError) and exc.batch_fatal)
                                or consecutive_transient_errors >= MAX_CONSECUTIVE_TRANSIENT_ERRORS):
                            print('Batch stopped; remaining rows were not sent.', file=sys.stderr)
                            break
            finally:
                if stream is not sys.stdin:
                    stream.close()
            return 1 if failures else 0
    except (KaggleLLMError, ValueError, OSError) as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
