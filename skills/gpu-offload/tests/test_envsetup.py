#!/usr/bin/env python3
"""Offline tests for envsetup. No installs, no network; one venv is created."""
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
        yes = ['torch', 'torch>=2.3', 'torch==2.11.0', 'torch==2.11.*', 'torch~=2.11',
               'torch<3,>=2.10', 'torch!=2.10.0', 'torch==2.11.0+cu128', 'torch @ https://x']
        no = ['torch==2.4.0', 'torch<2.11', 'torch>2.11', 'torch==2.10.*', 'torch~=2.12']
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
            subprocess.run([sys.executable, '-m', 'venv', '--without-pip', f'{d}/w'], check=True)
            self.assertFalse(es.layered(f'{d}/w/bin/python'))


if __name__ == '__main__':
    unittest.main()
