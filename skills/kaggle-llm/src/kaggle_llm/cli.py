import argparse
import json
import math
from pathlib import Path
import sys

from . import Client, KaggleLLMError, best, remote
from .client import _prepare_schema
from .jsonutil import loads


MAX_CONSECUTIVE_TRANSIENT_ERRORS = 3
REMOTE_ONLY = (('detach', '--detach'), ('max_cost', '--max-cost'), ('concurrency', '--concurrency'),
               ('dedup', '--dedup'), ('wait_timeout', '--wait-timeout'), ('execute_in', '--execute-in'))


def emit(value):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


def _options(parser):
    parser.add_argument('--model', help='Exact provider/model ID (including unlisted models) or a known bare slug; '
                        'default: the model chosen by `kaggle-llm best`')
    parser.add_argument('--system')
    parser.add_argument('--schema', type=Path, help='JSON Schema file; validated locally')
    parser.add_argument('--schema-mode', choices=['prompt', 'native'], default='native')
    parser.add_argument('--max-tokens', type=int, help=f'Omitted by default locally; --remote default '
                        f'{remote.DEFAULT_MAX_TOKENS}')
    parser.add_argument('--temperature', type=float, help='Omitted by default; support varies by model')
    parser.add_argument('--reasoning', choices=['none', 'minimal', 'low', 'medium', 'high'])


def _remote_options(parser, *, batch):
    parser.add_argument('--max-cost', type=float, metavar='USD',
                        help=f'Stop dispatching rows once the job has spent this much (remote only; batch default '
                             f'{remote.DEFAULT_MAX_COST_USD:g})')
    parser.add_argument('--concurrency', type=int, help=f'Parallel calls inside the Kaggle job, 1-16 (default {remote.DEFAULT_CONCURRENCY})')
    parser.add_argument('--detach', action='store_true', help='Print {"job_id": ...} after the push and exit')
    parser.add_argument('--wait-timeout', type=float, metavar='SECONDS',
                        help=f'Stop waiting after this long; the job continues (default {remote.DEFAULT_WAIT_TIMEOUT})')
    if batch:
        parser.add_argument('--remote', action='store_true',
                            help='Run inside a private Kaggle benchmark task (full model catalog; prompts and '
                                 'responses are stored permanently in your Kaggle account)')
        parser.add_argument('--dedup', action='store_true',
                            help='Drop near-duplicate prompts before sending; they get no output row (remote only)')
        parser.add_argument('--execute-in', choices=['creation', 'run'],
                            help='Kaggle run that executes the job (remote only; default creation)')


def _check_remote_flags(parser, args):
    if args.max_cost is not None and not (math.isfinite(args.max_cost) and args.max_cost > 0):
        parser.error('--max-cost must be a positive number')
    if args.concurrency is not None and not 1 <= args.concurrency <= remote.MAX_CONCURRENCY:
        parser.error(f'--concurrency must be from 1 to {remote.MAX_CONCURRENCY}')
    if args.wait_timeout is not None and not (math.isfinite(args.wait_timeout) and args.wait_timeout > 0):
        parser.error('--wait-timeout must be a positive number')


def _read_remote_rows(parser, path):
    """Validate every row up front: a remote job is all-or-nothing to submit."""
    stream = sys.stdin if path == '-' else open(path, encoding='utf-8')
    rows = []
    try:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = loads(line)
            except ValueError:
                parser.error(f'line {number}: invalid JSON')
            if not isinstance(row, dict) or set(row) - {'id', 'prompt'}:
                parser.error(f'line {number}: each row must be an object with prompt and optional id')
            if row.get('id') is not None and type(row['id']) not in (str, int):
                parser.error(f'line {number}: id must be a string or integer')
            if not isinstance(row.get('prompt'), str) or not row['prompt']:
                parser.error(f'line {number}: prompt must be a nonempty string')
            rows.append({'line': number, 'id': row.get('id'), 'prompt': row['prompt']})
    finally:
        if stream is not sys.stdin:
            stream.close()
    if not rows:
        parser.error('input has no rows')
    return rows


