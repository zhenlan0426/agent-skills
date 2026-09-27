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

from contextlib import redirect_stdout
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


if __name__ == '__main__':
    unittest.main()
