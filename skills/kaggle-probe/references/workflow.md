# Adapters, execution, and observations

## Adapter contract

Write one Python adapter defining four hooks at module scope. The adapter is
concatenated after `scripts/probe_runtime.py`, so avoid `from __future__`
imports and do not call the hooks at import time. The generated entrypoint
invokes them. Do not redefine `Codec` or `emit_signal`.

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
`quantity()`. Hidden runs compute the quantity first, encode the selected
digit, sleep, and produce the selected outcome. Writer hooks must use the same
filename passed to `prepare --submission-file`.

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

For digit `d`, `channel, bucket = divmod(d, K)` and sleep is
`bucket * resolution_hours * 3600`. Decoding uses
`d = channel * K + bucket`. Default settings yield base 32 and five digits;
fraction resolution is `1 / 32**5`. All leading zero digits are necessary.
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
Confirm them against actual errors and notebook behavior. A dependency crash
or setup failure is not a calibrated intentional exception. Runtime exceeded,
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

For monitoring, use `kaggle competitions submission "$SUBMISSION_REF"` when
available, or `api.competition_submissions(competition, ...)` filtered by
recorded refs and manifest descriptions. Paginate if targets are absent from
the first page. Start observing immediately, preserve raw snapshots with UTC
timestamps, and stop after all target submissions are terminal or a bounded
deadline covering expected runtime plus queue margin. Do not include unrelated
active/recent submissions. Poll at a modest interval (e.g. 10–30 seconds);
avoid sleeps over 60 seconds in an interactive agent turn.

Expected probe errors are terminal data, so an error result does not by itself
mean the experiment should be retried. On deadline, preserve partial results
and report missing digits. Terminal results already completed before timing
observation began need trustworthy recorded duration; otherwise omit their
elapsed time and report them as undecodable.

## Normalized observation format

Create `observations.json` as a JSON array. This is a normalized experiment
file, not raw Kaggle API output. Copy `description` from the manifest, retain
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

Additional provenance fields are retained by the file and ignored by the
decoder. Input array order does not matter: indices determine digit order.
Unknown channels, mixed run descriptions, duplicates, incomplete sets, and
off-grid timing fail decoding. Fractions are reported as quantization
intervals, upper-exclusive except the final interval includes 1; the interval
assumes correctly observed digits and is not a statistical confidence bound.
