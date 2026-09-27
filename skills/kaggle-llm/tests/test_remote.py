"""Executable contract for the remote batch backend. See docs/remote-plan.md.

Each test class names the phase that must make it pass. Change a test only when
a live experiment disproves its assumption, and record why in the plan's log.
"""
import ast
import io
import json
import os
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import httpx

from kaggle_llm import Client, KaggleLLMError, remote, remote_runner
from kaggle_llm.cli import main
from kaggle_llm.client import build_request, finish
from kaggle_llm.dedup import near_duplicates, normalize

TOP = 'openai/gpt-6-astra'
OPUS = 'anthropic/claude-opus-5@default'
ENV = ('MODEL_PROXY_URL=https://proxy.example/models\n'
       'MODEL_PROXY_API_KEY=secret-test-token\n'
       'MODEL_PROXY_EXPIRY_TIME=2099-01-01T00:00:00Z\n'
       'LLMS_AVAILABLE=google/test\n')
DOLLAR = (500_000_000, 500_000_000)  # nanodollars: $1 per call


def completion(text='hello', *, model=None, cost=(1000, 2000), finish_reason='stop'):
    return {'id': 'cmpl', 'model': model,
            'choices': [{'finish_reason': finish_reason, 'message': {'content': text}}],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 1,
                      'cost': {'input_tokens_cost_nanodollars': cost[0],
                               'output_tokens_cost_nanodollars': cost[1]}}}


def make_spec(n=3, **changes):
    spec = {'version': 1, 'job_id': 'job123', 'candidates': [TOP, OPUS], 'execute_in': 'creation',
            'rows': [{'line': i + 1, 'id': f'r{i + 1}',
                      'messages': [{'role': 'user', 'content': f'prompt {i + 1}'}]} for i in range(n)],
            'options': {}, 'concurrency': 1, 'max_attempts': 3, 'backoff_seconds': 1.0,
            'max_cost_usd': None, 'deadline_seconds': None, 'dry_run': None}
    spec.update(changes)
    return spec


class FakeTime:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self):
        return self.now


class FakeCall:
    """Stands in for remote_runner.proxy_call.

    script maps a model, or (model, last user message), to outcomes. A list is
    consumed one outcome per call and then falls through to the default; any
    other value repeats forever. Outcomes: completion dict, HTTP status int, or
    'timeout'. respond(model, prompt) builds the default completion.
    """

    def __init__(self, script=None, respond=None, time=None, seconds_per_call=0):
        self.script, self.respond, self.time = dict(script or {}), respond, time
        self.seconds_per_call = seconds_per_call
        self.calls, self.lock = [], threading.Lock()

    def __call__(self, model, payload):
        prompt = payload['messages'][-1]['content']
        with self.lock:
            self.calls.append((model, prompt, payload))
            if self.time:
                self.time.now += self.seconds_per_call
            key = (model, prompt) if (model, prompt) in self.script else model
            outcome = self.script.get(key)
            if isinstance(outcome, list):
                outcome = outcome.pop(0) if outcome else None
        if outcome is None:
            outcome = self.respond(model, prompt) if self.respond else completion(model=model)
        if outcome == 'timeout':
            raise remote_runner.CallError(None, 'timeout')
        if isinstance(outcome, int):
            raise remote_runner.CallError(outcome, f'HTTP {outcome}')
        return outcome


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]


def fake_kbench():
    module = types.ModuleType('kaggle_benchmarks')
    module.llm = object()

    class Task:
        def __init__(self, fn, name):
            self.fn, self.name = fn, name

        def run(self, llm):
            return self.fn(llm)

    def task(name=None, description=None, **_):
        return lambda fn: Task(fn, name)

    module.task = task
    return module


def exec_task(source, call, results_path, environ=None):
    """Execute a rendered task file the way Kaggle would, with fakes injected."""
    namespace = {'__name__': '__main__', 'KAGGLE_LLM_CALL': call}
    env = {'KAGGLE_LLM_RESULTS': str(results_path), **(environ or {})}
    with patch.dict(sys.modules, {'kaggle_benchmarks': fake_kbench()}), patch.dict(os.environ, env):
        exec(compile(source, 'task.py', 'exec'), namespace)
    return namespace


# Phase 1 ---------------------------------------------------------------------

