# Adapters, execution, and observations

## Adapter contract

Write one Python adapter defining four hooks at module scope. The adapter is
embedded as source and executed by `run_probe` under the computation watchdog.
Do not call the hooks at import time or overwrite runtime helpers (`Codec`,
`emit_signal`, `run_probe`, `wait_until`, `decimal`, `signals`, or `time`). Do not
replace `SIGALRM` or `ITIMER_REAL`; the runtime reserves them on Kaggle/Linux.

| Hook | Behavior |
| --- | --- |
| `is_toy()` | Identify the visible/sample test; return a boolean. |
| `quantity()` | Compute the same numeric statistic on every hidden rerun; return int, Decimal, float, or decimal string. |
| `write_valid_submission(index)` | Write the configured output file for valid outcome `index`, starting at zero. |
| `write_invalid_submission()` | Write that file with a deliberately invalid schema that gives a calibrated format error. |

Example structure for a CSV competition, to adapt to its actual paths/schema:

```python
import csv
from pathlib import Path

INPUT = Path('/kaggle/input/competition-slug')
OUTPUT = Path('submission.csv')

def rows():
    with (INPUT / 'test.csv').open(newline='') as stream:
        return list(csv.DictReader(stream))

def is_toy():
    # Only use a size cutoff if visible and hidden test sizes justify it.
    return len(rows()) < 50

def quantity():
    # Example statistic, replace with the requested computation.
    return len(rows())

def write_valid_submission(index):
    if index != 0:
        raise ValueError('Only one valid outcome is implemented')
    # Example assumes sample_submission.csv has the required output shape.
    # Hidden reruns may change row IDs/count; regenerate those where required.
    OUTPUT.write_bytes((INPUT / 'sample_submission.csv').read_bytes())

def write_invalid_submission():
    OUTPUT.write_text('wrong_column\nwrong_value\n')
```

Use `--mode integer` with this example quantity. A row-count heuristic must not
be assumed correct for a different competition. Some competitions use an
inference server or gateway; adapt to that contract instead of assuming CSV
output alone is sufficient. If that contract cannot expose these outcomes,
explain that the timing/outcome method does not directly apply.

The runtime removes a stale submission before emitting an outcome. On a toy
run it calls `write_valid_submission(0)` immediately, without evaluating
`quantity()`. Hidden runs compute the quantity, encode the selected digit, and
prepare any output file before waiting for a fixed emission deadline. Writer
hooks must use the filename passed to `prepare --submission-file`. No adapter
code should register exit handlers or background work that changes the outcome
after the deadline.

The monotonic clock starts at the beginning of the generated script, before
runtime and adapter imports. All preparation must finish within the computation
allowance (`allowance_hours`, default one `resolution_hours` step). `run_probe` uses a Linux real-time alarm to bound
preparation and routes caught failures (including missing dependencies,
out-of-range values, writer errors, and timeouts) to the reserved first exception
slot. Toy failures exit immediately. Normal output is ready before waiting, so
its generation time does not shift the signal. Keep calculation comfortably
inside the allowance: Python alarms can be delayed by native code that does not
return control to Python, and process kills/platform failures cannot be routed
reliably. Reserving one slot does not detect every possible late external crash.

Quantity determinism matters across separate notebook reruns. Fix random
seeds, pin input/model versions, and avoid quantities whose fluctuations cross
quantization boundaries. Increasing the number of digits cannot repair a
statistic that varies between submissions.

## Encoding and calibration

The outcome channel order is fixed:

| `outcome` | Channel index | Action |
| --- | --- | --- |
| `exception` | 0 | Raise `RuntimeError('KAGGLE_PROBE_EXCEPTION')`. |
| `missing` | 1 | Exit without the submission file. |
| `format` | 2 | Call the invalid-file writer. |
| `valid:0`, `valid:1`, ... | 3, 4, ... | Call the corresponding valid-file writer. |

For digit `d`, `channel, bucket = divmod(d + 1, K)`. The exception channel's
bucket zero is reserved and is never emitted for data. Its deadline is one
allowance after startup; earlier unhandled startup errors are also
rejected as outside the data timing grid. Digit zero uses exception bucket one.
With `K = 1`, no intentional exception digits remain.

Every outcome deadline is `allowance_seconds + bucket * resolution_hours * 3600`
from script startup. Remaining wait is that deadline minus time already spent, including
calculation and writing. Maximum target equals `total_hours * 3600`; leave
platform margin below the competition runtime cap. Setup's optional
`--runtime-limit-hours` checks the full window, not just the remaining wait.

