import argparse
import json
from pathlib import Path
import sys

from . import Client, KaggleLLMError
from .jsonutil import loads


def emit(value):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


def _options(parser):
    parser.add_argument('--model', help='Exact provider/model ID (including unlisted models), or a known bare slug')
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
    sub.add_parser('models', help='Show the curated local model list and default (not exhaustive; JSON)')
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
    try:
        with Client(env_file=args.env_file, timeout=args.timeout) as client:
            if args.command in ('auth', 'models'):
                emit(client.credentials.status(refresh=args.command == 'auth'))
                return 0
            options = {key: getattr(args, key) for key in (
                'model', 'system', 'schema_mode', 'max_tokens', 'temperature', 'reasoning')}
            options['schema'] = loads(args.schema.read_text()) if args.schema else None
            if args.command == 'prompt':
                text = args.prompt if args.prompt is not None else (
                    args.file.read_text(encoding='utf-8') if args.file else sys.stdin.read())
                result = client.prompt(text, **options)
                print(result['text']) if args.text else emit(result)
                return 0
            failures = 0
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
                        if row_id is not None and not isinstance(row_id, (str, int)):
                            raise ValueError('id must be a string or integer')
                        result = client.prompt(row.get('prompt'), **options)
                        emit({'line': number, 'id': row_id, 'ok': True, 'result': result})
                    except (KaggleLLMError, ValueError) as exc:
                        failures += 1
                        emit({'line': number, 'id': row_id, 'ok': False, 'error': str(exc)})
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
