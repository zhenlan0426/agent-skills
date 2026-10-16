#!/usr/bin/env python3
"""Public notebooks with known public scores as extra valid outcomes. See references/known-scores.md.

Library (imported by project wrappers) and CLI:
  pull    snapshot a public notebook's current version, its score, and pinned inputs
  smoke   build a kernel that test-runs embedded notebooks on the visible and a synthetic hidden-size test
  select  choose the largest smoke-passing set whose scores are pairwise >= --gap apart
  rules   watcher `valid_rules` score windows for a list of known scores
"""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE_KEYS = ('dataset_sources', 'kernel_sources', 'model_sources', 'competition_sources')


def load_notebook(folder):
    folder = Path(folder)
    embed = json.loads((folder / 'embed.json').read_text())
    return {**embed, 'source': (folder / embed['code_file']).read_text()}


def listing_entry(listing, kernel):
    for row in json.loads(Path(listing).read_text())['notebooks']:
        if row['kernel'] == kernel:
            return row
    raise ValueError(f'{kernel} has no score in {listing}; re-run fetch_listing.sh')


def pinned_datasets(api, sources, scored_at):
    """Pin each dataset to the version the scored run used: its current one, provided it predates that run."""
    pinned = []
    for ref in sources:
        owner, slug = ref.split('/')[:2]
        found = [d for d in api.dataset_list(user=owner, search=slug) if d.ref == f'{owner}/{slug}']
        if len(found) != 1:
            raise ValueError(f'Cannot resolve dataset {ref}')
        updated = datetime.fromisoformat(str(found[0].last_updated).replace('Z', '+00:00'))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if updated > scored_at:
            raise ValueError(f'{ref} changed after the scored run; its scored version cannot be pinned')
        pinned.append(f'{owner}/{slug}/{found[0].current_version_number}')
    return pinned


def pull(kernel, listing, dest_root, score=None, allow_kernel_sources=False):
    """Write <dest_root>/<owner>__<slug>/{code, embed.json}, refusing anything whose known score is ambiguous."""
    from kaggle.api.kaggle_api_extended import KaggleApi
    from kagglesdk.kernels.types.kernels_api_service import ApiGetKernelRequest
    owner, slug = kernel.split('/')
    dest = Path(dest_root) / f'{owner}__{slug}'
    if dest.exists():
        raise ValueError(f'{dest} exists; delete it to re-pull (the version and score may have changed)')
    row = listing_entry(listing, kernel)
    if not row['latest_complete']:
        raise ValueError(f"Version {row['latest_version']} did not complete, so it has no score to embed")
    if score is None and row['complete_versions'] != 1:
        raise ValueError(f"{row['complete_versions']} completed versions: the listed best score may belong to an "
                         'older version. Pass --score only after confirming the current version\'s score.')
    score = Decimal(score or row['best_public_score'])
    scored_at = datetime.fromisoformat(row['latest_run_created'].replace('Z', '+00:00'))
    api = KaggleApi()
    api.authenticate()
    request = ApiGetKernelRequest()
    request.user_name, request.kernel_slug = owner, slug
    with api.build_kaggle_client() as client:
        # One call returns metadata and source together, so the recorded version matches the code.
        response = client.kernels.kernels_api_client.get_kernel(request).to_dict(ignore_defaults=False)
    meta, blob = response['metadata'], response['blob']
    if meta['currentVersionNumber'] != row['latest_version']:
        raise ValueError('The notebook changed since the listing snapshot; re-run fetch_listing.sh')
    if meta['language'] != 'python':
        raise ValueError(f"Only Python notebooks can be embedded, got {meta['language']!r}")
    if any(not ref for ref in meta['kernelDataSources']):
        raise ValueError('A notebook-output input is private or deleted, so the probe cannot attach it')
    if meta['kernelDataSources'] and not allow_kernel_sources:
        raise ValueError('Reads other notebooks\' outputs, which cannot be pinned to the scored run; '
                         'pass --allow-kernel-sources to accept the drift risk')
    source = blob['source']
    if meta['kernelType'] == 'notebook':
        notebook = json.loads(source)
        for cell in notebook['cells']:
            if cell['cell_type'] == 'code':
                cell['outputs'], cell['execution_count'] = [], None
        source, code_file = json.dumps(notebook, indent=1) + '\n', f'{slug}.ipynb'
    else:
        code_file = f'{slug}.py'
    embed = {
        'kernel': kernel, 'version': meta['currentVersionNumber'], 'public_score': str(score),
        'scored_at': row['latest_run_created'], 'code_file': code_file, 'enable_gpu': meta['enableGpu'],
        'enable_tpu': meta['enableTpu'], 'machine_shape': meta['machineShape'] if meta['enableGpu'] else None,
        'docker_image': meta['dockerImage'],
        'dataset_sources': pinned_datasets(api, meta['datasetDataSources'], scored_at),
        # Notebook outputs cannot be pinned; they are the source notebook's latest output.
        'kernel_sources': meta['kernelDataSources'], 'model_sources': meta['modelDataSources'],
        'competition_sources': meta['competitionDataSources'],
    }
    dest.mkdir(parents=True)
    (dest / code_file).write_text(source)
    (dest / 'embed.json').write_text(json.dumps(embed, indent=2) + '\n')
    return {'folder': str(dest), **{k: embed[k] for k in ('version', 'public_score', 'enable_gpu', 'dataset_sources')}}


