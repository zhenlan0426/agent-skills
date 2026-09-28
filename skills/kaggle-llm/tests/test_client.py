import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx

from kaggle_llm import Client, KaggleLLMError
from kaggle_llm import best
from kaggle_llm.auth import Credentials
from kaggle_llm.cli import main
from kaggle_llm.client import _endpoint, resolve_model
import jsonschema

from contextlib import redirect_stdout, redirect_stderr
from io import StringIO


ENV = ('MODEL_PROXY_URL=https://proxy.example/models\n'
       'MODEL_PROXY_API_KEY=secret-test-token\n'
       'MODEL_PROXY_EXPIRY_TIME=2099-01-01T00:00:00Z\n'
       'LLM_DEFAULT=google/test\nLLMS_AVAILABLE=google/test,anthropic/other@20250101\n')


def completion(text='hello', **changes):
    result = {'id': 'test', 'model': 'google/test', 'usage': {'total_tokens': 3},
              'choices': [{'finish_reason': 'stop', 'message': {'content': text}}]}
    result.update(changes)
    return httpx.Response(200, json=result)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'credentials.env'
        self.path.write_text(ENV)
        self.requests = []
        self.cache_model('google/test')

    def cache_model(self, model, age=timedelta(0)):
        best.cache_path(Credentials(self.path)).write_text(json.dumps({
            'model': model, 'selected_at': (datetime.now(timezone.utc) - age).isoformat()}))

    def client(self, responder):
        def handler(request):
            self.requests.append(request)
            return responder(request)
        client = Client(env_file=self.path, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def test_endpoint_alias_and_stateless_messages(self):
        c = self.client(lambda r: completion())
        for text in ['first', 'second']:
            self.assertEqual(c.prompt(text, model='test')['text'], 'hello')
        body = json.loads(self.requests[1].content)
        self.assertEqual(body['messages'], [{'role': 'user', 'content': 'second'}])
        self.assertEqual(body['model'], 'google/test')
        self.assertNotIn('temperature', body)
        self.assertEqual(str(self.requests[0].url), 'https://proxy.example/models/openapi/chat/completions')
        self.assertEqual(self.requests[0].headers['Authorization'], 'Bearer secret-test-token')

    def test_unknown_model_rejected_before_inference(self):
        self.path.write_text(ENV.replace('2099-', '2000-'))
        c = self.client(lambda r: completion())
        with patch.object(c.credentials, 'ensure') as ensure, self.assertRaises(KaggleLLMError) as caught:
            c.prompt('hello', model='unavailable')
        ensure.assert_not_called()
        self.assertTrue(caught.exception.batch_fatal)
        self.assertFalse(self.requests)

    def test_unlisted_exact_model_is_sent_unchanged(self):
        c = self.client(lambda r: completion())
        c.prompt('hello', model='google/gemini-3.7-flash')
        self.assertEqual(json.loads(self.requests[0].content)['model'], 'google/gemini-3.7-flash')

    def test_qualified_model_never_remapped_to_different_provider(self):
        c = self.client(lambda r: completion())
        c.prompt('hello', model='another-provider/test')
        self.assertEqual(json.loads(self.requests[0].content)['model'], 'another-provider/test')

    def test_unlisted_model_server_rejection_surfaces(self):
        c = self.client(lambda r: httpx.Response(404))
        with self.assertRaisesRegex(KaggleLLMError, '404'):
            c.prompt('hello', model='provider/unavailable')
        self.assertEqual(len(self.requests), 1)

    def test_json_schema_validation(self):
        schema = {'type': 'object', 'properties': {'n': {'type': 'integer'}}, 'required': ['n']}
        for tag in ('json', 'JSON', 'Json', ''):
            with self.subTest(tag=tag):
                c = self.client(lambda r: completion(f'```{tag}\n{{"n": 4}}\n```'))
                self.assertEqual(c.prompt('count', schema=schema)['structured_output'], {'n': 4})
        c = self.client(lambda r: completion('{"n": "wrong type"}'))
        with self.assertRaisesRegex(KaggleLLMError, 'schema'):
            c.prompt('count', schema=schema)

    def test_native_schema_and_external_refs(self):
        c = self.client(lambda r: completion('4'))
        self.assertEqual(c.prompt('count', schema={'type': 'integer'}, schema_mode='native')['structured_output'], 4)
        self.assertEqual(json.loads(self.requests[0].content)['response_format']['type'], 'json_schema')
        with self.assertRaisesRegex(ValueError, 'local'):
            c.prompt('count', schema={'$ref': 'https://example.com/schema'})
        self.assertEqual(len(self.requests), 1)

    def test_schema_defaults_to_native_and_prompt_mode_opts_out(self):
        c = self.client(lambda r: completion('4'))
        c.prompt('count', schema={'type': 'integer'})
        c.prompt('count', schema={'type': 'integer'}, schema_mode='prompt')
        c.prompt('count')
        sent = [json.loads(r.content) for r in self.requests]
        self.assertEqual(sent[0]['response_format']['json_schema']['schema'], {'type': 'integer'})
        self.assertNotIn('response_format', sent[1])
        self.assertNotIn('response_format', sent[2])

    def test_nonfinite_json_is_rejected(self):
        c = self.client(lambda r: completion('NaN'))
        with self.assertRaisesRegex(KaggleLLMError, 'invalid JSON'):
            c.prompt('number', schema={'type': 'number'})

    def test_invalid_generation_options_never_call_api(self):
        c = self.client(lambda r: completion())
        for options in [{'max_tokens': 0}, {'temperature': float('nan')}, {'reasoning': 'bogus'}]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                c.prompt('hello', **options)
        self.assertFalse(self.requests)

    def test_truncation_refusal_and_missing_content(self):
        for choice in [
            {'finish_reason': 'length', 'message': {'content': 'partial'}},
            {'finish_reason': 'stop', 'message': {'content': '', 'refusal': 'no'}},
            {'finish_reason': 'stop', 'message': None},
        ]:
            with self.subTest(choice=choice), self.assertRaises(KaggleLLMError):
                self.client(lambda r: completion(choices=[choice])).prompt('hello')

    def test_auth_refresh_once(self):
        c = self.client(lambda r: httpx.Response(401) if len(self.requests) == 1 else completion())
        values = c.credentials.read()
        with patch.object(c.credentials, 'ensure', return_value=values) as ensure:
            self.assertEqual(c.prompt('hello')['text'], 'hello')
            self.assertEqual(ensure.call_count, 2)
            ensure.assert_called_with(force=True, rejected_token='secret-test-token')
        self.assertEqual(len(self.requests), 2)

    def test_auth_refresh_stops_after_second_401(self):
        c = self.client(lambda r: httpx.Response(401))
        with patch.object(c.credentials, 'ensure', return_value=c.credentials.read()):
            with self.assertRaisesRegex(KaggleLLMError, '401'):
                c.prompt('hello')
        self.assertEqual(len(self.requests), 2)

    def test_http_errors_do_not_leak_or_retry(self):
        for status in [400, 403, 429, 500]:
            self.requests.clear()
            c = self.client(lambda r: httpx.Response(status, text='secret-test-token PRIVATE PROMPT'))
            with self.assertRaises(KaggleLLMError) as caught:
                c.prompt('PRIVATE PROMPT')
            self.assertNotIn('secret', str(caught.exception))
            self.assertNotIn('PRIVATE', str(caught.exception))
            self.assertEqual(len(self.requests), 1)

    def test_timeout_no_retry(self):
        def timeout(r):
            raise httpx.ReadTimeout('private details')
        with self.assertRaisesRegex(KaggleLLMError, 'timed out'):
            self.client(timeout).prompt('hi')
        self.assertEqual(len(self.requests), 1)

    def test_refresh_atomic_private_and_skips_valid(self):
        self.path.write_text('')
        def init(command, **kwargs):
            self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
            Path(command[command.index('--env-file') + 1]).write_text(ENV)
            return subprocess.CompletedProcess(command, 0)
        with patch('kaggle_llm.auth.shutil.which', return_value='/mock/bin/kaggle'), \
                patch('kaggle_llm.auth.subprocess.run', side_effect=init) as run:
            credentials = Credentials(self.path)
            credentials.ensure()
            credentials.ensure()
            self.assertEqual(run.call_count, 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.path.parent.glob('refresh-*')))

    def test_other_worker_already_refreshed(self):
        with patch('kaggle_llm.auth.subprocess.run') as run:
            Credentials(self.path).ensure(force=True, rejected_token='older-token')
            run.assert_not_called()

    def test_expiry_and_environment_isolation(self):
        self.assertFalse(Credentials.valid({'MODEL_PROXY_URL': 'a', 'MODEL_PROXY_API_KEY': 'b',
                                            'MODEL_PROXY_EXPIRY_TIME': '2000-01-01T00:00:00Z'}))
        with patch.dict(os.environ, {'MODEL_PROXY_API_KEY': 'wrong-token'}):
            self.assertEqual(Credentials(self.path).read()['MODEL_PROXY_API_KEY'], 'secret-test-token')

    def test_batch_continues_after_bad_input(self):
        source = Path(self.tmp.name) / 'input.jsonl'
        source.write_text('{"id":"a","prompt":"hello"}\ninvalid\n{"id":"b","prompt":"world"}\n')
        out = StringIO()
        with patch('kaggle_llm.cli.Client') as mock, redirect_stdout(out):
            mock.return_value.__enter__.return_value.prompt.return_value = {'text': 'ok'}
            code = main(['batch', str(source)])
        rows = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(code, 1)
        self.assertEqual([r['ok'] for r in rows], [True, False, True])
        self.assertEqual([r['line'] for r in rows], [1, 2, 3])

    def run_batch(self, client, extra=()):
        source = StringIO(''.join(json.dumps({'id': n, 'prompt': 'hello'}) + '\n' for n in range(5)))
        out, err = StringIO(), StringIO()
        with patch('kaggle_llm.cli.Client', return_value=client), patch('sys.stdin', source), \
                redirect_stdout(out), redirect_stderr(err):
            code = main(['batch', '-', *extra])
        return code, [json.loads(line) for line in out.getvalue().splitlines()], err.getvalue()

    def test_batch_stops_on_access_quota_and_persistent_401(self):
        for status, count in [(403, 1), (404, 1), (429, 1), (401, 2)]:
            with self.subTest(status=status):
                self.requests.clear()
                self.cache_model('google/test')  # A 403/404 drops the cached pick.
                c = self.client(lambda r: httpx.Response(status))
                with patch.object(c.credentials, 'ensure', return_value=c.credentials.read()) as ensure:
                    code, rows, err = self.run_batch(c)
                self.assertEqual(code, 1)
                self.assertEqual(len(rows), 1)
                self.assertFalse(rows[0]['ok'])
                self.assertIn(str(status), rows[0]['error'])
                self.assertEqual(len(self.requests), count)
                self.assertEqual(ensure.call_count, count)
                self.assertIn('remaining rows were not sent', err)

    def test_batch_stops_on_refresh_failure(self):
        for on_401 in (False, True):
            for failure in ('login', 'timeout', 'malformed'):
                with self.subTest(on_401=on_401, failure=failure):
                    self.requests.clear()
                    self.path.write_text(ENV if on_401 else '')
                    c = self.client(lambda r: httpx.Response(401))
                    def init(command, **kwargs):
                        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
                        if failure == 'timeout':
                            raise subprocess.TimeoutExpired(command, 90)
                        if failure == 'malformed':
                            Path(command[command.index('--env-file') + 1]).write_text('')
                        return subprocess.CompletedProcess(command, int(failure == 'login'))
                    with patch('kaggle_llm.auth.shutil.which', return_value='/mock/bin/kaggle'), \
                            patch('kaggle_llm.auth.subprocess.run', side_effect=init) as run:
                        code, rows, _ = self.run_batch(c)
                    self.assertEqual(code, 1)
                    self.assertEqual(len(rows), 1)
                    self.assertFalse(rows[0]['ok'])
                    self.assertEqual(run.call_count, 1)
                    self.assertEqual(len(self.requests), int(on_401))

    def test_batch_continues_after_nonfatal_inference_error(self):
        c = self.client(lambda r: httpx.Response(500) if len(self.requests) == 1 else completion())
        code, rows, _ = self.run_batch(c)
        self.assertEqual(code, 1)
        self.assertEqual([row['ok'] for row in rows], [False, True, True, True, True])
        self.assertEqual(len(self.requests), 5)

    def test_batch_stops_on_invalid_local_configuration(self):
        for env, model in (
            (ENV, 'unknown'), (ENV, 'bad/model/id'),
            (ENV.replace('https://proxy.example/models', 'http://proxy.example'), None),
            (ENV.replace('https://proxy.example/models', 'https://proxy.example:bad'), None),
            (ENV.replace('https://proxy.example/models', 'https://[broken'), None),
        ):
            with self.subTest(env=env, model=model):
                self.requests.clear()
                self.path.write_text(env)
                c = self.client(lambda r: completion())
                with patch.object(c.credentials, 'ensure') as ensure:
                    code, rows, err = self.run_batch(c, ['--model', model] if model else [])
                self.assertEqual(code, 1)
                self.assertEqual(len(rows), 1)
                self.assertFalse(rows[0]['ok'])
                self.assertFalse(self.requests)
                ensure.assert_not_called()
                self.assertIn('remaining rows were not sent', err)

    def test_first_use_bootstraps_before_resolving_alias(self):
        self.path.unlink()
        c = self.client(lambda r: completion())
        def init(command, **kwargs):
            Path(command[command.index('--env-file') + 1]).write_text(ENV)
            return subprocess.CompletedProcess(command, 0)
        with patch('kaggle_llm.auth.shutil.which', return_value='/mock/bin/kaggle'), \
                patch('kaggle_llm.auth.subprocess.run', side_effect=init) as run:
            self.assertEqual(c.prompt('hello', model='test')['text'], 'hello')
        self.assertEqual(run.call_count, 1)
        self.assertEqual(len(self.requests), 1)

    def test_batch_stops_after_three_consecutive_transient_errors(self):
        for outcomes in (['timeout'] * 5, [503] * 5, [500, 'timeout', 502, 200, 200]):
            with self.subTest(outcomes=outcomes):
                self.requests.clear()
                def respond(request):
                    status = outcomes[len(self.requests) - 1]
                    if status == 'timeout':
                        raise httpx.ReadTimeout('private details')
                    return httpx.Response(status)
                code, rows, err = self.run_batch(self.client(respond))
                self.assertEqual(code, 1)
                self.assertEqual(len(self.requests), 3)
                self.assertEqual([row['ok'] for row in rows], [False] * 3)
                self.assertIn('remaining rows were not sent', err)

    def test_batch_transient_counter_resets(self):
        for middle in (200, 400):
            with self.subTest(middle=middle):
                self.requests.clear()
                statuses = [503, 503, middle, 503, 503]
                def respond(request):
                    status = statuses[len(self.requests) - 1]
                    return completion() if status == 200 else httpx.Response(status)
                code, rows, err = self.run_batch(self.client(respond))
                self.assertEqual(code, 1)
                self.assertEqual(len(rows), 5)
                self.assertEqual(len(self.requests), 5)
                self.assertEqual(err, '')

    def test_batch_rejects_boolean_ids_before_inference(self):
        source = StringIO(''.join(json.dumps({'id': value, 'prompt': 'hello'}) + '\n'
                                 for value in (True, False, 0, 'a')))
        c = self.client(lambda r: completion())
        with patch('kaggle_llm.cli.Client', return_value=c), patch('sys.stdin', source), \
                redirect_stdout(StringIO()) as out:
            self.assertEqual(main(['batch', '-']), 1)
        rows = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([row['ok'] for row in rows], [False, False, True, True])
        self.assertEqual(len(self.requests), 2)

    def test_leading_think_stripped_before_validation(self):
        for options in ({}, {'reasoning': 'none'}, {'reasoning': 'low'}):
            with self.subTest(options=options):
                c = self.client(lambda r: completion(' \n<think>reason\nover lines</think>\n{"n": 4}'))
                result = c.prompt('count', schema={'type': 'object'}, **options)
                self.assertEqual(result['text'], '{"n": 4}')
                self.assertEqual(result['structured_output'], {'n': 4})
                c = self.client(lambda r: completion(' <think>reason</think> \n'))
                with self.assertRaisesRegex(KaggleLLMError, 'no text'):
                    c.prompt('hello', **options)

    def test_nonleading_think_preserved(self):
        text = 'An example: <think>literal text</think>'
        c = self.client(lambda r: completion(text))
        self.assertEqual(c.prompt('hello', reasoning='low')['text'], text)

    def test_cli_prompt_sources_and_output_modes(self):
        prompt_file = Path(self.tmp.name) / 'prompt.txt'
        prompt_file.write_text('café', encoding='utf-8')
        for source in (['-p', 'café'], ['--file', str(prompt_file)], ['--stdin']):
            for plain in (False, True):
                with self.subTest(source=source, plain=plain):
                    c = self.client(lambda r: completion('bonjour'))
                    out = StringIO()
                    with patch('kaggle_llm.cli.Client', return_value=c), \
                            patch('sys.stdin', StringIO('café')), redirect_stdout(out):
                        code = main(['prompt', *source, *(['--text'] if plain else [])])
                    self.assertEqual(code, 0)
                    self.assertEqual(json.loads(self.requests[-1].content)['messages'][-1]['content'], 'café')
                    if plain:
                        self.assertEqual(out.getvalue(), 'bonjour\n')
                    else:
                        self.assertEqual(json.loads(out.getvalue())['text'], 'bonjour')

    def test_schema_errors_are_usage_errors_before_client_creation(self):
        path = Path(self.tmp.name) / 'schema.json'
        for schema in ('invalid', 'null', '{"type":"not-a-type"}', '{"$ref":"https://example.com/schema"}'):
            path.write_text(schema, encoding='utf-8')
            with self.subTest(schema=schema), patch('kaggle_llm.cli.Client') as client, \
                    redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()) as err:
                with self.assertRaises(SystemExit) as caught:
                    main(['batch', '-', '--schema', str(path)])
                self.assertEqual(caught.exception.code, 2)
                client.assert_not_called()
                self.assertEqual(out.getvalue(), '')
                self.assertIn('Invalid --schema', err.getvalue())

    def test_numeric_options_are_usage_errors_before_input_or_client(self):
        for command in (['batch', '-'], ['prompt', '--stdin']):
            for option, values in (
                ('--max-tokens', ('0', '-1')),
                ('--temperature', ('-1', '2.1', 'nan', 'inf', '-inf')),
                ('--timeout', ('0', '-1', 'nan', 'inf', '-inf')),
            ):
                for value in values:
                    argv = ([f'{option}={value}', *command] if option == '--timeout'
                            else [*command, f'{option}={value}'])
                    with self.subTest(argv=argv), patch('kaggle_llm.cli.Client') as client, \
                            patch('sys.stdin') as source, redirect_stdout(StringIO()) as out, \
                            redirect_stderr(StringIO()) as err:
                        with self.assertRaises(SystemExit) as caught:
                            main(argv)
                        self.assertEqual(caught.exception.code, 2)
                        client.assert_not_called()
                        source.read.assert_not_called()
                        source.__iter__.assert_not_called()
                        self.assertEqual(out.getvalue(), '')
                        self.assertIn(option, err.getvalue())

    def test_batch_schema_checked_once_and_reused(self):
        path = Path(self.tmp.name) / 'schema.json'
        path.write_text('{"type":"integer", "description":"café"}', encoding='utf-8')
        c = self.client(lambda r: completion('4'))
        validator = jsonschema.Draft202012Validator
        with patch.object(validator, 'check_schema', wraps=validator.check_schema) as check:
            code, rows, _ = self.run_batch(c, ['--schema', str(path), '--schema-mode', 'native'])
        self.assertEqual(code, 0)
        self.assertEqual(check.call_count, 1)
        self.assertEqual([row['result']['structured_output'] for row in rows], [4] * 5)
        self.assertEqual(json.loads(self.requests[-1].content)['response_format']['json_schema']['schema'],
                         {'type': 'integer', 'description': 'café'})

    def test_endpoint_cleanup_and_rejection(self):
        for suffix in ('', '/', '/openapi', '/openapi/', '/genai', '/genai/'):
            self.assertEqual(_endpoint('https://proxy.example/models' + suffix),
                             'https://proxy.example/models/openapi/chat/completions')
        for url in ('http://proxy.example', 'file:///tmp/proxy', 'proxy.example',
                    'https://', 'https://user:password@proxy.example',
                    'https://proxy.example?q=value', 'https://proxy.example#fragment'):
            with self.subTest(url=url), self.assertRaises(KaggleLLMError):
                _endpoint(url)

    def test_bare_aliases_versions_and_ambiguity(self):
        values = {'LLMS_AVAILABLE': 'anthropic/claude-sonnet-5@default,google/test'}
        for alias in ('claude-sonnet-5', 'claude-sonnet-5@default', 'claude-sonnet-5-default'):
            self.assertEqual(resolve_model(alias, values), 'anthropic/claude-sonnet-5@default')
        for available in ('', 'google/test,other/test', 'provider/test@v1,provider/test@v2'):
            with self.subTest(available=available), self.assertRaises(KaggleLLMError):
                resolve_model('test', {'LLMS_AVAILABLE': available})

    def test_nonfinite_proxy_envelope_rejected_at_parse(self):
        for value in ('NaN', 'Infinity', '-Infinity', '1e999'):
            with self.subTest(value=value):
                c = self.client(lambda r: httpx.Response(200, text=(
                    '{"usage":{"cost":' + value + '},"choices":[{"message":{"content":"ok"}}]}'
                )))
                with self.assertRaisesRegex(KaggleLLMError, 'Proxy returned invalid JSON'):
                    c.prompt('hello')

    def test_rank_prefers_claude_openai_then_gemini_newest_first(self):
        slugs = ['google/gemini-3.8-flash', 'google/gemini-3.1-pro-preview', 'google/gemini-3.5-flash-lite',
                 'anthropic/claude-sonnet-5@default', 'anthropic/claude-opus-4-8@default',
                 'anthropic/claude-opus-5@default', 'anthropic/claude-haiku-4-5@20251001',
                 'openai/gpt-5.4-nano-2026-03-17', 'openai/gpt-5.5-2026-04-23', 'openai/gpt-6-astra',
                 'openai/gpt-oss-120b', 'qwen/qwen3-next-80b-a3b-instruct', 'google/gemma-4-31b']
        self.assertEqual(best.rank(slugs), [
            'openai/gpt-6-astra', 'openai/gpt-5.5-2026-04-23', 'anthropic/claude-opus-5@default',
            'anthropic/claude-sonnet-5@default', 'openai/gpt-5.4-nano-2026-03-17',
            'anthropic/claude-opus-4-8@default', 'anthropic/claude-haiku-4-5@20251001',
            'google/gemini-3.1-pro-preview', 'google/gemini-3.8-flash', 'google/gemini-3.5-flash-lite',
        ])

    def test_default_model_is_cached_pick(self):
        self.cache_model('anthropic/claude-sonnet-5@default')
        c = self.client(lambda r: completion())
        with patch('kaggle_llm.best.fetch_catalog') as catalog:
            c.prompt('hello')
        catalog.assert_not_called()
        self.assertEqual(json.loads(self.requests[0].content)['model'], 'anthropic/claude-sonnet-5@default')

    def test_selection_probes_in_rank_order_and_caches(self):
        self.cache_model('google/test', age=timedelta(days=2))
        available = {'anthropic/claude-sonnet-5@default', 'google/gemini-3.7-flash'}
        c = self.client(lambda r: completion() if json.loads(r.content)['model'] in available
                        else httpx.Response(404))
        catalog = ['anthropic/claude-opus-5@default', 'anthropic/claude-sonnet-5@default',
                   'google/gemini-3.7-flash', 'openai/gpt-oss-120b']
        with patch('kaggle_llm.best.fetch_catalog', return_value=catalog):
            self.assertEqual(c.best_model(), 'anthropic/claude-sonnet-5@default')
            self.assertEqual(c.best_model(), 'anthropic/claude-sonnet-5@default')
        probed = [json.loads(r.content)['model'] for r in self.requests]
        self.assertEqual(probed, ['anthropic/claude-opus-5@default', 'anthropic/claude-sonnet-5@default'])
        cached = best.read_cache(c.credentials)
        self.assertEqual(cached['unavailable'], ['anthropic/claude-opus-5@default'])
        # LLMS_AVAILABLE's anthropic/other has no version, so it is not a candidate.
        self.assertEqual(cached['untried'], ['google/gemini-3.7-flash'])

    def test_selection_stops_on_quota_and_rejected_pick_is_forgotten(self):
        self.cache_model('google/test', age=timedelta(days=2))
        c = self.client(lambda r: httpx.Response(429))
        with patch('kaggle_llm.best.fetch_catalog', return_value=['anthropic/claude-opus-5@default',
                                                                  'anthropic/claude-sonnet-5@default']), \
                self.assertRaises(KaggleLLMError) as caught:
            c.best_model()
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(len(self.requests), 1)
        self.cache_model('google/gemini-3.7-flash')
        c = self.client(lambda r: httpx.Response(404))
        with self.assertRaises(KaggleLLMError):
            c.prompt('hello')
        self.assertIsNone(best.read_cache(c.credentials))

    def test_selection_skips_unusable_completions_before_caching(self):
        top, next_model = 'anthropic/claude-opus-5@default', 'anthropic/claude-sonnet-5@default'
        invalid = [completion(''), completion('  '), completion(None),
                   completion('<think>reasoning only</think>'),
                   completion(choices=[{'message': {'content': 'OK'}, 'finish_reason': 'length'}]),
                   completion(choices=[{'message': {'content': 'OK', 'refusal': 'refused'}}]),
                   completion(choices=[{'message': {'content': 'OK'}, 'finish_reason': 'content_filter'}]),
                   completion(choices=[None])]
        for response in invalid:
            with self.subTest(response=response.json()):
                self.requests.clear()
                c = self.client(lambda r: response if json.loads(r.content)['model'] == top else completion())
                with patch('kaggle_llm.best.fetch_catalog', return_value=[top, next_model]):
                    self.assertEqual(c.best_model(refresh=True), next_model)
                cached = best.read_cache(c.credentials)
                self.assertEqual(cached['unavailable'], [top])
                self.assertEqual(cached['untried'], [])
                self.assertEqual(c.prompt('hello')['text'], 'hello')
                self.assertEqual([json.loads(r.content)['model'] for r in self.requests],
                                 [top, next_model, next_model])

    def test_empty_probe_never_creates_cache(self):
        c = self.client(lambda r: completion(''))
        best.cache_path(c.credentials).unlink()
        with patch('kaggle_llm.best.fetch_catalog', return_value=['anthropic/claude-opus-5@default']), \
                self.assertRaisesRegex(KaggleLLMError, 'No Claude'):
            c.best_model()
        self.assertFalse(best.cache_path(c.credentials).exists())

    def test_cli_batch_pins_one_model(self):
        c = self.client(lambda r: completion())
        with patch.object(c, 'best_model', return_value='google/test') as pick:
            code, rows, _ = self.run_batch(c)
        self.assertEqual(code, 0)
        pick.assert_called_once()
        self.assertEqual({json.loads(r.content)['model'] for r in self.requests}, {'google/test'})


if __name__ == '__main__':
    unittest.main()