def _finish_remote(store, backend, job_id, args):
    """Wait for the job, emit its rows (merged with any resumed parents), and return the exit code."""
    if args.detach:
        emit({'job_id': job_id})
        return 0
    timeout = args.wait_timeout or remote.DEFAULT_WAIT_TIMEOUT
    try:
        remote.wait(store, backend, job_id, timeout=timeout)
    except KeyboardInterrupt:
        print(f'job {job_id} continues on Kaggle; collect with: kaggle-llm remote collect {job_id}', file=sys.stderr)
        return 130
    rows = remote.collect(store, job_id)
    for row in rows:
        emit(row)
    info = remote.summary(store, job_id)
    if store.load_state(job_id).get('parent'):
        info['merged_rows_ok'] = sum(row['ok'] for row in rows)
        info['merged_rows_failed'] = len(rows) - info['merged_rows_ok']
    print(json.dumps(info), file=sys.stderr)
    return 0 if all(row['ok'] for row in rows) else 1


def _remote_batch(parser, args, schema):
    _check_remote_flags(parser, args)
    rows = _read_remote_rows(parser, args.input)
    exact = isinstance(args.model, str) and '/' in args.model
    spec, dropped = remote.prepare_job(
        rows, catalog=None if exact else best.fetch_catalog(), system=args.system, schema=schema,
        schema_mode=args.schema_mode, model=args.model, max_tokens=args.max_tokens or remote.DEFAULT_MAX_TOKENS,
        temperature=args.temperature, reasoning=args.reasoning, concurrency=args.concurrency or remote.DEFAULT_CONCURRENCY,
        max_cost_usd=remote.DEFAULT_MAX_COST_USD if args.max_cost is None else args.max_cost,
        dedup=args.dedup, execute_in=args.execute_in or 'creation')
    for row in dropped:
        print(json.dumps({'dropped_duplicate': row}), file=sys.stderr)
    store = remote.JobStore()
    job_id = store.create(spec, dropped)
    backend = remote.default_backend()
    remote.submit(store, backend, job_id)
    return _finish_remote(store, backend, job_id, args)


def _remote_command(parser, args):
    store = remote.JobStore()
    if args.remote_command == 'list':
        for job in store.list():
            emit(job)
        return 0
    if args.remote_command == 'status':
        emit(remote.summary(store, args.job))
        return 0
    _check_remote_flags(parser, args)
    backend = remote.default_backend()
    if args.remote_command == 'resume':
        overrides = {'max_cost_usd': args.max_cost} if args.max_cost is not None else {}
        if args.concurrency is not None:
            overrides['concurrency'] = args.concurrency
        job_id = remote.resume(store, args.job, **overrides)
        print(json.dumps({'resumed_from': args.job, 'job_id': job_id}), file=sys.stderr)
        remote.submit(store, backend, job_id)
        return _finish_remote(store, backend, job_id, args)
    return _finish_remote(store, backend, args.job, args)  # collect


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
    batch = sub.add_parser('batch', help='JSONL input/output; one result or error per row '
                           '(sequential locally, or --remote in a private Kaggle task)')
    batch.add_argument('input', help='JSONL path or - for stdin; rows contain prompt and optional id')
    _options(batch)
    _remote_options(batch, batch=True)
    remote_parser = sub.add_parser('remote', help='Inspect, collect, or resume remote batch jobs')
    remote_sub = remote_parser.add_subparsers(dest='remote_command', required=True)
    remote_sub.add_parser('list', help='Local job records (JSON lines)')
    remote_sub.add_parser('status', help='Job summary JSON (local state; no Kaggle calls)').add_argument('job')
    collect_parser = remote_sub.add_parser('collect', help='Wait if needed, then print result rows')
    collect_parser.add_argument('job')
    collect_parser.add_argument('--wait-timeout', type=float, metavar='SECONDS')
    resume_parser = remote_sub.add_parser('resume', help='Submit failed or unrun rows as a child job')
    resume_parser.add_argument('job')
    _remote_options(resume_parser, batch=False)
    args = parser.parse_args(argv)
    if args.command == 'batch' and not args.remote:
        given = [flag for name, flag in REMOTE_ONLY if getattr(args, name) not in (None, False)]
        if given:
            parser.error(f'{", ".join(given)}: only valid with --remote')
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
        if args.command == 'remote':
            for name in ('max_cost', 'concurrency', 'detach'):
                setattr(args, name, getattr(args, name, None))
            return _remote_command(parser, args)
        if args.command == 'batch' and args.remote:
            return _remote_batch(parser, args, schema)
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
