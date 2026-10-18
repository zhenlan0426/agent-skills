"""Timing-noise budget: decode tolerance and the narrowest safe bucket, measured from watched runs.

Kaggle finish times jitter (queueing, scoring, polling, possibly deliberate noise), so tolerance and bucket width come
from residuals observed on earlier runs on the same hardware, never from a guess. A residual is the observed elapsed
time minus the computation allowance minus its bucket's target, bracketed by the last pending and first terminal
poll. A run directory holds `manifest.json`, one `probe-NN/kernel-metadata.json` per digit, and the watcher output in
`watch/observations.json`.
"""

from decimal import Decimal
import json
import math
from pathlib import Path

from probe_runtime import Codec

NOISE_MULTIPLIER = 3  # tolerance >= 3 x the measured residual spread
TOLERANCE_FLOOR_SECONDS = 200  # until MIN_TIMING_DIGITS residuals back a tighter tolerance
MIN_TIMING_DIGITS = 10
# bucket >= 4 x tolerance: a residual past the tolerance is refused, and a silent misread into the neighbouring
# bucket needs an error of at least bucket - tolerance = 3 x tolerance.
BUCKETS_PER_TOLERANCE = 4


def run_uses_gpu(run, manifest):
    """Hardware pool of a run, read from its first kernel's metadata (edited metadata counts)."""
    folder = Path(run) / manifest["probes"][0]["folder"]
    return bool(json.loads((folder / "kernel-metadata.json").read_text()).get("enable_gpu", False))


def observed_timing(run):
    """(manifest, codec, observations) with each timed observation annotated by `bucket` and residual bounds."""
    manifest = json.loads((Path(run) / "manifest.json").read_text())
    codec = Codec(**manifest["codec"])
    path = Path(run) / "watch" / "observations.json"
    observations = json.loads(path.read_text()) if path.is_file() else []
    calibrate = manifest.get("calibrate")
    for row in observations:
        if row.get("elapsed_seconds") is None:
            continue
        lower = Decimal(str(row.get("elapsed_lower_seconds", row["elapsed_seconds"])))
        # A polling gap of half a bucket or more (a watcher outage) cannot place the digit, so it measures no noise.
        if Decimal(str(row["elapsed_seconds"])) - lower >= codec.step_seconds / 2:
            continue
        upper = Decimal(str(row["elapsed_seconds"])) - codec.allowance_seconds
        # A calibration knows its bucket, so even a residual past half a bucket is measured, not wrapped.
        bucket = (codec.signal(codec.calibration_digit(calibrate))["bucket"] if calibrate
                  else int((upper / codec.step_seconds).to_integral_value()))
        row["bucket"] = bucket
        row["residual_upper"] = float(upper - bucket * codec.step_seconds)
        row["residual_lower"] = float(lower - codec.target_seconds(bucket))
    return manifest, codec, observations


def timing_residuals(runs, exclude=None):
    """Residual interval of every cleanly observed digit in `runs`, skipping the run at `exclude`."""
    rows = []
    for run in runs:
        if exclude is not None and Path(run).resolve() == Path(exclude).resolve():
            continue
        manifest, _, observations = observed_timing(run)
        gpu = run_uses_gpu(run, manifest)
        rows += [{"run": manifest["run_id"], "index": row["index"], "gpu": gpu,
                  "lower": round(row["residual_lower"], 1), "upper": round(row["residual_upper"], 1)}
                 for row in observations if row.get("bucket") is not None and not row.get("issues")]
    return rows


def timing_budget(residuals, gpu):
    """Overhead, decode tolerance, and narrowest safe bucket for one hardware pool."""
    pool = [r for r in residuals if r["gpu"] == gpu]
    floor = TOLERANCE_FLOOR_SECONDS if len(pool) < MIN_TIMING_DIGITS else 0
    if not pool:
        return {"gpu": gpu, "digits": 0, "tolerance_seconds": floor,
                "min_resolution_seconds": BUCKETS_PER_TOLERANCE * floor}
    lowest, highest = min(r["lower"] for r in pool), max(r["upper"] for r in pool)
    # Any overhead in [highest lower bound, lowest upper bound] fits every observation; centre on the span.
    tolerance = math.ceil(max(NOISE_MULTIPLIER * (highest - lowest), floor))
    return {"gpu": gpu, "digits": len(pool), "overhead_seconds": round((lowest + highest) / 2),
            "residual_range_seconds": [lowest, highest], "spread_seconds": round(highest - lowest),
            "tolerance_seconds": tolerance, "min_resolution_seconds": BUCKETS_PER_TOLERANCE * tolerance}


def check_resolution(codec, budget, calibrate):
    """Refuse buckets the measured noise cannot support, and unmeasured hardware outside a calibration."""
    kind = "GPU" if budget["gpu"] else "CPU"
    if not budget["digits"] and not calibrate:
        raise ValueError(f"No measured timing for {kind} kernels in --noise-runs; prepare a --calibrate run first")
    if codec.step_seconds < budget["min_resolution_seconds"]:
        raise ValueError(f"{kind} timing noise needs buckets >= {budget['min_resolution_seconds']} s "
                         f"(tolerance {budget['tolerance_seconds']} s); raise --resolution-hours")
