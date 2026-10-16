"""Kaggle-side runner for embedded public notebooks; `known_scores.adapter_prefix` prepends it to an adapter.

A valid outcome `valid:i` can rerun a public notebook whose public score is already known, so the score identifies
the outcome. `EMBEDDED_NOTEBOOKS` and `INPUT_PATHS` are replaced by the generated block that follows this source.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

EMBEDDED_NOTEBOOKS = []  # [{'kernel', 'version', 'code_file', 'source'}, ...]
INPUT_PATHS = ()  # Competition input paths notebooks hard-code; `input_dir` rewrites them (smoke tests).
INPUT_ROOT = '/kaggle/input'  # Notebooks that scan it for the data instead see `mirror_input` (smoke tests).


def run_notebook(notebook, submission_file, input_dir=None, timeout=None):
    """Run an embedded notebook in a scratch directory; return the path of a copy of its submission file."""
    source = notebook['source']
    stray = Path.cwd() / submission_file
    stray.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as mirror:
        if input_dir is not None:
            for path in INPUT_PATHS:
                source = source.replace(path, str(input_dir))
            source = source.replace(INPUT_ROOT, str(mirror_input(input_dir, Path(mirror))))
        work = Path(folder)
        code = work / notebook['code_file']
        code.write_text(source)
        if code.suffix == '.ipynb':
            command = [sys.executable, '-m', 'nbconvert', '--to', 'notebook', '--execute',
                       '--ExecutePreprocessor.timeout=-1', '--output', 'executed.ipynb', code.name]
        else:
            command = [sys.executable, code.name]
        process = subprocess.Popen(command, cwd=work, start_new_session=True)
        try:
            status = process.wait(timeout=timeout)
        except BaseException:
            # On a timeout (including the probe watchdog's alarm) kill the whole tree. Jupyter starts the kernel
            # in its own session, so a process-group kill alone leaves it running, and an orphan could write a
            # submission after the probe has chosen its crash signal.
            kill_tree(process.pid)
            process.wait()
            stray.unlink(missing_ok=True)
            raise
        if status:
            raise subprocess.CalledProcessError(status, command)
        # Notebooks write either relative to their cwd or to the absolute /kaggle/working path.
        for path in (work / submission_file, stray):
            if path.is_file():
                kept = Path(tempfile.mkdtemp()) / submission_file
                shutil.move(path, kept)
                return kept
    raise ValueError(f"{notebook['kernel']} wrote no {submission_file}")


def mirror_input(input_dir, mirror):
    """Copy of the INPUT_ROOT tree (real directories, symlinked files) whose INPUT_PATHS directories hold
    `input_dir`'s files, so a notebook that finds the data by scanning (e.g. `rglob('train.csv')`) reads `input_dir`."""
    root = Path(INPUT_ROOT)
    for folder, dirs, files in os.walk(root, followlinks=True):
        target = mirror / Path(folder).relative_to(root)
        target.mkdir(parents=True, exist_ok=True)
        source = Path(folder)
        if str(source) in INPUT_PATHS:
            dirs[:], source, files = [], Path(input_dir), os.listdir(input_dir)
        for name in files:
            (target / name).symlink_to((source / name).resolve())
    return mirror


def kill_tree(root):
    children = {}
    for stat in Path('/proc').glob('[0-9]*/stat'):
        try:
            parent = int(stat.read_text().rsplit(')', 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(parent, []).append(int(stat.parent.name))
    stack, tree = [root], []
    while stack:
        pid = stack.pop()
        tree.append(pid)
        stack.extend(children.get(pid, []))
    for pid in tree:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