Decoding subtracts calibrated platform overhead and the computation allowance,
rounds to a bucket, then uses `d = channel * K + bucket - 1`. Reserved symbol
zero raises a crash error. Default settings yield base 31 and five digits;
fraction resolution is `1 / 31**5`. All leading zero digits are necessary.
Do not count nonzero digits to determine the number of probes: an integer
equal to `base**n` needs `n + 1` digits.

Use authorized calibration submissions with a known digit/statistic, or
existing measurements from the same environment. Validate the distinction
between all enabled channels. Historical Kaggle strings in the source utility
included:

- `Your notebook hit an unhandled error` → candidate `exception`.
- `Your notebook did not output the expected submission file` → candidate `missing`.
- `Your notebook generated a submission file with incorrect format` → candidate `format`.

These are historical observations, not a complete current API specification.
Confirm them against actual errors and notebook behavior. The generic exception
text alone cannot distinguish an intended exception from a crash; timing must
also pass the crash-reserving decoder. Runtime exceeded,
cancelled, unknown error, and pending statuses are not digit outcomes. Confirm
a valid terminal result and its distinguishing score before using `valid:n`.

Measure elapsed time with the same start/end convention for calibration and
observations. The old utility measured from submission creation to first poll
detecting completion; this includes queueing and poll delay and is only usable
if those variations fit the timing tolerance. Do not derive duration from when
a long-finished submission is first inspected. Prefer actual execution timing
when exposed by the target interface, and preserve its provenance. If timing
cannot be bounded below half a bucket, widen resolution or leave the digit
undecoded. A single fixed offset cannot compensate for arbitrary queue delays.

`decode` requires an explicit overhead estimate and tolerance, in seconds.
Tolerance must be nonnegative and strictly less than half the bucket width.
The corrected duration must be within tolerance of a configured bucket. No
negative clamping or bucket wraparound is performed.

## Upload and submit exact versions

The bundled helpers need only Python 3.10+. Live transport additionally needs
the authenticated Kaggle CLI/Python API. Reuse the existing credential setup;
never copy credentials or browser cookies into generated files. No browser
automation is required.