def valid_rules(scores, margin):
    """Inclusive score windows of +-margin around each known public score (valid:0, valid:1, ...); overlap is an
    error."""
    margin = Decimal(margin)
    windows = sorted((Decimal(s) - margin, Decimal(s) + margin, f'valid:{i}') for i, s in enumerate(scores))
    for (_, high, a), (low, _, b) in zip(windows, windows[1:]):
        if low <= high:
            raise ValueError(f'{a} and {b} score windows overlap; drop one notebook or lower the margin')
    return [{'score_min': str(lo), 'score_max': str(hi), 'outcome': name} for lo, hi, name in windows]


def kernel_settings(notebooks, allow_gpu, docker_image, competition):
    """Sources, accelerator, and image a kernel needs so every embedded notebook can run."""
    settings = {key: sorted({s for nb in notebooks for s in nb[key]} | ({competition} if key == 'competition_sources'
                                                                         else set())) for key in SOURCE_KEYS}
    if any(nb['enable_tpu'] for nb in notebooks):
        raise ValueError('TPU notebooks are not supported')
    gpu = any(nb['enable_gpu'] for nb in notebooks)
    if gpu and not allow_gpu:
        raise ValueError('An embedded notebook needs a GPU; every digit would then hold a GPU for its whole wait. '
                         'Pass --allow-gpu to accept that quota cost.')
    settings['enable_gpu'] = gpu
    shapes = {nb['machine_shape'] for nb in notebooks if nb['enable_gpu']}
    if len(shapes) > 1:
        raise ValueError(f'Embedded notebooks need different GPU machines: {shapes}')
    if shapes:
        settings['machine_shape'] = shapes.pop()
    images = {nb['docker_image'] for nb in notebooks}
    if docker_image:
        settings['docker_image'] = docker_image
    elif len(images) == 1:
        settings['docker_image'] = images.pop()
    elif images:
        raise ValueError(f'Embedded notebooks use different docker images; choose one with --docker-image: {images}')
    return settings


def adapter_prefix(notebooks, input_paths):
    """Kaggle-side runner source plus the generated EMBEDDED_NOTEBOOKS / INPUT_PATHS block, to put before hooks."""
    entries = [{k: nb[k] for k in ('kernel', 'version', 'code_file', 'source')} for nb in notebooks]
    return ((HERE / 'notebook_runner.py').read_text()
            + f'\n\n# ---- generated by known_scores.py ----\nEMBEDDED_NOTEBOOKS = {entries!r}\n'
            + f'INPUT_PATHS = {tuple(input_paths)!r}\n\n')


SMOKE_MAIN = '''

def smoke():
    """Run each embedded notebook on the visible test once and on a synthetic hidden-size test SMOKE_REPEATS times."""
    import json
    import time as clock
    results = []
    synthetic_dir = Path(tempfile.mkdtemp()) / 'input'
    synthetic, truth = synthetic_input(INPUT, synthetic_dir, SMOKE_ROWS)
    for index, notebook in enumerate(EMBEDDED_NOTEBOOKS, 1):
        runs = [('visible', None, read_test())] + [('synthetic', synthetic_dir, synthetic)] * SMOKE_REPEATS
        first = None
        for split, input_dir, test in runs:
            row = {'valid_index': index, 'kernel': notebook['kernel'], 'version': notebook['version'],
                   'split': split, 'rows': len(test)}
            started = clock.monotonic()
            try:
                submission = read_submission(run_notebook(notebook, SUBMISSION_FILE, input_dir, SMOKE_TIMEOUT))
                check_submission(submission, test)
                row['ok'] = True
                if split == 'synthetic':
                    predicted = list(predictions(submission, test))
                    if truth is not None:
                        row['train_accuracy'] = sum(p == t for p, t in zip(predicted, truth)) / len(predicted)
                    if first is None:
                        first = predicted
                    else:
                        row['agreement_with_first_run'] = sum(a == b for a, b in zip(predicted, first)) / len(first)
            except Exception as exc:
                row.update(ok=False, error=repr(exc)[:500])
            row['seconds'] = round(clock.monotonic() - started, 1)
            print(json.dumps(row), flush=True)
            results.append(row)
            Path('smoke.json').write_text(json.dumps(results, indent=2))


smoke()
'''


