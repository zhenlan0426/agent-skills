import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import httpx

from kaggle_llm import Client, KaggleLLMError
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
        with self.assertRaises(KaggleLLMError):
            self.client(lambda r: completion()).prompt('hello', model='unavailable')
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
        c = self.client(lambda r: completion('```json\n{"n": 4}\n```'))
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
        with patch('kaggle_llm.auth.subprocess.run', side_effect=init) as run:
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
        for status, count in [(403, 1), (429, 1), (401, 2)]:
            with self.subTest(status=status):
                self.requests.clear()
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
                    with patch('kaggle_llm.auth.subprocess.run', side_effect=init) as run:
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


if __name__ == '__main__':
    unittest.main()
