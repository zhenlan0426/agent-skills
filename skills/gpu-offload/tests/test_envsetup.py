#!/usr/bin/env python3
"""Offline tests for envsetup. No installs or network; temporary venvs only."""
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

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'envsetup'
es = SimpleNamespace(**runpy.run_path(str(SCRIPT)))
G = es.cmd_install.__globals__


def install_args(**kw):
    base = dict(requirement=[], packages=[], allow=[], skip=[], wheels=None, venv=None,
                system=False, installer='auto', report=None, dry_run=False)
    base.update(kw)
    return SimpleNamespace(**base)


class RequirementParsing(unittest.TestCase):
    def test_names(self):
        cases = {'torch>=2.3': 'torch', 'Flash_Attn==2.8.3': 'flash-attn',
                 'peft': 'peft', 'transformers[torch]>=4.5; python_version>"3.8"': 'transformers',
                 'pkg @ https://x/y.whl': 'pkg', 'git+https://g/r.git#egg=My.Pkg': 'my-pkg',
                 '--extra-index-url https://x': None, '-e .': None}
        for line, name in cases.items():
            self.assertEqual(es.requirement_name(line), name, line)

    def test_includes_and_comments(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'base.txt').write_text('numpy  # pinned elsewhere\n# comment\n\n')
            (Path(d) / 'req.txt').write_text('-r base.txt\ntorch>=2.3\n-r req.txt\n-c pins.txt\n')
            self.assertEqual(es.read_requirements(Path(d) / 'req.txt'),
                             [('numpy', 'numpy'), ('torch', 'torch>=2.3'),
                              (None, f'-c {Path(d).resolve() / "pins.txt"}')])

    def test_satisfied_by(self):
        v = '2.11.0+cu128'
        yes = ['torch', 'torch>=2.3', 'torch==2.11.0', 'torch==2.11', 'torch==2.11.*', 'torch~=2.11',
               'torch~=2.11.0',
               'torch<3,>=2.10', 'torch!=2.10.0', 'torch==2.11.0+cu128', 'torch @ https://x']
        no = ['torch==2.4.0', 'torch<2.11', 'torch>2.11', 'torch==2.10.*', 'torch~=2.12',
              'torch~=2.0.0']
        for line in yes:
            self.assertTrue(es.satisfied_by(line, v), line)
        for line in no:
            self.assertFalse(es.satisfied_by(line, v), line)

    def test_platform_flags_cover_older_manylinux(self):
        flags = es.platform_flags('2.35', 'x86_64')
        for tag in ['manylinux_2_35_x86_64', 'manylinux_2_24_x86_64', 'manylinux_2_17_x86_64',
                    'manylinux2014_x86_64']:
            self.assertIn(tag, flags)
        self.assertNotIn('manylinux_2_36_x86_64', flags)