def smoke_kernel(notebooks, hooks_source, input_paths, kernel, output, settings, rows, repeats, timeout):
    """Write a private script kernel (smoke.py + kernel-metadata.json) that test-runs every embedded notebook.

    `hooks_source` must define INPUT, SUBMISSION_FILE, read_test(), read_submission(path),
    check_submission(submission, test), synthetic_input(source, dest, rows) -> (test, truth or None), and
    predictions(submission, test) -> labels aligned with test rows.
    """
    output = Path(output)
    if output.exists():
        raise ValueError(f'{output} exists; choose a new directory')
    script = (adapter_prefix(notebooks, input_paths) + hooks_source
              + f'\n\nSMOKE_ROWS = {rows}\nSMOKE_REPEATS = {repeats}\nSMOKE_TIMEOUT = {timeout}\n' + SMOKE_MAIN)
    compile(script, 'smoke.py', 'exec')
    metadata = {'id': kernel, 'title': kernel.split('/')[1], 'code_file': 'smoke.py',
                'language': 'python', 'kernel_type': 'script', 'is_private': True, 'enable_internet': False,
                **settings}
    output.mkdir(parents=True)
    (output / 'smoke.py').write_text(script)
    (output / 'kernel-metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
    return {'folder': str(output), 'kernel': kernel, 'kernel_settings': settings}


def select(folders, smoke_paths, baseline_score, gap='0.010', max_seconds=900, min_agreement=1.0, cpu_only=False):
    """Drop smoke failures, nondeterminism, and slow runs, then keep the largest set whose scores (with the
    baseline's) are pairwise >= gap apart. Greedy over sorted scores is optimal for this."""
    runs = {}
    for path in smoke_paths:
        for row in json.loads(Path(path).read_text()):
            runs.setdefault((row['kernel'], row['version']), []).append(row)
    eligible, rejected = [], {}
    for folder in folders:
        nb = load_notebook(folder)
        rows = runs.get((nb['kernel'], nb['version']), [])  # results for an older pull do not count
        synthetic = [r for r in rows if r['split'] == 'synthetic']
        seconds = max((r['seconds'] for r in synthetic), default=None)
        agreement = min((r.get('agreement_with_first_run', 1.0) for r in synthetic), default=None)
        if not rows or not all(r['ok'] for r in rows):
            reason = 'smoke failed or missing: ' + '; '.join(r.get('error', '') for r in rows if not r['ok'])
        elif len(synthetic) < 2 or agreement < min_agreement:
            reason = f'not shown deterministic (agreement {agreement})'
        elif seconds > max_seconds:
            reason = f'too slow ({seconds} s on {synthetic[0]["rows"]} rows)'
        elif cpu_only and nb['enable_gpu']:
            reason = 'needs a GPU'
        else:
            eligible.append({'folder': str(folder), 'kernel': nb['kernel'], 'score': Decimal(nb['public_score']),
                             'gpu': nb['enable_gpu'], 'seconds': seconds})
            continue
        rejected[nb['kernel']] = reason
    gap = Decimal(gap)
    chosen, last = [], None
    for nb in sorted(eligible, key=lambda n: (-n['score'], n['gpu'], n['seconds'])):
        if (last is None or last - nb['score'] >= gap) and nb['score'] - Decimal(baseline_score) >= gap:
            chosen.append(nb)
            last = nb['score']
    for nb in eligible:
        if nb not in chosen:
            rejected[nb['kernel']] = f"score {nb['score']} within {gap} of a chosen one"
    return {'chosen': [{**nb, 'score': str(nb['score'])} for nb in chosen], 'valid_outcomes': 1 + len(chosen),
            'needs_gpu': any(nb['gpu'] for nb in chosen), 'rejected': rejected,
            'notebook_args': ' '.join(f"--notebook {nb['folder']}" for nb in chosen)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('pull')
    p.add_argument('kernel', help='owner/slug')
    p.add_argument('--listing', required=True, help='Snapshot from fetch_listing.sh')
    p.add_argument('--dest', required=True, help='Folder that collects pulled notebooks')
    p.add_argument('--score', help='Override the listing score (required when several versions completed)')
    p.add_argument('--allow-kernel-sources', action='store_true')
    p = sub.add_parser('select')
    p.add_argument('--notebook', action='append', required=True)
    p.add_argument('--smoke', action='append', required=True, help='smoke.json files (kaggle kernels output)')
    p.add_argument('--baseline-score', required=True, help='Known score of valid:0 (e.g. a constant submission)')
    p.add_argument('--gap', default='0.010', help='Minimum score gap; must exceed 2 x the rules margin')
    p.add_argument('--max-seconds', type=float, default=900, help='Slowest synthetic run allowed')
    p.add_argument('--min-agreement', type=float, default=1.0)
    p.add_argument('--cpu-only', action='store_true')
    p = sub.add_parser('rules')
    p.add_argument('scores', nargs='+', help='valid:0, valid:1, ... known public scores')
    p.add_argument('--margin', default='0.004')
    args = parser.parse_args()
    try:
        if args.command == 'pull':
            result = pull(args.kernel, args.listing, args.dest, args.score, args.allow_kernel_sources)
        elif args.command == 'select':
            result = select(args.notebook, args.smoke, args.baseline_score, args.gap, args.max_seconds,
                            args.min_agreement, args.cpu_only)
        else:
            result = valid_rules(args.scores, args.margin)
        print(json.dumps(result, indent=2))
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, f'error: {exc}\n')


if __name__ == '__main__':
    main()
