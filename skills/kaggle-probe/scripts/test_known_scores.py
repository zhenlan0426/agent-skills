import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import known_scores

NOTEBOOK = {'kernel': 'someone/nb', 'version': 3, 'code_file': 'nb.py', 'source': 'print(1)\n',
            'public_score': '0.300', 'enable_gpu': False, 'enable_tpu': False, 'docker_image': 'img-a',
            'machine_shape': None, 'dataset_sources': ['someone/weights'], 'kernel_sources': [],
            'model_sources': [], 'competition_sources': ['comp']}


class KnownScoreTests(unittest.TestCase):
    def test_valid_rules_are_windows_and_reject_overlap(self):
        rules = known_scores.valid_rules(['0.055', '0.300'], '0.004')
        self.assertEqual(rules[0], {'score_min': '0.051', 'score_max': '0.059', 'outcome': 'valid:0'})
        with self.assertRaisesRegex(ValueError, 'overlap'):
            known_scores.valid_rules(['0.300', '0.055', '0.306'], '0.004')

    def test_kernel_settings_merge_sources_and_guard_gpu_and_images(self):
        settings = known_scores.kernel_settings([NOTEBOOK], False, None, 'comp')
        self.assertEqual((settings['dataset_sources'], settings['docker_image']), (['someone/weights'], 'img-a'))
        self.assertEqual(known_scores.kernel_settings([], False, None, 'comp')['competition_sources'], ['comp'])
        with self.assertRaisesRegex(ValueError, 'GPU'):
            known_scores.kernel_settings([{**NOTEBOOK, 'enable_gpu': True}], False, None, 'comp')
        other = {**NOTEBOOK, 'docker_image': 'img-b'}
        with self.assertRaisesRegex(ValueError, 'docker'):
            known_scores.kernel_settings([NOTEBOOK, other], False, None, 'comp')
        self.assertEqual(known_scores.kernel_settings([NOTEBOOK, other], False, 'img-b', 'comp')['docker_image'],
                         'img-b')

    def test_select_filters_smoke_failures_and_keeps_max_spaced_set(self):
        with tempfile.TemporaryDirectory() as folder:
            root, smoke, folders = Path(folder), [], []
            for name, score, gpu, ok, agreement, seconds in (
                    ('a', '0.505', True, True, 1.0, 600), ('b', '0.499', False, True, 1.0, 60),
                    ('c', '0.490', False, True, 1.0, 60), ('d', '0.480', True, True, 0.99, 60),
                    ('e', '0.470', True, False, 1.0, 60), ('f', '0.300', True, True, 1.0, 5000)):
                nb = root / name
                nb.mkdir()
                (nb / 'nb.py').write_text('')
                (nb / 'embed.json').write_text(json.dumps({**NOTEBOOK, 'kernel': f'x/{name}', 'public_score': score,
                                                           'enable_gpu': gpu}))
                folders.append(nb)
                smoke.append({'kernel': f'x/{name}', 'version': 3, 'split': 'visible', 'ok': ok, 'seconds': 1})
                smoke += [{'kernel': f'x/{name}', 'version': 3, 'split': 'synthetic', 'ok': ok, 'seconds': seconds,
                           'rows': 4000},
                          {'kernel': f'x/{name}', 'version': 3, 'split': 'synthetic', 'ok': ok, 'seconds': seconds,
                           'rows': 4000, 'agreement_with_first_run': agreement}]
            (root / 'smoke.json').write_text(json.dumps(smoke))
            result = known_scores.select(folders, [root / 'smoke.json'], '0.055')
            self.assertEqual([c['kernel'] for c in result['chosen']], ['x/a', 'x/c'])
            self.assertEqual(set(result['rejected']), {'x/b', 'x/d', 'x/e', 'x/f'})
            cpu = known_scores.select(folders, [root / 'smoke.json'], '0.055', cpu_only=True)
            self.assertEqual([c['kernel'] for c in cpu['chosen']], ['x/b'])

    def test_runner_returns_submission_and_kills_timed_out_tree(self):
        namespace = {}
        exec(known_scores.adapter_prefix([], ['/kaggle/input/comp']), namespace)
        with tempfile.TemporaryDirectory() as folder:
            writer = {'kernel': 'x/w', 'code_file': 'w.py',
                      'source': "open('out.csv', 'w').write(open('/kaggle/input/comp/x').read())\n"}
            Path(folder, 'x').write_text('rewritten input')
            path = namespace['run_notebook'](writer, 'out.csv', input_dir=folder)
            self.assertEqual(path.read_text(), 'rewritten input')
            sleeper = {'kernel': 'x/s', 'code_file': 's.py',
                       'source': "import time\ntime.sleep(30)\nopen('out.csv', 'w').write('late')\n"}
            with self.assertRaises(subprocess.TimeoutExpired):
                namespace['run_notebook'](sleeper, 'out.csv', timeout=1)


if __name__ == '__main__':
    unittest.main()
