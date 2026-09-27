#!/usr/bin/env python3
"""Offline behavioral tests. No Kaggle requests or GPU allocation."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

kg = SimpleNamespace(**runpy.run_path(str(Path(__file__).with_name('kgpu'))))
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
                                    kernel_source=[], exclude=[])

    def prepare(self, command=None):
        with contextlib.redirect_stdout(io.StringIO()):
            kg.prepare(self.args, command or ['python', 'train.py'])
        return json.loads((self.job / 'job.json').read_text())

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

    def run_generated(self, command, gpu=False):
        payload, _ = kg.bundle(self.project, [])
        working = self.root / 'remote'
        script = kg.runner(payload, command, 'test-run', gpu)
        # The only runtime substitution isolates Kaggle's absolute output root.
        script = script.replace("pathlib.Path('/kaggle/working')", f'pathlib.Path({str(working)!r})')
        run = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
        return run, working

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
