---
name: kaggle-probe
description: >-
  Prepare, run, and decode Kaggle code-competition probes that encode a numeric
  quantity through submission runtime and distinguishable error or scoring
  outcomes. Use for Kaggle leaderboard probing, kaggle_probe.py/meta_probe.py
  workflows, or recovering a hidden-run statistic through timing and outcomes.
  Not for ordinary leaderboard downloads or model evaluation.
---

# Kaggle probe

Turn a numeric quantity computed during a hidden-test rerun into base-N digits,
one submission per digit. Each digit selects a timing bucket and an outcome:
exception, missing submission, malformed submission, or a distinguishable valid
submission. Decode observations back into an integer or a bounded fraction.
The earliest exception slot is reserved for computation failures and cannot
decode as digit zero.

## Prepare

Infer the competition and quantity from the current request and project docs.
Resolve only missing decisions: integer versus fraction, required precision,
the code-competition runtime limit, remaining submission budget, and how to
identify the visible/toy test run. Use the relevant competition rules when
choosing a probe. Do not assume another competition's schema, runtime, or quota.

Read [references/workflow.md](references/workflow.md) for adapter hooks,
calibration, transport, and watcher configuration. Work in a new project-local
run directory. Keep the installed skill reusable; put competition-specific
adapters, generated scripts, metadata, and observations in the project.

Use the bundled standard-library helpers, resolving `SKILL_DIR` to this skill's
actual location:

```bash
python "$SKILL_DIR/scripts/probe.py" prepare \
  --competition "$COMPETITION" --kernel "$KAGGLE_USER/$PROBE_SLUG" \
  --adapter adapter.py --output probe-run \
  --mode fraction --total-hours 4 --resolution-hours 0.5 --digits 5 \
  --runtime-limit-hours "$RUNTIME_LIMIT_HOURS"
python "$SKILL_DIR/scripts/probe.py" encode \
  --manifest probe-run/manifest.json --value 0.333333333333333333
```

With `K = total_hours / resolution_hours` and `V` valid outcomes,
`base = K * (3 + V) - 1`; capacity is `base ** digits`. Integer values must be
in `[0, capacity - 1]`. Fractions are in `[0, 1]`; endpoint 1 maps to the last
quantization interval. Digits are most significant first. Choose the smallest
digit count meeting the precision target and budget, using integer arithmetic
for integer capacity. Defaults give base 31 and capacity `31**5 = 28,629,151`,
about 85.3% of the previous 32-symbol capacity.

The first resolution interval is the computation allowance, including adapter
imports and output preparation. Outcome deadlines are `(bucket + 1) *
resolution_hours` after script startup; the last deadline equals `total_hours`.
Computation reduces the remaining wait rather than shifting the deadline.
Choose a window below the competition cap to leave startup, shutdown, and
platform margin. Optional `--runtime-limit-hours` rejects windows above the cap.

`prepare` only writes local files: a manifest and a standalone script plus
private Kaggle metadata per digit, with suffixes `-00`, `-01`, etc. Each script
embeds its runtime and adapter; no skill installation is needed on Kaggle.
Setup prints that GPU and internet are disabled in the metadata. Update required
datasets, accelerator, or competition settings before uploading. Schema-1
manifests use the old mapping and are rejected; prepare a new run.

## Run and decode

Develop and test the adapter locally with synthetic inputs first. Test the toy
branch: it must write a valid submission without sleeping or computing the
hidden quantity. Ensure the quantity is deterministic and identical across
reruns. Extra valid outcomes count only if their observable scores distinguish
them. For Name That LLM, any constant-class prediction has balanced accuracy
`1/18` when all 18 classes occur in the scored split; changing the constant
label does not create another distinguishable valid outcome.

Use the reference procedure to save exact notebook versions and submit with
manifest descriptions. Honor existing authorization: building this skill or
preparing a probe does not itself authorize consuming live submissions. When
execution is requested, proceed within that scope and budget. Stop on an
unexplained failure or ambiguous acceptance; inspect remote state before a
retry that might create a duplicate.

Calibrate timing overhead and outcome signatures for the target competition.
Do not reuse the historical 217-second correction or infer success from an
unrecognized error. Fixed deadlines remove computation-time variation while
preparation fits its allowance; queueing, scoring, shutdown, and polling still
require bounded uncertainty within the bucket tolerance.

Use `watch` as described in the reference to match manifest descriptions, save
raw responses, and produce normalized observations. Supply calibrated error
rules and score ranges for extra valid outcomes. Start watching before results
finish; existing terminal submissions need a trustworthy API duration field.
The watcher is read-only, resumable, and requires a deadline.

```bash
python "$SKILL_DIR/scripts/probe.py" decode \
  --manifest probe-run/manifest.json --observations probe-run/watch/observations.json \
  --overhead-seconds "$MEASURED_OVERHEAD" --tolerance-seconds "$TIMING_TOLERANCE"
```

The decoder requires every index exactly once, matches unique descriptions,
and rejects reserved crash signals, unresolved watcher issues, unknown outcomes,
or timing outside tolerance. Both ends of a polling interval must identify the
same digit within tolerance. Report an integer exactly, or a fraction's interval
and resolution. Preserve raw timestamps, submission refs, notebook versions,
error text, and scores. Report submissions actually used and undecodable digits.

For codec, runtime, emitter, or watcher changes, run:

```bash
python -m unittest discover -s "$SKILL_DIR/scripts" -p 'test_*.py'
```