class InstallGuards(unittest.TestCase):
    """Refusals happen before any installer runs; `run_installer` would fail the
    test if one were reached."""

    def run_install(self, args, pkgs, cap=None, platform='kaggle'):
        cards = [{'name': 'T4', 'memory_mib': 15360, 'driver': 'x',
                  'compute_cap': f'{cap[0]}.{cap[1]}'}] if cap else []
        out = io.StringIO()
        with patch.dict(G, {'installed': lambda python: dict(pkgs), 'gpus': lambda: cards,
                            'platform_name': lambda: platform, 'driver_cuda': lambda: '13.0',
                            'in_venv': lambda python: True,
                            'externally_managed': lambda python: False}), \
                patch.object(es.subprocess, 'run', side_effect=AssertionError('installer ran')), \
                contextlib.redirect_stdout(out):
            code = es.cmd_install(args)
        return code, out.getvalue()

    def test_conflicting_torch_pin_is_refused(self):
        code, out = self.run_install(install_args(packages=['torch==2.4.0', 'peft']),
                                     {'torch': '2.11.0+cu128'})
        self.assertEqual(code, 2)
        self.assertIn('--allow torch', out)

    def test_flash_attn_refused_below_sm80(self):
        code, out = self.run_install(install_args(packages=['flash-attn']), {}, cap=(7, 5))
        self.assertEqual(code, 2)
        self.assertIn('--skip flash-attn', out)

    def test_flash_attn_source_build_refused_but_wheel_allowed(self):
        code, out = self.run_install(install_args(packages=['flash-attn']), {}, cap=(12, 0))
        self.assertEqual(code, 2)
        self.assertIn('would compile', out)
        # Already installed: nothing to build, so the installer is reached.
        with self.assertRaises(AssertionError):
            self.run_install(install_args(packages=['flash-attn']), {'flash-attn': '2.8.3'}, cap=(8, 9))
        with self.assertRaises(AssertionError):
            self.run_install(install_args(packages=['flash-attn @ https://x/flash_attn-2.8.3-cp313-linux_x86_64.whl']),
                             {}, cap=(12, 0))

    def test_local_base_interpreter_refused(self):
        with patch.dict(G, {'in_venv': lambda python: False}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = es.cmd_install(install_args(packages=['requests']))
        self.assertEqual(code, 2)
        self.assertIn('--venv', out.getvalue())


class Environment(unittest.TestCase):
    def test_installed_prefers_first_on_path(self):
        # A venv copy shadows the inherited one; the dict must say which imports.
        with tempfile.TemporaryDirectory() as d:
            for where, ver in (('a', '2.0'), ('b', '1.0')):
                info = Path(d, where, f'demo-{ver}.dist-info')
                info.mkdir(parents=True)
                (info / 'METADATA').write_text(f'Metadata-Version: 2.1\nName: demo\nVersion: {ver}\n')
            with patch.dict('os.environ', {'PYTHONPATH': f'{d}/a:{d}/b'}):
                self.assertEqual(es.installed(sys.executable)['demo'], '2.0')

    def test_system_site_venv_is_layered(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run([sys.executable, '-m', 'venv', '--without-pip',
                            '--system-site-packages', f'{d}/v'], check=True)
            self.assertTrue(es.layered(f'{d}/v/bin/python'))
            self.assertEqual(es.ensure_venv(f'{d}/v'), f'{d}/v/bin/python')
            subprocess.run([sys.executable, '-m', 'venv', '--without-pip', f'{d}/w'], check=True)
            self.assertFalse(es.layered(f'{d}/w/bin/python'))
            original = Path(d, 'w', 'pyvenv.cfg').read_text()
            with patch.object(es.subprocess, 'run', side_effect=AssertionError('installer ran')), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                code = es.cmd_install(install_args(venv=f'{d}/w', packages=['peft']))
            self.assertEqual(code, 2)
            self.assertIn('include-system-site-packages = true', output.getvalue())
            self.assertEqual(Path(d, 'w', 'pyvenv.cfg').read_text(), original)

    def test_existing_python_without_venv_config_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, 'bin').mkdir()
            Path(d, 'bin', 'python').symlink_to(sys.executable)
            with self.assertRaisesRegex(ValueError, 'refusing'):
                es.ensure_venv(d)

    def test_probe_omits_installer_environment_secrets(self):
        def fake_run(cmd, **kwargs):
            output = 'pip 24.1.2 from /site/pip (python 3.13)\n'
            if len(cmd) > 2 and cmd[-1].startswith('import sys; print("%d.%d.%d"'):
                output = '3.13.15\n'
            return subprocess.CompletedProcess(cmd, 0, output, '')

        with patch.dict('os.environ', {
                'PIP_INDEX_URL': 'https://example.com/simple?token=query-secret',
                'PIP_EXTRA_INDEX_URL': 'https://build-user:password-secret@packages.example/path-secret/simple',
                'UV_INDEX_URL': 'https://mirror.example/simple#fragment-secret',
                'UV_INDEX_PRIVATE_USERNAME': 'username-secret',
                'PIP_ARBITRARY': 'arbitrary-secret'}), \
                patch.dict(G, {'installed': lambda python: {}, 'run': fake_run,
                               'in_venv': lambda python: False,
                               'externally_managed': lambda python: False,
                               'uv_path': lambda: None, 'gpus': lambda: [],
                               'driver_cuda': lambda: None, 'online': lambda: False}), \
                tempfile.TemporaryDirectory() as d, \
                contextlib.redirect_stdout(io.StringIO()):
            fingerprint = Path(d, 'env.json')
            self.assertEqual(es.cmd_probe(SimpleNamespace(json=str(fingerprint))), 0)
            saved = fingerprint.read_text()
        self.assertNotIn('pip_index', json.loads(saved))
        self.assertNotIn('secret', saved)
        self.assertNotIn('example.com', saved)

    def test_probe_handles_mig_memory_and_compute_na(self):
        response = subprocess.CompletedProcess([], 0,
                                              'NVIDIA MIG, [N/A], 580.0, [N/A]\n', '')
        with patch.object(es.shutil, 'which', return_value='/usr/bin/nvidia-smi'), \
                patch.dict(G, {'run': lambda *a, **k: response}):
            cards = es.gpus()
            capability = es.gpu_capability()
        self.assertEqual(cards[0]['memory_mib'], None)
        self.assertIsNone(cards[0]['compute_cap'])
        self.assertIsNone(capability)

    def test_summary_formats_uv_and_unknown_gpu_memory(self):
        info = {
            'torch': None, 'gpus': [{'name': 'NVIDIA MIG', 'memory_mib': None,
                                     'compute_cap': None}],
            'uv': 'uv 0.8.17', 'pip': '24.1.2', 'platform': 'colab',
            'python': '3.13.15', 'executable': '/usr/bin/python3', 'venv': False,
            'externally_managed': False, 'site_writable': True, 'user_site': None,
            'online': False, 'driver_cuda': None, 'packages': {}, 'packages_total': 0,
            'protected': {}, 'disk_free_gb': 1.0,
        }
        output = es.summary(info)
        self.assertIn('uv 0.8.17', output)
        self.assertNotIn('uv uv', output)
        self.assertIn('NVIDIA MIG memory N/A', output)


class WheelBuild(unittest.TestCase):
    def test_unsupported_lock_entries_fail_before_download(self):
        entries = ['directpkg @ https://example.com/pkg.whl?token=secret',
                   'directpkg @ git+https://example.com/repo.git',
                   'directpkg @ file:///tmp/pkg', '-e /tmp/pkg']
        with tempfile.TemporaryDirectory() as d:
            envfile = Path(d, 'env.json')
            envfile.write_text(json.dumps({'all_packages': {'directpkg': '1.0'},
                                           'python': '3.12.3'}))
            args = SimpleNamespace(env=str(envfile), output=f'{d}/wheels', skip=[],
                                   requirement=[], packages=['directpkg'])
            for entry in entries:
                with self.subTest(entry=entry):
                    def fake_run(cmd, **kwargs):
                        self.assertEqual(cmd[:3], ['uv', 'pip', 'compile'])
                        Path(cmd[cmd.index('-o') + 1]).write_text(
                            '# resolved\n\notherpkg==1.0\n' + entry + '\n')
                        return subprocess.CompletedProcess(cmd, 0, '', '')

                    with patch.dict(G, {'uv_path': lambda: 'uv', 'run': fake_run}), \
                            contextlib.redirect_stdout(io.StringIO()) as output:
                        code = es.cmd_wheels(args)
                    self.assertEqual(code, 2)
                    self.assertIn('Direct URLs', output.getvalue())
                    self.assertNotIn('secret', output.getvalue())
                    self.assertNotIn('already has everything', output.getvalue())
                    self.assertEqual(list(Path(args.output).iterdir()), [])

    def test_fallback_wheel_tempdir_is_removed(self):
        real_tempdir = es.tempfile.TemporaryDirectory
        created = []

        class TrackedTempDir:
            def __init__(self, *args, **kwargs):
                self.inner = real_tempdir(*args, **kwargs)
                created.append(Path(self.inner.name))

            def __enter__(self):
                return self.inner.__enter__()

            def __exit__(self, *args):
                return self.inner.__exit__(*args)

        def fake_run(cmd, **kwargs):
            if cmd[0] == 'uv':
                lock = Path(cmd[cmd.index('-o') + 1])
                lock.write_text('purepkg==1.0\n')
                return subprocess.CompletedProcess(cmd, 0, '', '')
            if cmd[3] == 'download':
                return subprocess.CompletedProcess(cmd, 1, '', 'no target wheel')
            if cmd[3] == 'wheel':
                out = Path(cmd[cmd.index('-w') + 1])
                out.mkdir(parents=True, exist_ok=True)
                (out / 'purepkg-1.0-py3-none-any.whl').write_bytes(b'wheel')
                return subprocess.CompletedProcess(cmd, 0, '', '')
            raise AssertionError(cmd)

        with tempfile.TemporaryDirectory() as outer:
            root = Path(outer)
            envfile = root / 'env.json'
            envfile.write_text(json.dumps({'all_packages': {}, 'python': '3.12.3',
                                           'platform': 'kaggle',
                                           'glibc': '2.35', 'machine': 'x86_64'}))
            output = root / 'wheels'
            args = SimpleNamespace(env=str(envfile), output=str(output), skip=[],
                                   requirement=[], packages=['purepkg'])
            with patch.object(es.tempfile, 'TemporaryDirectory', TrackedTempDir), \
                    patch.dict(G, {'uv_path': lambda: 'uv', 'run': fake_run,
                                   'platform_flags': lambda *a: []}):
                with contextlib.redirect_stdout(io.StringIO()) as output_text:
                    code = es.cmd_wheels(args)
            self.assertEqual(code, 0)
            self.assertEqual((output / 'purepkg-1.0-py3-none-any.whl').read_bytes(), b'wheel')
        self.assertTrue(created)
        self.assertTrue(all(not path.exists() for path in created))
        self.assertIn('no immutable image digest', output_text.getvalue())


if __name__ == '__main__':
    unittest.main()
