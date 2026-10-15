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
one submission per digit. Each digit selects a sleep duration and an outcome:
exception, missing submission, malformed submission, or a distinguishable valid
submission. Decode the observed durations and outcomes back into an integer or
a bounded fraction. This preserves the technique from the user's Utility
`kaggle_probe.py` and `meta_probe.py`, without their Jigsaw paths, browser
dependency, fixed five-submission limit, or 217-second timing offset.

## Prepare

Infer the competition and quantity from the current request and project docs.
Resolve only missing decisions: integer versus fraction, required precision,
the code-competition runtime limit, remaining submission budget, and how to
identify the visible/toy test run. Use the relevant competition rules when
choosing a probe. Do not treat all competitions as having Jigsaw's schema,
runtime, or daily quota.

Read [references/workflow.md](references/workflow.md) for adapter hooks,
calibration, transport, and the observation format. Work in a new project-local
run directory. Keep the installed skill reusable; put competition-specific
adapters, generated scripts, metadata, and observations in the project.

Use the bundled standard-library helpers, resolving `SKILL_DIR` to this skill's
actual location:

```bash
python "$SKILL_DIR/scripts/probe.py" prepare \
  --competition "$COMPETITION" --kernel "$KAGGLE_USER/$PROBE_SLUG" \
  --adapter adapter.py --output probe-run \
  --mode fraction --total-hours 4 --resolution-hours 0.5 --digits 5
python "$SKILL_DIR/scripts/probe.py" encode \
  --manifest probe-run/manifest.json --value 0.333333333333333333
```

The example time settings are illustrative. With `K = total_hours /
resolution_hours` and `V` valid outcomes, `base = K * (3 + V)`; capacity is
`base ** digits`. Integer values must be in `[0, capacity - 1]`. Fraction values
are in `[0, 1]`; the endpoint 1 maps to the last quantization interval. Digits
are most significant first. Choose the smallest digit count meeting the
precision target and budget, using integer arithmetic for integer capacity.
Maximum sleep is `(K - 1) * resolution_hours`, leaving one resolution interval
inside the configured window for computation and other overhead. Leave more
margin when needed.

`prepare` only writes local files. It creates a manifest and a standalone script
plus private Kaggle metadata per digit, with kernel suffixes `-00`, `-01`, etc.
Each generated script embeds its own runtime and adapter; no Utility package
or skill installation is needed on Kaggle. Update metadata for required
datasets, accelerator, or other competition settings before uploading.

## Run and decode

Develop and test the adapter locally with synthetic inputs first. Test the toy
branch: it must write a valid submission without sleeping or computing the
hidden quantity. Ensure the quantity is deterministic and identical across all
digit reruns. Extra valid outcomes count only if their observable scores can
actually distinguish them; multiple valid files are not automatically distinct.

Use the transport procedure in the reference to save exact notebook versions,
submit with manifest descriptions, and monitor only those submissions. Honor
existing authorization: a request to build this skill or prepare a probe does
not itself authorize consuming live competition submissions. When execution
is requested, proceed within that authorized scope and stated submission
budget. Stop on an unexplained failure or ambiguous submission acceptance;
inspect remote state before any retry that might create a duplicate.

Calibrate timing overhead and outcome signatures for the target competition.
Do not reuse the historical 217-second correction or interpret an unrecognized
error as success. Queue time, compute time, scoring time, and polling delay can
all affect elapsed time; uncertainty must fit the chosen bucket tolerance.

```bash
python "$SKILL_DIR/scripts/probe.py" decode \
  --manifest probe-run/manifest.json --observations observations.json \
  --overhead-seconds "$MEASURED_OVERHEAD" --tolerance-seconds "$TIMING_TOLERANCE"
```

The decoder requires every index exactly once, matches the manifest's unique
descriptions, and rejects unknown outcomes or timing outside tolerance. Report
an integer exactly, or a fraction's lower/upper interval and resolution. Keep
raw timestamps, submission refs, notebook versions, error text, and scores
alongside normalized observations so an uncertain decoding can be audited.
Report the submissions actually used and any undecodable digits.

For codec or emitter changes, run:

```bash
python -m unittest discover -s "$SKILL_DIR/scripts" -p 'test_*.py'
```
