#!/usr/bin/env python3
"""Offline behavioral tests. No Kaggle requests or GPU allocation."""
import contextlib
import io
import json
from pathlib import Path
import random
import runpy
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

kg = SimpleNamespace(**runpy.run_path(str(Path(__file__).resolve().parent.parent / 'scripts' / 'kgpu')))
G = kg.prepare.__globals__


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        (self.project / 'train.py').write_text('print("hello")\n')
        self.job = self.root / 'job'
        self.args = SimpleNamespace(project=str(self.project), job=str(self.job),
                                    owner='testuser', accelerator='cpu', run_timeout=300,
                                    internet=False, dataset=[], competition=[],
                                    kernel_source=[], exclude=[], setup=None)

    def prepare(self, command=None):
        with contextlib.redirect_stdout(io.StringIO()):
            kg.prepare(self.args, command or ['python', 'train.py'])
        return json.loads((Path(self.args.job) / 'job.json').read_text())

    def test_git_ignore_credentials_symlinks_and_nested_project(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.project / '.gitignore').write_text('ignored.bin\n')
        for name in ['ignored.bin', '.env', '.env.production', 'kaggle.json', 'key.pem', 'omit.txt']:
            (self.project / name).write_text('not uploaded')
        (self.project / 'link').symlink_to('/etc/hosts')
        self.args.exclude = ['omit.*']
        self.prepare()
        manifest = json.loads((self.job / 'manifest.json').read_text())
        self.assertEqual({f['path'] for f in manifest}, {'.gitignore', 'train.py'})
        metadata = json.loads((self.job / 'kernel-metadata.json').read_text())
        self.assertIs(metadata['is_private'], True)
        self.assertIs(metadata['enable_internet'], False)

    def test_refuses_nested_job_and_large_bundle(self):
        self.args.job = str(self.project / 'job')
        with self.assertRaises(ValueError):
            kg.prepare(self.args, ['python', 'train.py'])
        with patch.dict(G, {'UNPACKED_LIMIT': 1}):
            with self.assertRaises(ValueError):
                kg.bundle(self.project, [])
        with patch.dict(G, {'COMPRESSED_LIMIT': 1}):
            with self.assertRaises(ValueError):
                kg.bundle(self.project, [])

    def run_generated(self, command, gpu=False, setup=None):
        payload, _ = kg.bundle(self.project, [])
        working = self.root / 'remote'
        script = kg.runner(payload, command, 'test-run', gpu, setup)
        # Runtime substitutions isolate Kaggle's absolute output root and /tmp.
        script = script.replace("pathlib.Path('/kaggle/working')", f'pathlib.Path({str(working)!r})')
        script = script.replace("pathlib.Path('/tmp/kgpu-envsetup')",
                                f'pathlib.Path({str(self.root / "envsetup")!r})')
        script_path = self.root / 'kernel.py'
        script_path.write_text(script)
        run = subprocess.run([sys.executable, str(script_path)], capture_output=True, text=True)
        return run, working

    def test_large_incompressible_project_round_trip_and_rejection(self):
        data = random.Random(0).randbytes(kg.COMPRESSED_LIMIT - 1024)
        (self.project / 'payload.bin').write_bytes(data)
        job = self.prepare()
        sizes = job['sizes']
        self.assertGreater(sizes['compressed_bytes'], kg.COMPRESSED_LIMIT - 1024)
        self.assertLessEqual(sizes['compressed_bytes'], kg.COMPRESSED_LIMIT)
        self.assertEqual(sizes['source_bytes'], (self.job / 'kernel.py').stat().st_size)
        run, working = self.run_generated([sys.executable, 'train.py'])
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual((working / 'project/payload.bin').read_bytes(), data)
        # A real oversized incompressible archive must fail without preparing a job.
        (self.project / 'payload.bin').write_bytes(data + random.Random(1).randbytes(2048))
        self.args.job = str(self.root / 'oversized')
        with self.assertRaisesRegex(ValueError, 'private dataset'):
            kg.prepare(self.args, ['python', 'train.py'])
        self.assertFalse(Path(self.args.job).exists())

    def test_source_limit_counts_utf8_command_before_creating_job(self):
        # len(str) fits, but UTF-8 bytes do not.
        with self.assertRaisesRegex(ValueError, 'Kernel source'):
            kg.prepare(self.args, ['python', 'train.py', '\u754c' * (kg.SOURCE_LIMIT // 2)])
        self.assertFalse(self.job.exists())

    def test_submit_rechecks_source_before_recording_attempt(self):
        self.prepare()
        source = self.job / 'kernel.py'
        source.write_bytes(b'#' * (kg.SOURCE_LIMIT + 1))
        fake_cli = Mock(return_value='Kernel version 1 successfully pushed\n')
        with patch.dict(G, {'cli': fake_cli}), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'Kernel source'):
                kg.submit(self.args)
            fake_cli.assert_not_called()
            self.assertFalse((self.job / 'submission-attempt.json').exists())
            source.write_bytes(b'#' * kg.SOURCE_LIMIT)
            kg.submit(self.args)
        self.assertEqual(fake_cli.call_count, 1)
        attempt = json.loads((self.job / 'submission-attempt.json').read_text())
        self.assertEqual(attempt['source_bytes'], kg.SOURCE_LIMIT)

    def test_submit_cannot_switch_to_an_unchecked_code_file(self):
        self.prepare()
        path = self.job / 'kernel-metadata.json'
        metadata = json.loads(path.read_text())
        metadata['code_file'] = 'other.py'
        kg.save(path, metadata)
        with self.assertRaisesRegex(ValueError, 'code_file'):
            kg.submit(self.args)
        self.assertFalse((self.job / 'submission-attempt.json').exists())

    def test_submission_failure_and_timeout_preserve_diagnostics(self):
        for index, response in enumerate([
            subprocess.CompletedProcess([], 1, '', '400 Client Error: Bad Request\n'),
            subprocess.TimeoutExpired('kaggle', 120, output=b'partial response\n', stderr=b'diagnostic\n'),
        ]):
            self.args.job = str(self.root / f'failed-{index}')
            self.prepare()
            job_path = Path(self.args.job)
            options = {'side_effect': response} if isinstance(response, Exception) else {'return_value': response}
            with patch.object(subprocess, 'run', **options) as call, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises((ValueError, subprocess.TimeoutExpired)):
                    kg.submit(self.args)
                with self.assertRaises(FileExistsError):
                    kg.submit(self.args)
            self.assertEqual(call.call_count, 1)
            self.assertFalse((job_path / 'submitted.json').exists())
            log = (job_path / 'submission.log').read_text()
            self.assertIn('400 Client Error' if index == 0 else 'partial response\ndiagnostic', log)

    def test_runner_preserves_argv_and_outputs(self):
        tricky = 'spaces ; $HOME `uname` "quotes"'
        (self.project / 'train.py').write_text(
            'import os, pathlib, sys\n'
            'pathlib.Path(os.environ["KGPU_OUTPUT_DIR"], "result.txt").write_text(sys.argv[1])\n')
        run, working = self.run_generated([sys.executable, 'train.py', tricky])
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual((working / 'out/result.txt').read_text(), tricky)
        self.assertEqual(json.loads((working / 'kgpu-result.json').read_text())['exit_code'], 0)

    def test_runner_propagates_child_failure(self):
        (self.project / 'train.py').write_text('raise SystemExit(7)\n')
        run, working = self.run_generated([sys.executable, 'train.py'])
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(json.loads((working / 'kgpu-result.json').read_text())['exit_code'], 7)

    def test_setup_runs_before_command_and_failure_skips_it(self):
        (self.project / 'train.py').write_text(
            'import os, pathlib\n'
            'pathlib.Path(os.environ["KGPU_OUTPUT_DIR"], "ran").write_text("yes")\n')
        fake = ('import pathlib, sys\n'
                'if sys.argv[1] == "install":\n'
                '    pathlib.Path(sys.argv[sys.argv.index("--report") + 1]).write_text(" ".join(sys.argv[2:]))\n'
                '    sys.exit(int(sys.argv[2]))\n')
        for code in (0, 3):
            run, working = self.run_generated([sys.executable, 'train.py'],
                                              setup=(fake, [str(code), '-r', 'req.txt']))
            result = json.loads((working / 'kgpu-result.json').read_text())
            self.assertEqual(result['setup_exit_code'], code)
            self.assertEqual(result['exit_code'], code)
            self.assertEqual((working / 'out/ran').exists(), code == 0, run.stdout)
            self.assertTrue((working / 'out/envsetup.json').read_text().startswith(f'{code} -r req.txt'))
            subprocess.run(['rm', '-rf', str(working)])

    def test_setup_needs_internet_or_wheels(self):
        self.args.setup = '-r requirements.txt'
        with self.assertRaises(ValueError):
            self.prepare()
        self.args.setup = '-r requirements.txt --wheels /kaggle/input/wheels'
        self.assertEqual(self.prepare()['setup'], ['-r', 'requirements.txt', '--wheels', '/kaggle/input/wheels'])
        self.assertIn('def cmd_install', (self.job / 'kernel.py').read_text())

    def test_executable_permission_preserved(self):
        executable = self.project / 'run.sh'
        executable.write_text('#!/bin/sh\nprintf success\n')
        executable.chmod(0o755)
        run, _ = self.run_generated(['./run.sh'])
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('success', run.stdout)

    def test_missing_gpu_probe_fails_and_records_marker(self):
        import os
        payload, _ = kg.bundle(self.project, [])
        working = self.root / 'no-gpu'
        script = kg.runner(payload, [sys.executable, 'train.py'], 'no-gpu', True)
        script = script.replace("pathlib.Path('/kaggle/working')", f'pathlib.Path({str(working)!r})')
        env = dict(os.environ, PATH=str(self.root / 'empty-path'))
        run = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(json.loads((working / 'kgpu-result.json').read_text())['exit_code'], 1)

    def test_submit_error_with_zero_cli_exit_is_failure_and_no_retry(self):
        self.prepare()
        with patch.dict(G, {'cli': lambda *a, **k: 'Kernel push error: quota exceeded\n'}):
            with self.assertRaises(ValueError):
                kg.submit(self.args)
            with self.assertRaises(FileExistsError):
                kg.submit(self.args)
        self.assertFalse((self.job / 'submitted.json').exists())

    def test_gpu_probe_only_for_nvidia_shapes(self):
        for shape, gpu in [('NvidiaTeslaT4', True), ('Tpu1VmV38', False), ('cpu', False)]:
            self.args.accelerator, self.args.job = shape, str(self.root / shape)
            with contextlib.redirect_stdout(io.StringIO()):
                kg.prepare(self.args, ['python', 'train.py'])
            metadata = json.loads((self.root / shape / 'kernel-metadata.json').read_text())
            self.assertIs(metadata['enable_gpu'], gpu)
            self.assertIn(f'    if {gpu!r}:', (self.root / shape / 'kernel.py').read_text())

    def test_submit_accepts_unnumbered_success(self):
        self.prepare()
        response = 'Kernel version successfully pushed.  Please check progress at https://www.kaggle.com/code/testuser/job\n'
        with patch.dict(G, {'cli': lambda *a, **k: response}), contextlib.redirect_stdout(io.StringIO()):
            kg.submit(self.args)
        self.assertIsNone(json.loads((self.job / 'submitted.json').read_text())['version'])

    def test_submit_acceptance_and_invalid_sources(self):
        self.prepare()
        response = 'Kernel version 1 successfully pushed. Please check progress at https://www.kaggle.com/code/testuser/job\n'
        with patch.dict(G, {'cli': lambda *a, **k: response}):
            kg.submit(self.args)
        self.assertEqual(json.loads((self.job / 'submitted.json').read_text())['version'], 1)
        self.args.job = str(self.root / 'second')
        kg.prepare(self.args, ['python', 'train.py'])
        with patch.dict(G, {'cli': lambda *a, **k: 'The following are not valid dataset sources\n' + response}):
            with self.assertRaises(ValueError):
                kg.submit(self.args)

    def test_wait_terminal_states_and_timeout(self):
        self.prepare()
        args = SimpleNamespace(job=str(self.job), timeout=1, poll=1)
        for state, expected in [('complete', 0), ('error', 1), ('cancel_acknowledged', 1), ('running', 124)]:
            with patch.dict(G, {'status': lambda *a, **k: (state, state + '\n')}):
                self.assertEqual(kg.wait(args), expected)
        with patch.dict(G, {'status': lambda *a, **k: ('unexpected', '')}):
            with self.assertRaises(ValueError):
                kg.wait(args)

    def test_live_style_status_parser(self):
        with patch.dict(G, {'cli': lambda *a, **k: 'testuser/job has status "KernelWorkerStatus.COMPLETE"\n'}):
            self.assertEqual(kg.status({'ref': 'testuser/job'})[0], 'complete')

    def test_queue_timeout_survives_short_waits_and_never_resubmits(self):
        self.prepare()
        clock = SimpleNamespace(now=10000.0)
        clock.time = clock.monotonic = lambda: clock.now
        clock.sleep = lambda seconds: setattr(clock, 'now', clock.now + seconds)
        args = SimpleNamespace(job=str(self.job), timeout=45, poll=15, queue_timeout=900)
        remote = Mock(return_value=('queued', 'QUEUED\n'))
        unexpected_cli = Mock(side_effect=AssertionError('wait must not mutate remote state'))
        output = io.StringIO()
        with patch.dict(G, {'time': clock, 'status': remote, 'cli': unexpected_cli}), \
                contextlib.redirect_stdout(output):
            self.assertEqual(kg.wait(args), 124)
            self.assertNotIn('Workaround:', output.getvalue())
            self.assertEqual(json.loads((self.job / 'queued.json').read_text())['since'], 10000)
            clock.now = 10900
            self.assertEqual(kg.wait(args), 124)
        self.assertIn('https://www.kaggle.com/code/', output.getvalue())
        self.assertIn('Save Version > Save & Run All', output.getvalue())
        self.assertIn('remote job was not cancelled', output.getvalue())
        unexpected_cli.assert_not_called()

    def test_queue_limit_caps_poll_and_resets_on_running(self):
        self.prepare()
        clock = SimpleNamespace(now=10000.0)
        clock.time = clock.monotonic = lambda: clock.now
        clock.sleep = lambda seconds: setattr(clock, 'now', clock.now + seconds)
        args = SimpleNamespace(job=str(self.job), timeout=100, poll=60, queue_timeout=10)
        remote = Mock(return_value=('queued', 'QUEUED\n'))
        with patch.dict(G, {'time': clock, 'status': remote}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(kg.wait(args), 124)
            self.assertEqual(clock.now, 10010)
            # A fresh status wins over an expired queue timer.
            remote.side_effect = [('running', 'RUNNING\n'), ('complete', 'COMPLETE\n')]
            self.assertEqual(kg.wait(args), 0)
        self.assertFalse((self.job / 'queued.json').exists())

    def test_queue_timeout_can_be_disabled_and_terminal_state_clears_record(self):
        self.prepare()
        clock = SimpleNamespace(now=10000.0)
        clock.time = clock.monotonic = lambda: clock.now
        clock.sleep = lambda seconds: setattr(clock, 'now', clock.now + seconds)
        args = SimpleNamespace(job=str(self.job), timeout=1000, poll=500, queue_timeout=0)
        remote = Mock(return_value=('queued', 'QUEUED\n'))
        output = io.StringIO()
        with patch.dict(G, {'time': clock, 'status': remote}), contextlib.redirect_stdout(output):
            self.assertEqual(kg.wait(args), 124)
            self.assertEqual(clock.now, 11000)
            self.assertNotIn('Workaround:', output.getvalue())
            remote.return_value = ('error', 'ERROR\n')
            self.assertEqual(kg.wait(args), 1)
        self.assertFalse((self.job / 'queued.json').exists())

    def test_json_event_logs_and_plain_text(self):
        events = [{'stream_name': 'stdout', 'time': 1, 'data': 'hello\n'},
                  {'stream_name': 'stderr', 'time': 2, 'data': 'error\n'}]
        self.assertEqual(kg.readable_logs(json.dumps(events)), 'hello\nerror\n')
        for raw in ['plain log\n', '[{"data":', '{"unexpected": "shape"}']:
            self.assertEqual(kg.readable_logs(raw), raw)

    def test_pull_validates_run_identity_failure_and_missing_marker(self):
        job = self.prepare()
        args = SimpleNamespace(job=str(self.job), output='', partial=False, timeout=30)
        for index, marker in enumerate([{'run_id': job['run_id'], 'exit_code': 0},
                                        {'run_id': 'wrong-run', 'exit_code': 0},
                                        {'run_id': job['run_id'], 'exit_code': 7}, None]):
            args.output = str(self.root / f'results-{index}')
            def fake_cli(*a, **k):
                if marker is not None:
                    kg.save(Path(args.output) / 'kgpu-result.json', marker)
                return 'downloaded\n'
            with patch.dict(G, {'status': lambda *a, **k: ('complete', ''), 'cli': fake_cli}):
                if index == 0:
                    kg.pull(args)
                    with self.assertRaises(ValueError):
                        kg.pull(args)  # refuses mixing local results
                else:
                    with self.assertRaises(ValueError):
                        kg.pull(args)


if __name__ == '__main__':
    unittest.main()
