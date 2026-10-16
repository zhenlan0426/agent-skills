# Known-score valid outcomes from public notebooks

Each extra valid outcome multiplies the codec base by `(3 + V)` instead of `(3 + V - 1)`. Valid outcomes need
submissions whose public scores are already known and pairwise distinct. A constant prediction gives one. Public
notebooks give many at no calibration cost. Their leaderboard scores are listed, and rerunning one on the hidden test
reproduces its score. For digit outcome `valid:i` (i ≥ 1), the probe reruns the embedded notebook `i` and submits its
output. The watcher identifies the outcome from the score window.

Bits per submission = `log2(K · (3 + V) − 1)`. For example, with K = 31 buckets: V = 1 → 6.94 bits, V = 10 → 8.65.

## Tools (`scripts/known_scores.py`, `scripts/notebook_runner.py`)

| Step | Command / function |
| --- | --- |
| Snapshot scores | `scripts/fetch_listing.sh <slug> <competition-id> listing.json`. Anonymous curl to the endpoints behind kaggle.com's code page (`ListKernels` `bestPublicScore`, `ListKernelVersions`). The public API exposes neither. The id comes from `KaggleApi().competitions_list(search=slug)[0].id`. Only the top 100 by score are returned. |
| Pull | `known_scores.py pull owner/slug --listing listing.json --dest notebooks/` writes `<owner>__<slug>/{code, embed.json}`, with outputs stripped and inputs pinned. |
| Smoke | `known_scores.smoke_kernel(...)` builds a private script kernel. It runs each notebook once on the visible test and `repeats` times on a synthetic hidden-size test, then writes `smoke.json`. Push it and fetch the output. |
| Select | `known_scores.py select --notebook ... --smoke smoke.json --baseline-score <valid:0 score> --gap 0.010` |
| Rules | `known_scores.py rules <valid:0 score> <valid:1 score> ... --margin 0.004` gives the watcher `valid_rules` |
| Adapter | `known_scores.adapter_prefix(notebooks, input_paths)` + the project's hooks + `quantity()`. The hooks call `run_notebook(EMBEDDED_NOTEBOOKS[i-1], submission_file)`, which returns a path to a copy of the notebook's submission. |
| Kernel settings | `known_scores.kernel_settings(notebooks, allow_gpu, docker_image, competition)` is merged into every probe kernel's metadata. Pass `prepare --gpu` when it enables a GPU. |

The smoke kernel's hooks source must define `INPUT`, `SUBMISSION_FILE`, `read_test()`, `read_submission(path)`,
`check_submission(submission, test)`, `synthetic_input(source, dest, rows) -> (test, truth or None)`, and
`predictions(submission, test)` (labels aligned with test rows). `input_paths` lists the competition input paths that
notebooks hard-code, which the smoke run rewrites to the synthetic input. Notebooks that instead find the data by
scanning `/kaggle/input` (`rglob('train.csv')`, `glob('/kaggle/input/**/…')`) get `/kaggle/input` rewritten to a mirror
of that tree (`notebook_runner.mirror_input`: real dirs, symlinked files) whose `input_paths` dirs hold the synthetic
files. Without it they read the visible test and fail the id check on every synthetic run.

## Robustness rules (enforced unless marked manual)

- **Unambiguous score.** `pull` refuses a notebook whose latest version did not complete, or one with more than one
  completed version: `bestPublicScore` is the best over versions, and no endpoint gives per-version scores.
  `--score` overrides the second check only after a manual check of the current version's score. Only the current
  version is pullable (`kernels pull owner/slug/<v>` returns 403 for others' kernels).
- **Pinned inputs.** `pull` pins each dataset as `owner/slug/<version>`, and refuses if the dataset changed after the
  scored run. It refuses notebook-output inputs, which are unpinnable (`--allow-kernel-sources` overrides), and
  private or deleted inputs. Model sources carry their version. Probe kernels pin the notebook's `docker_image`, which
  Kaggle honours on push. Mixed images need an explicit `--docker-image`. Mixed GPU machine shapes are refused.
- **Smoke-tested.** `select` drops any notebook that failed, whose repeated synthetic runs disagree on any row, or
  whose slowest run exceeds `--max-seconds` (keep it ≤ half the allowance). Only results for the pulled `version`
  count. Greedy selection over sorted scores gives the largest set whose scores, including the baseline's, are
  pairwise ≥ `--gap` apart.
- **Separated windows.** With gap 0.010 and margin 0.004, a score drift of ≤ 0.004 still decodes, 0.004–0.006 is
  undecodable, and only > 0.006 could be misread. Estimate how far one flipped prediction moves the metric on the
  public split, and keep the margin well above it.
- **Crash-safe.** A notebook that errors or overruns the allowance sends the digit to the reserved crash slot, where
  it is lost but never misread. `run_notebook` kills the whole process tree on timeout or watchdog alarm, including
  the Jupyter kernel, which runs in its own session. An orphan therefore cannot write a submission after the crash
  signal.
- **Verified on first use (manual).** After a digit lands on `valid:i`, compare the observed and expected scores. A
  watcher issue "Expected exactly one outcome rule match" on a scored `COMPLETE` row means a rerun scored outside its
  window. Read the raw snapshot, and never widen windows to force a match. Calibrate one GPU notebook (`prepare
  --calibrate valid:<i>@0`) before relying on GPU digits. That one submission measures GPU timing and checks the
  notebook's score in your environment.
- **GPU cost (manual).** One GPU notebook makes every digit's kernel a GPU kernel, which holds the accelerator for its
  whole wait. That needs the user's approval. Whether submission reruns count against the weekly GPU quota is
  unknown. Build a CPU-only tier with `select --cpu-only` when GPU time matters.