Official references: [kernel commands](https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels.md)
and [competition commands](https://github.com/Kaggle/kaggle-cli/blob/main/docs/competitions.md).
Check local `kaggle ... --help` for the installed version. For example:

```bash
kaggle kernels push -p probe-run/probe-00
kaggle kernels status "$EXACT_KERNEL"
kaggle competitions submit "$COMPETITION" \
  -f submission.csv -k "$EXACT_KERNEL" -v "$EXACT_VERSION" \
  -m "$MANIFEST_DESCRIPTION"
```

Use the exact kernel, index, and description from `manifest.json`. Save the
version returned by the push and the submission ref returned by acceptance in
a run ledger. Wait for the visible/toy save run to succeed before submitting
its version. Never look up an unpinned "latest version" after another push.
If the installed CLI does not expose the required version, use the Python API
push response or inspect that saved notebook version in the UI.

The Python equivalents are `KaggleApi.kernels_push(folder)` and
`KaggleApi.competition_submit_code(file_name=..., message=...,
competition=..., kernel=..., kernel_version=...)`. Authenticate first with
`api.authenticate()`. Inspect the installed response schema when recording
version/ref fields; they differ between client generations.

Submit only the authorized digits and budget, including any calibration runs.
If a network call has ambiguous acceptance, inspect existing submissions by
the unique manifest description before retrying. Do not retry unchanged after
a save failure or blindly loop competition submissions until accepted.

For monitoring, use the bundled `watch` command below. It uses the installed
Kaggle SDK's paginated submission list and per-ref detail calls, filtering by
the exact descriptions from the manifest. It never uploads or submits. Keep
the pinned notebook version and accepted ref in the run ledger; optionally add
`kernel_version` to each manifest probe before starting the watcher so that
normalized observations retain it.

Expected probe errors are terminal data, so an error result does not by itself
mean the experiment should be retried. On deadline, preserve partial results
and report missing digits. Terminal results already completed before timing
observation began need trustworthy recorded duration; otherwise omit their
elapsed time and report them as undecodable.

## Watch configuration and execution

Create a project-local `outcomes.json` with regex mappings calibrated for the
competition. This example uses the historical strings above; verify them before
using them as outcome signatures:

```json
{
  "error_rules": [
    {"pattern": "Your notebook hit an unhandled error", "outcome": "exception"},
    {"pattern": "Your notebook did not output the expected submission file", "outcome": "missing"},
    {"pattern": "Your notebook generated a submission file with incorrect format", "outcome": "format"}
  ]
}
```

Patterns are case-insensitive regular expressions matched against the
`errorDescription` of an `ERROR` submission, or of a `COMPLETE` one that carries
an error description: Kaggle reports missing and malformed submission files as
`COMPLETE` with an error and no score. Exactly one rule must match. Unknown
or overlapping matches remain undecodable, with the raw error preserved.
Timeout/cancelled statuses are terminal issues, never data outcomes. Do not use
catchall error patterns to turn unrelated failures into valid digits.

With one valid outcome, a clean `COMPLETE` result with a finite public score
maps to `valid:0`. With multiple valid outcomes, supply calibrated inclusive,
nonoverlapping score ranges in `valid_rules`, for example:

```json
{
  "valid_rules": [
    {"score_min": "0.10", "score_max": "0.11", "outcome": "valid:0"},
    {"score_min": "0.20", "score_max": "0.21", "outcome": "valid:1"}
  ]
}
```

Combine these with `error_rules` in the same file. The ranges are illustrative;
they must distinguish actual outcomes for the target competition. In Name That
LLM, constant predictions for different labels all score `1/18` if all 18 classes
are present, so those files cannot populate different ranges.

```bash
python "$SKILL_DIR/scripts/probe.py" watch \
  --manifest probe-run/manifest.json --mapping outcomes.json \
  --output probe-run/watch --poll-seconds 20 --timeout-seconds 18000
```

Start immediately after submission acceptance, preferably while submissions
are still pending. Choose the deadline to cover the configured runtime plus
queue margin. Poll intervals must be in `(0, 60]` seconds. `--page-size` defaults
to 100 and `--max-pages` to 20. The summary reports whether pagination exhausted
the list; increase the bound if targets may be beyond the scanned pages. Each
poll fetches details only for matching descriptions, refuses multiple refs for
one description, saves the matched raw response dictionaries under
`watch/snapshots/`, and writes `watch/state.json` and `watch/observations.json`.
On deadline it preserves partial results and reports missing/undecodable indices.
On transport errors or identity ambiguity it stops; inspect the saved evidence
before resuming. It does not retry or create submissions.

Run the same command to resume the same manifest and mapping. State preserves
the last pending timestamp and first terminal timing rather than measuring a
long-finished submission at each subsequent poll; outcomes are re-derived from
the stored terminal response on every poll, so classifier fixes apply on resume. A changed manifest or mapping
requires a separate watch directory. Raw snapshots permit later manual audit or
reclassification without another submission.

By default timing runs from submission `date` to the first terminal poll,
provided a pending state was observed. Both the poll's request-start timestamp
and response-completion timestamp are saved in UTC. The last pending poll's
request start forms the lower elapsed bound; first terminal response completion
forms the upper bound. The decoder requires both bounds to fit the same bucket
within the supplied tolerance. This does not remove queueing or scoring delay;
calibrate those using the same timing convention.

If a trusted seconds-valued duration exists in the full per-ref API response,
set `"duration_field": "ACTUAL_CALIBRATED_FIELD"` in the mapping. There is no
assumed Kaggle duration field. This explicitly selected field replaces the poll
duration, and can recover timing for already-terminal submissions. Calibrate
overhead for that field's start/end convention. If it is absent or invalid,
leave timing undecodable rather than falling back silently. Without such a
field, results already terminal on first observation get no elapsed time.

## Normalized observation format

The watcher writes `observations.json` as a JSON array; manual observations may
use the same schema. This is a normalized experiment file, not raw Kaggle API
output. Copy `description` from the manifest, retain
explicit zero-based indices, and map verified outcome signatures to the
channel vocabulary above. The example rows show the shape; supply every digit
in the actual manifest:

```json
[
  {
    "index": 0,
    "description": "kaggle-probe:RUN_ID:0",
    "elapsed_seconds": 3817,
    "outcome": "missing",
    "submission_ref": "123456",
    "kernel_version": 1,
    "error_description": "preserve actual raw error text here"
  }
]
```

Additional provenance fields are retained. `issues` must be resolved before
decoding; `elapsed_lower_seconds`, when present, is checked with the upper
`elapsed_seconds` bound. Other provenance is ignored by the decoder. Input
array order does not matter: indices determine digit order.
Unknown channels, mixed run descriptions, duplicates, incomplete sets, and
off-grid timing fail decoding. Fractions are reported as quantization
intervals, upper-exclusive except the final interval includes 1; the interval
assumes correctly observed digits and is not a statistical confidence bound.