class ClientRefactorTests(unittest.TestCase):
    SCHEMA = {'type': 'object', 'properties': {'n': {'type': 'integer'}}, 'required': ['n']}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = Path(self.tmp.name) / 'credentials.env'
        self.env.write_text(ENV)
        self.requests = []

    def client(self, raw):
        def handler(request):
            self.requests.append(json.loads(request.content))
            return httpx.Response(200, json=raw)
        client = Client(env_file=self.env, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def test_build_request_matches_what_prompt_sends(self):
        messages, options, prepared = build_request(
            'Count.', system='Be brief.', schema=self.SCHEMA, schema_mode='native', max_tokens=50, reasoning='low')
        self.client(completion('{"n": 4}')).prompt(
            'Count.', system='Be brief.', schema=self.SCHEMA, schema_mode='native', max_tokens=50,
            reasoning='low', model='google/test')
        sent = self.requests[0]
        self.assertEqual(sent['messages'], messages)
        self.assertEqual(set(options), {'max_tokens', 'reasoning_effort', 'response_format'})
        for key, value in options.items():
            self.assertEqual(sent[key], value)
        self.assertIsNotNone(prepared)

    def test_build_request_omits_unset_options(self):
        messages, options, prepared = build_request('Hi')
        self.assertEqual(messages, [{'role': 'user', 'content': 'Hi'}])
        self.assertEqual(options, {})
        self.assertIsNone(prepared)

    def test_finish_matches_prompt_envelope(self):
        raw = completion('{"n": 4}', model='google/test')
        _, _, prepared = build_request('Count.', schema=self.SCHEMA)
        envelope = self.client(raw).prompt('Count.', schema=self.SCHEMA, model='google/test')
        self.assertEqual(finish(raw, prepared), envelope)
        self.assertEqual(envelope['structured_output'], {'n': 4})

    def test_finish_errors_match_local_rules(self):
        _, _, prepared = build_request('Count.', schema=self.SCHEMA)
        with self.assertRaisesRegex(KaggleLLMError, 'truncated'):
            finish(completion('x', finish_reason='length'), None)
        with self.assertRaisesRegex(KaggleLLMError, 'schema'):
            finish(completion('{"n": "four"}'), prepared)
        self.assertEqual(finish(completion('<think>hmm</think> done'), None)['text'], 'done')

    def test_build_request_validates_arguments(self):
        for kwargs in ({'max_tokens': 0}, {'temperature': 3}, {'reasoning': 'max'},
                       {'schema': {'type': 'nope'}}, {'schema_mode': 'strict'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                build_request('Hi', **kwargs)


class DedupTests(unittest.TestCase):
    WORDS = [f'w{i}' for i in range(30)]

    def test_normalize(self):
        self.assertEqual(normalize('  Hello,   WORLD!! '), 'hello world')

    def test_short_texts_match_only_exactly_after_normalizing(self):
        self.assertEqual(near_duplicates(['What is 2+2?', 'what is 2 + 2', 'What is 3+3?']), [(1, 0, 1.0)])

    def test_near_duplicate_reported_against_first_kept_item(self):
        text = ' '.join(self.WORDS)
        variant = ' '.join(self.WORDS[:-1] + ['changed'])
        distinct = ' '.join(f'x{i}' for i in range(30))
        found = near_duplicates([text, distinct, variant, text.upper()])
        self.assertEqual([(i, kept) for i, kept, _ in found], [(2, 0), (3, 0)])
        self.assertGreaterEqual(found[0][2], 0.85)
        self.assertEqual(found[1][2], 1.0)

    def test_threshold(self):
        text = ' '.join(self.WORDS)
        variant = ' '.join(self.WORDS[:-1] + ['changed'])
        self.assertEqual(near_duplicates([text, variant], threshold=0.95), [])


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / 'results.jsonl'
        self.time = FakeTime()

    def run_job(self, spec, call, environ=None):
        summary = remote_runner.run_job(spec, call, self.out, environ=environ or {},
                                        sleep=self.time.sleep, clock=self.time.clock)
        return summary, read_jsonl(self.out)

    @staticmethod
    def kind(lines, kind):
        return [line for line in lines if line['kind'] == kind]

    def test_probe_skips_unavailable_and_pins_first_answering(self):
        call = FakeCall({TOP: 404})
        summary, lines = self.run_job(make_spec(), call)
        job = self.kind(lines, 'job')[0]
        self.assertEqual(job['job_id'], 'job123')
        self.assertEqual(job['model'], OPUS)
        self.assertEqual(job['row_count'], 3)
        self.assertEqual(job['probe'], [{'model': TOP, 'status': 404}, {'model': OPUS, 'status': 200}])
        probe_payload = call.calls[0][2]
        self.assertEqual(probe_payload['messages'], remote_runner.PROBE_MESSAGES)
        self.assertEqual(probe_payload['max_tokens'], 256)
        self.assertEqual({model for model, _, _ in call.calls[2:]}, {OPUS})
        rows = self.kind(lines, 'row')
        self.assertEqual(sorted(row['line'] for row in rows), [1, 2, 3])
        self.assertTrue(all(row['ok'] and row['attempts'] == 1 for row in rows))
        self.assertEqual(lines[-1]['kind'], 'end')
        self.assertIsNone(lines[-1]['stopped_reason'])
        self.assertEqual((summary['rows_ok'], summary['rows_failed']), (3, 0))

    def test_row_payload_is_messages_plus_options(self):
        call = FakeCall()
        self.run_job(make_spec(1, candidates=[TOP], options={'max_tokens': 77, 'reasoning_effort': 'low'}), call)
        self.assertEqual(call.calls[1][2], {'messages': [{'role': 'user', 'content': 'prompt 1'}],
                                            'max_tokens': 77, 'reasoning_effort': 'low'})

    def test_probe_retries_transient_errors_before_pinning(self):
        call = FakeCall({TOP: [429, 429]})
        _, lines = self.run_job(make_spec(1), call)
        self.assertEqual(self.kind(lines, 'job')[0]['model'], TOP)
        self.assertEqual(self.time.sleeps, [1.0, 2.0])

    def test_probe_moves_on_after_exhausting_retries(self):
        call = FakeCall({TOP: 503})
        _, lines = self.run_job(make_spec(1, max_attempts=2), call)
        job = self.kind(lines, 'job')[0]
        self.assertEqual(job['model'], OPUS)
        self.assertEqual(job['probe'][0], {'model': TOP, 'status': 503})
        self.assertEqual(self.time.sleeps, [1.0])

    def test_no_model_answers(self):
        call = FakeCall({TOP: 404, OPUS: 403})
        _, lines = self.run_job(make_spec(), call)
        self.assertIsNone(self.kind(lines, 'job')[0]['model'])
        self.assertEqual(self.kind(lines, 'row'), [])
        self.assertEqual(lines[-1]['stopped_reason'], 'no_model')

    def test_row_retries_only_transient_failures(self):
        call = FakeCall({(TOP, 'prompt 2'): 400, (TOP, 'prompt 3'): [500], (TOP, 'prompt 1'): ['timeout']})
        summary, lines = self.run_job(make_spec(), call)
        rows = {row['line']: row for row in self.kind(lines, 'row')}
        self.assertEqual((rows[1]['ok'], rows[1]['attempts']), (True, 2))
        self.assertEqual((rows[2]['ok'], rows[2]['status'], rows[2]['attempts']), (False, 400, 1))
        self.assertEqual((rows[3]['ok'], rows[3]['attempts']), (True, 2))
        self.assertIn('raw', rows[1])
        self.assertEqual((summary['rows_ok'], summary['rows_failed']), (2, 1))

    def test_row_403_after_probe_is_retried(self):
        # Live: the proxy answers 403 to calls over its in-flight spend budget (plan log).
        call = FakeCall({(TOP, 'prompt 1'): [403, 403], (TOP, 'prompt 2'): 403})
        _, lines = self.run_job(make_spec(2, candidates=[TOP]), call)
        rows = {row['line']: row for row in self.kind(lines, 'row')}
        self.assertEqual((rows[1]['ok'], rows[1]['attempts']), (True, 3))
        self.assertEqual((rows[2]['ok'], rows[2]['status'], rows[2]['attempts']), (False, 403, 3))

    def test_probe_403_moves_on_without_retrying(self):
        call = FakeCall({TOP: 403})
        _, lines = self.run_job(make_spec(1), call)
        self.assertEqual(self.kind(lines, 'job')[0]['model'], OPUS)
        self.assertEqual(self.time.sleeps, [])

    def test_every_row_written_exactly_once_under_concurrency(self):
        _, lines = self.run_job(make_spec(50, concurrency=8), FakeCall())
        self.assertEqual(sorted(row['line'] for row in self.kind(lines, 'row')), list(range(1, 51)))

    def test_max_cost_stops_dispatch_and_counts_probe(self):
        call = FakeCall(respond=lambda model, prompt: completion(model=model, cost=DOLLAR))
        summary, lines = self.run_job(make_spec(candidates=[TOP], max_cost_usd=2.5), call)
        self.assertEqual([row['line'] for row in self.kind(lines, 'row')], [1, 2])
        self.assertEqual(lines[-1]['stopped_reason'], 'max_cost')
        self.assertAlmostEqual(lines[-1]['cost_usd'], 3.0)
        self.assertAlmostEqual(summary['cost_usd'], 3.0)

    def test_deadline_stops_dispatch(self):
        call = FakeCall(time=self.time, seconds_per_call=100)
        _, lines = self.run_job(make_spec(candidates=[TOP], deadline_seconds=250), call)
        self.assertEqual([row['line'] for row in self.kind(lines, 'row')], [1, 2])
        self.assertEqual(lines[-1]['stopped_reason'], 'deadline')

    def test_run_mode_skips_when_this_run_is_another_model(self):
        call = FakeCall()
        _, lines = self.run_job(make_spec(execute_in='run', candidates=[OPUS]), call,
                                environ={'LLM_DEFAULT': 'google/gemini-3.7-flash'})
        self.assertEqual(call.calls, [])
        self.assertTrue(self.kind(lines, 'job')[0]['skipped'])
        self.assertEqual(lines[-1]['stopped_reason'], 'skipped')

    def test_run_mode_executes_on_matching_model(self):
        _, lines = self.run_job(make_spec(execute_in='run', candidates=[OPUS]), FakeCall(),
                                environ={'LLM_DEFAULT': OPUS})
        self.assertEqual(len(self.kind(lines, 'row')), 3)

    def test_dry_run_makes_no_calls_and_heartbeats(self):
        call = FakeCall()
        _, lines = self.run_job(make_spec(dry_run={'sleep_seconds': 10, 'heartbeat_seconds': 5}), call)
        self.assertEqual(call.calls, [])
        job = self.kind(lines, 'job')[0]
        self.assertEqual((job['model'], job['row_count']), (None, 3))
        beats = [line['elapsed'] for line in self.kind(lines, 'heartbeat')]
        self.assertTrue(beats and beats == sorted(beats) and beats[-1] >= 10)
        self.assertEqual(lines[-1]['stopped_reason'], 'dry_run')

    def test_never_writes_environment_values(self):
        environ = {'MODEL_PROXY_API_KEY': 'sekrit-key', 'MODEL_PROXY_URL': 'https://proxy.example/secret-path',
                   'LLM_DEFAULT': TOP}
        self.run_job(make_spec(), FakeCall({(TOP, 'prompt 2'): 400}), environ=environ)
        text = self.out.read_text()
        self.assertNotIn('sekrit-key', text)
        self.assertNotIn('secret-path', text)

    def test_summary_is_numbers_only(self):
        summary, _ = self.run_job(make_spec(), FakeCall())
        self.assertEqual(set(summary), {'rows_ok', 'rows_failed', 'cost_usd'})
        self.assertTrue(all(isinstance(value, (int, float)) for value in summary.values()))


class ProxyCallTests(unittest.TestCase):
    ENVIRON = {'MODEL_PROXY_URL': 'https://proxy.example/models/openapi/', 'MODEL_PROXY_API_KEY': 'sekrit'}

    def test_posts_openai_compatible_request(self):
        captured = {}

        class Response(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        def urlopen(request, timeout=None):
            captured.update(url=request.full_url, auth=request.get_header('Authorization'),
                            body=json.loads(request.data), timeout=timeout)
            return Response(json.dumps(completion('hi')).encode())

        with patch('urllib.request.urlopen', urlopen):
            raw = remote_runner.proxy_call(TOP, {'messages': [{'role': 'user', 'content': 'x'}], 'max_tokens': 5},
                                           environ=self.ENVIRON)
        self.assertEqual(raw['choices'][0]['message']['content'], 'hi')
        self.assertEqual(captured['url'], 'https://proxy.example/models/openapi/chat/completions')
        self.assertEqual(captured['auth'], 'Bearer sekrit')
        self.assertEqual(captured['body'], {'model': TOP, 'stream': False, 'max_tokens': 5,
                                            'messages': [{'role': 'user', 'content': 'x'}]})
        self.assertTrue(captured['timeout'])

    def test_http_error_becomes_call_error_without_body_or_secrets(self):
        error = urllib.error.HTTPError('https://proxy.example', 404, 'Not Found', {}, io.BytesIO(b'private body'))
        with patch('urllib.request.urlopen', side_effect=error), \
                self.assertRaises(remote_runner.CallError) as caught:
            remote_runner.proxy_call(TOP, {'messages': []}, environ=self.ENVIRON)
        self.assertEqual(caught.exception.status, 404)
        self.assertNotIn('private body', str(caught.exception))
        self.assertNotIn('sekrit', str(caught.exception))

    def test_network_failures_have_no_status(self):
        for error in (urllib.error.URLError('refused'), TimeoutError()):
            with self.subTest(error=error), patch('urllib.request.urlopen', side_effect=error), \
                    self.assertRaises(remote_runner.CallError) as caught:
                remote_runner.proxy_call(TOP, {'messages': []}, environ=self.ENVIRON)
            self.assertIsNone(caught.exception.status)


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.results = Path(self.tmp.name) / 'results.jsonl'

    def test_percent_format_with_task_name_kaggle_accepts(self):
        source = remote.render_task(make_spec())
        self.assertIn('# %%', source)
        names = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.FunctionDef):
                for decorator in node.decorator_list:
                    if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                            and decorator.func.attr == 'task'):
                        names += [k.value.value for k in decorator.keywords if k.arg == 'name']
        self.assertEqual(names, [remote.SLUG])
        self.assertIn('.run(kbench.llm)', source.strip().splitlines()[-1])

    def test_rendered_task_executes_job(self):
        call = FakeCall()
        spec = make_spec(candidates=[TOP])
        spec['rows'][0]['messages'][0]['content'] = 'café 🚀'
        spec['local'] = {'schema': None, 'schema_mode': 'prompt'}
        namespace = exec_task(remote.render_task(spec), call, self.results)
        lines = read_jsonl(self.results)
        self.assertEqual(lines[0]['job_id'], 'job123')
        self.assertEqual(len([line for line in lines if line['kind'] == 'row']), 3)
        self.assertIn('café 🚀', [prompt for _, prompt, _ in call.calls])
        self.assertNotIn('local', namespace['SPEC'])

    def test_runner_is_stdlib_only_and_avoids_dataclasses(self):
        tree = ast.parse(Path(remote_runner.__file__).read_text())
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules |= {alias.name.split('.')[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, 'no relative imports: the source is embedded in the task')
                modules.add(node.module.split('.')[0])
        self.assertLessEqual(modules, set(sys.stdlib_module_names))
        self.assertNotIn('dataclasses', modules)


class PrepareJobTests(unittest.TestCase):
    ROWS = [{'line': 1, 'id': 'a', 'prompt': 'Explain gradient descent in two sentences for a beginner.'},
            {'line': 2, 'id': 'b', 'prompt': 'explain gradient descent in two sentences, for a beginner!'},
            {'line': 3, 'id': 'c', 'prompt': 'Write a haiku about autumn leaves falling on a quiet pond.'}]
    CATALOG = [OPUS, TOP, 'google/gemini-3.8-flash', 'qwen/qwen3-next-80b-a3b-instruct']

    def test_spec_contents(self):
        spec, dropped = remote.prepare_job(self.ROWS, catalog=self.CATALOG, system='Be brief.', max_tokens=100)
        self.assertEqual(spec['candidates'], [TOP, OPUS, 'google/gemini-3.8-flash'])
        self.assertEqual([row['line'] for row in spec['rows']], [1, 3])
        self.assertEqual([row['id'] for row in spec['rows']], ['a', 'c'])
        self.assertEqual(spec['rows'][0]['messages'], build_request(self.ROWS[0]['prompt'], system='Be brief.')[0])
        self.assertEqual(spec['options'], {'max_tokens': 100})
        self.assertEqual(len(dropped), 1)
        self.assertEqual({k: dropped[0][k] for k in ('line', 'id', 'duplicate_of_line')},
                         {'line': 2, 'id': 'b', 'duplicate_of_line': 1})
        self.assertAlmostEqual(dropped[0]['similarity'], 1.0)
        self.assertEqual(spec['local'], {'schema': None, 'schema_mode': 'prompt'})
        self.assertEqual((spec['version'], spec['execute_in'], spec['concurrency'], spec['max_attempts']),
                         (1, 'creation', 8, 4))
        self.assertIsNone(spec['dry_run'])
        self.assertEqual(len(spec['job_id']), 12)

    def test_remote_defaults_cap_tokens_and_cost(self):
        # Live: uncapped calls exhaust the proxy's in-flight budget one call at a time (plan log).
        spec, _ = remote.prepare_job(self.ROWS, catalog=self.CATALOG)
        self.assertEqual(spec['options'], {'max_tokens': remote.DEFAULT_MAX_TOKENS})
        self.assertEqual(spec['max_cost_usd'], remote.DEFAULT_MAX_COST_USD)
        spec, _ = remote.prepare_job(self.ROWS, catalog=self.CATALOG, max_tokens=None, max_cost_usd=None)
        self.assertEqual((spec['options'], spec['max_cost_usd']), ({}, None))

    def test_dedup_can_be_disabled(self):
        spec, dropped = remote.prepare_job(self.ROWS, catalog=self.CATALOG, dedup=False)
        self.assertEqual((len(spec['rows']), dropped), (3, []))

    def test_model_selection(self):
        self.assertEqual(remote.prepare_job(self.ROWS, catalog=None, model=OPUS)[0]['candidates'], [OPUS])
        self.assertEqual(remote.prepare_job(self.ROWS, catalog=self.CATALOG, model='claude-opus-5')[0]['candidates'],
                         [OPUS])
        with self.assertRaisesRegex(KaggleLLMError, 'catalog'):
            remote.prepare_job(self.ROWS, catalog=None)

    def test_native_schema(self):
        schema = {'type': 'object', 'properties': {'n': {'type': 'integer'}}}
        spec, _ = remote.prepare_job(self.ROWS, catalog=self.CATALOG, schema=schema, schema_mode='native')
        self.assertEqual(spec['options']['response_format']['json_schema']['schema'], schema)
        self.assertEqual(spec['local'], {'schema': schema, 'schema_mode': 'native'})

    def test_payload_over_kaggle_limit_is_refused(self):
        # E1: Kaggle rejects task notebooks of 1 MB or more at push time.
        with patch.object(remote, 'MAX_PAYLOAD_BYTES', 100), self.assertRaisesRegex(KaggleLLMError, 'too large'):
            remote.prepare_job(self.ROWS, catalog=self.CATALOG)


# Phase 3 ---------------------------------------------------------------------

class FakeBackend:
    """In-memory Kaggle. Each poll (task/runs) advances a pending creation by one
    step. When creation completes, its run completes; downloading a run executes
    the pushed task file with the injected FakeCall."""

    def __init__(self, call=None, *, creation_polls=2, public=False, creation_error=None,
                 final_run_state='completed'):
        self.call = call or FakeCall()
        self.creation_polls, self.public = creation_polls, public
        self.creation_error, self.final_run_state = creation_error, final_run_state
        self.version, self.state, self.remaining = 1, 'completed', 0
        self.run_states = {1: 'completed'}  # a stale run from an older version
        self.sources, self.results_override = {}, None

    def _tick(self):
        if self.state != 'pending':
            return
        self.remaining -= 1
        if self.remaining <= 0:
            current = 100 + self.version
            if self.creation_error:
                self.state = 'errored'
                self.run_states.pop(current, None)
            else:
                self.state = 'completed'
                self.run_states[current] = self.final_run_state

    def push(self, slug, source):
        assert slug == remote.SLUG
        if self.state == 'pending':
            raise AssertionError('push while creation is pending')
        self.version += 1
        self.state, self.remaining = 'pending', self.creation_polls
        self.run_states[100 + self.version] = 'pending'
        self.sources[100 + self.version] = source
        return self.version

    def task(self, slug):
        self._tick()
        error = self.creation_error if self.state == 'errored' else None
        return remote.TaskInfo(self.version, self.state, error, self.public)

    def runs(self, slug):
        self._tick()
        return [remote.RunInfo(run_id, 'gemini-3.7-flash', state) for run_id, state in self.run_states.items()]

    def download(self, run_id, dest):
        target = Path(dest) / remote.SLUG / str(self.version) / 'gemini-3.7-flash' / str(run_id)
        target.mkdir(parents=True, exist_ok=True)
        results = target / remote.RESULTS_NAME
        if run_id == 1:
            results.write_text(json.dumps({'kind': 'job', 'job_id': 'stale', 'model': None}) + '\n')
        elif self.results_override is not None:
            results.write_text(self.results_override)
        else:
            exec_task(self.sources[run_id], self.call, results)

    def log(self, slug):
        return 'Traceback: creation failed\n' * 3


class OrchestratorTests(unittest.TestCase):
    ROWS = [{'line': 1, 'id': 'a', 'prompt': 'first prompt about apples'},
            {'line': 2, 'id': 'b', 'prompt': 'second prompt about bananas'},
            {'line': 3, 'id': 'c', 'prompt': 'third prompt about cherries'}]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = remote.JobStore(Path(self.tmp.name) / 'jobs')
        self.time = FakeTime()

    def job(self, **kwargs):
        spec, dropped = remote.prepare_job(self.ROWS, catalog=[TOP, OPUS], **kwargs)
        return self.store.create(spec, dropped)

    def wait(self, backend, job_id, timeout=300):
        return remote.wait(self.store, backend, job_id, timeout=timeout, interval=5,
                           sleep=self.time.sleep, clock=self.time.clock)

    def run_job(self, backend, job_id):
        remote.submit(self.store, backend, job_id)
        self.wait(backend, job_id)
        return remote.collect(self.store, job_id)

    def test_happy_path(self):
        backend = FakeBackend()
        job_id = self.job()
        rows = self.run_job(backend, job_id)
        self.assertEqual([(row['line'], row['id'], row['ok']) for row in rows],
                         [(1, 'a', True), (2, 'b', True), (3, 'c', True)])
        self.assertEqual(rows[0]['result']['text'], 'hello')
        self.assertEqual(rows[0]['result']['model'], TOP)
        state = self.store.load_state(job_id)
        self.assertEqual((state['status'], state['version'], state['run_id']), ('downloaded', 2, 102))
        summary = remote.summary(self.store, job_id)
        self.assertEqual((summary['job_id'], summary['status'], summary['model']), (job_id, 'downloaded', TOP))
        self.assertEqual((summary['rows_ok'], summary['rows_failed'], summary['rows_not_run']), (3, 0, 0))
        self.assertEqual(summary['dropped_duplicates'], 0)
        self.assertGreater(summary['cost_usd'], 0)
        self.assertTrue((self.store.path(job_id) / 'task.py').exists())
        self.assertTrue((self.store.path(job_id) / 'raw.jsonl').exists())

    def test_store_is_private(self):
        job_id = self.job()
        self.assertEqual(self.store.path(job_id).parent.stat().st_mode & 0o777, 0o700)
        self.assertIn(job_id, [entry['job_id'] for entry in self.store.list()])

    def test_refuses_to_push_while_another_job_runs(self):
        backend = FakeBackend(creation_polls=10**6)
        first, second = self.job(), self.job()
        remote.submit(self.store, backend, first)
        with self.assertRaisesRegex(KaggleLLMError, 'still running'):
            remote.submit(self.store, backend, second)
        self.assertEqual(len(backend.sources), 1)

    def test_public_task_is_fatal(self):
        job_id = self.job()
        with self.assertRaisesRegex(KaggleLLMError, 'public'):
            remote.submit(self.store, FakeBackend(public=True), job_id)
        self.assertEqual(self.store.load_state(job_id)['status'], 'failed')

    def test_mismatched_job_id_in_results_fails(self):
        backend = FakeBackend()
        backend.results_override = json.dumps({'kind': 'job', 'job_id': 'someone-else', 'model': TOP}) + '\n'
        job_id = self.job()
        remote.submit(self.store, backend, job_id)
        with self.assertRaises(KaggleLLMError):
            self.wait(backend, job_id)
        self.assertEqual(self.store.load_state(job_id)['status'], 'failed')

    def test_creation_error_saves_log(self):
        backend = FakeBackend(creation_error='ValidationFailed')
        job_id = self.job()
        remote.submit(self.store, backend, job_id)
        with self.assertRaisesRegex(KaggleLLMError, 'creation.log'):
            self.wait(backend, job_id)
        self.assertEqual(self.store.load_state(job_id)['status'], 'failed')
        self.assertIn('creation failed', (self.store.path(job_id) / 'creation.log').read_text())

    def test_timeout_leaves_job_resumable(self):
        backend = FakeBackend(creation_polls=10**6)
        job_id = self.job()
        remote.submit(self.store, backend, job_id)
        with self.assertRaisesRegex(KaggleLLMError, job_id):
            self.wait(backend, job_id, timeout=30)
        self.assertNotIn(self.store.load_state(job_id)['status'], ('failed', 'downloaded'))
        backend.remaining = 1
        self.assertEqual(self.wait(backend, job_id)['status'], 'downloaded')

    def test_errored_run_partial_results_are_downloaded(self):
        backend = FakeBackend(final_run_state='errored')
        job_id = self.job()
        self.assertEqual(len(self.run_job(backend, job_id)), 3)

    def test_schema_validated_locally(self):
        schema = {'type': 'object', 'properties': {'n': {'type': 'integer'}}, 'required': ['n']}
        call = FakeCall(respond=lambda model, prompt: completion(
            'not json' if 'bananas' in prompt else '{"n": 1}', model=model))
        job_id = self.job(schema=schema)
        rows = self.run_job(FakeBackend(call), job_id)
        self.assertEqual([row['ok'] for row in rows], [True, False, True])
        self.assertEqual(rows[0]['result']['structured_output'], {'n': 1})
        self.assertIn('schema', rows[1]['error'])

    def test_kernel_row_failure_reported(self):
        call = FakeCall(respond=lambda model, prompt: 400 if 'bananas' in prompt else completion(model=model))
        rows = self.run_job(FakeBackend(call), self.job())
        self.assertFalse(rows[1]['ok'])
        self.assertIn('HTTP 400', rows[1]['error'])

    def test_max_cost_then_resume_merges_lineage(self):
        call = FakeCall(respond=lambda model, prompt: completion(model=model, cost=DOLLAR))
        backend = FakeBackend(call)
        job_id = self.job(max_cost_usd=1.5, concurrency=1)
        rows = self.run_job(backend, job_id)
        self.assertEqual([row['ok'] for row in rows], [True, False, False])
        self.assertIn('not run (stopped: max_cost)', rows[1]['error'])
        summary = remote.summary(self.store, job_id)
        self.assertEqual((summary['rows_not_run'], summary['stopped_reason']), (2, 'max_cost'))

        child = remote.resume(self.store, job_id, max_cost_usd=None)
        child_spec = self.store.spec(child)
        self.assertEqual([row['line'] for row in child_spec['rows']], [2, 3])
        self.assertEqual(child_spec['candidates'], self.store.spec(job_id)['candidates'])
        self.assertEqual(self.store.load_state(child)['parent'], job_id)
        merged = self.run_job(backend, child)
        self.assertEqual([(row['line'], row['ok']) for row in merged], [(1, True), (2, True), (3, True)])

    def test_output_near_duplicates_flagged_not_dropped(self):
        same = 'The quick brown fox jumps over the lazy dog near the river bank today.'
        call = FakeCall(respond=lambda model, prompt: completion(
            'Bananas are yellow and grow in tropical climates around the world.' if 'bananas' in prompt else same,
            model=model))
        rows = self.run_job(FakeBackend(call), self.job())
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2].get('near_duplicate_of'), 'a')
        self.assertNotIn('near_duplicate_of', rows[0])
        self.assertNotIn('near_duplicate_of', rows[1])


# Phase 4 ---------------------------------------------------------------------

class CliRemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backend = FakeBackend()
        for patcher in (patch.dict(os.environ, {'KAGGLE_LLM_JOBS_DIR': str(Path(self.tmp.name) / 'jobs')}),
                        patch('kaggle_llm.remote.default_backend', return_value=self.backend),
                        patch('kaggle_llm.best.fetch_catalog', return_value=[OPUS, TOP]),
                        patch('time.sleep')):
            patcher.start()
            self.addCleanup(patcher.stop)

    def input_file(self, rows):
        path = Path(self.tmp.name) / 'input.jsonl'
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        return str(path)

    def cli(self, *argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(list(argv))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def json_lines(text):
        rows = []
        for line in text.splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
        return rows

    def test_batch_remote_end_to_end(self):
        path = self.input_file([{'id': 1, 'prompt': 'first prompt about apples'},
                                {'id': 2, 'prompt': 'second prompt about bananas'},
                                {'id': 3, 'prompt': 'third prompt about cherries'},
                                {'id': 4, 'prompt': 'First prompt about apples!'}])
        code, out, err = self.cli('batch', path, '--remote')
        self.assertEqual(code, 0, err)
        rows = self.json_lines(out)
        self.assertEqual([(row['line'], row['ok']) for row in rows], [(1, True), (2, True), (3, True)])
        self.assertEqual(rows[0]['result']['model'], TOP)
        summaries = [row for row in self.json_lines(err) if 'job_id' in row]
        self.assertEqual(summaries[-1]['dropped_duplicates'], 1)
        self.assertEqual(summaries[-1]['model'], TOP)

    def test_batch_remote_default_caps(self):
        path = self.input_file([{'prompt': 'only prompt'}])
        code, out, _ = self.cli('batch', path, '--remote', '--detach')
        spec = remote.JobStore().spec(json.loads(out)['job_id'])
        self.assertEqual((spec['options'], spec['max_cost_usd']),
                         ({'max_tokens': remote.DEFAULT_MAX_TOKENS}, remote.DEFAULT_MAX_COST_USD))
        code, out, _ = self.cli('batch', path, '--remote', '--detach', '--max-tokens', '300', '--max-cost', '0.5')
        spec = remote.JobStore().spec(json.loads(out)['job_id'])
        self.assertEqual((spec['options'], spec['max_cost_usd']), ({'max_tokens': 300}, 0.5))

    def test_detach_status_collect(self):
        path = self.input_file([{'id': 'a', 'prompt': 'only prompt'}])
        code, out, _ = self.cli('batch', path, '--remote', '--detach')
        self.assertEqual(code, 0)
        job_id = json.loads(out)['job_id']
        code, out, _ = self.cli('remote', 'status', job_id)
        self.assertEqual((code, json.loads(out)['status']), (0, 'submitted'))
        code, out, _ = self.cli('remote', 'collect', job_id)
        self.assertEqual(code, 0)
        self.assertEqual([row['ok'] for row in self.json_lines(out)], [True])
        code, out, _ = self.cli('remote', 'list')
        self.assertIn(job_id, out)

    def test_failed_rows_exit_1(self):
        self.backend.call = FakeCall(respond=lambda model, prompt: 400 if 'bananas' in prompt
                                     else completion(model=model))
        path = self.input_file([{'prompt': 'first prompt about apples'}, {'prompt': 'second prompt about bananas'}])
        code, out, _ = self.cli('batch', path, '--remote')
        self.assertEqual(code, 1)
        self.assertEqual([row['ok'] for row in self.json_lines(out)], [True, False])

    def test_invalid_input_is_usage_error_before_submit(self):
        for rows in ([{'prompt': 'ok'}, {'nope': 1}], [{'id': True, 'prompt': 'x'}]):
            with self.subTest(rows=rows):
                code, _, _ = self.cli('batch', self.input_file(rows), '--remote')
                self.assertEqual(code, 2)
        for flags in (['--max-cost', '0'], ['--concurrency', '0'], ['--concurrency', '17']):
            with self.subTest(flags=flags):
                code, _, _ = self.cli('batch', self.input_file([{'prompt': 'x'}]), '--remote', *flags)
                self.assertEqual(code, 2)
        self.assertEqual(self.backend.sources, {})

    def test_remote_only_flags_require_remote(self):
        code, _, _ = self.cli('batch', self.input_file([{'prompt': 'x'}]), '--detach')
        self.assertEqual(code, 2)


if __name__ == '__main__':
    unittest.main()
