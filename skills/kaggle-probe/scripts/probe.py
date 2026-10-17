#!/usr/bin/env python3
"""Prepare standalone Kaggle probes and encode/decode local observations."""

import argparse
import ast
from dataclasses import asdict
import json
from pathlib import Path
import re
import uuid

from probe_noise import check_resolution, observed_timing, run_uses_gpu, timing_budget, timing_residuals
from probe_runtime import Codec, decimal


def load_manifest(path):
    manifest = json.loads(Path(path).read_text())
    if manifest.get("schema_version") != 2:
        raise ValueError("Unsupported manifest schema; prepare a new run with the crash-reserving codec")
    return manifest, Codec(**manifest["codec"])


def prepare(args):
    # Kaggle derives the slug from the title, so the slug must already be in slugified form.
    if not re.fullmatch(r"[\w-]+/[a-z0-9]{2,}(-[a-z0-9]+)*", args.kernel):
        raise ValueError("Kernel must be owner/slug, with a lowercase hyphenated slug of 2+ characters")
    for ref in args.dataset:
        if not re.fullmatch(r"[\w-]+/[\w.-]+/\d+", ref):
            raise ValueError(f"--dataset {ref}: use owner/slug/version so every rerun reads the same files")
    if args.calibrate and (args.mode, args.digits) != ("integer", 1):
        raise ValueError("--calibrate needs --mode integer --digits 1")
    codec = Codec(args.total_hours, args.resolution_hours, args.digits, args.valid_outcomes, args.allowance_hours)
    if not args.noise_runs and not args.calibrate:
        raise ValueError("Pass --noise-runs (watched runs on the same hardware) or prepare a --calibrate run first")
    budget = timing_budget(timing_residuals(args.noise_runs), args.gpu)
    check_resolution(codec, budget, args.calibrate)
    if args.runtime_limit_hours is not None:
        limit = decimal(args.runtime_limit_hours)
        if limit <= 0 or decimal(codec.total_hours) > limit:
            raise ValueError("Configured time window exceeds the runtime limit (or limit is not positive)")
    adapter = Path(args.adapter).read_text()
    if args.calibrate:
        adapter += f"\n\ndef quantity():\n    return {codec.calibration_digit(args.calibrate)}  # {args.calibrate}\n"
    tree = ast.parse(adapter, filename=args.adapter)
    hooks = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    required = {"is_toy", "quantity", "write_valid_submission", "write_invalid_submission"}
    if not required <= hooks:
        raise ValueError(f"Adapter is missing hooks: {', '.join(sorted(required - hooks))}")
    runtime = Path(__file__).with_name("probe_runtime.py").read_text()
    root = Path(args.output).resolve()
    if root.exists():
        raise ValueError("Output directory already exists; choose a new run directory")
    run_id = uuid.uuid4().hex[:12]
    probes = []
    for index in range(codec.digits):
        folder = root / f"probe-{index:02d}"
        kernel = f"{args.kernel}-{index:02d}"
        metadata = {
            "id": kernel, "title": kernel.split("/")[1],
            "code_file": "probe.py", "language": "python", "kernel_type": "script",
            "is_private": True, "enable_gpu": args.gpu, "enable_internet": False,
            "competition_sources": [args.competition], "dataset_sources": sorted(set(args.dataset)),
            "kernel_sources": [],
        }
        entrypoint = f'''
if __name__ == "__main__":
    _probe_codec = Codec(**{asdict(codec)!r})
    run_probe(_probe_codec, {index}, {args.mode!r}, {adapter!r},
              {args.submission_file!r}, _probe_started_at)
'''
        source = "import time\n_probe_started_at = time.monotonic()\n" + runtime + "\n" + entrypoint
        compile(source, str(folder / "probe.py"), "exec")
        probes.append({"index": index, "kernel": kernel, "folder": folder.name,
                       "description": f"kaggle-probe:{run_id}:{index}"})
        folder.mkdir(parents=True)
        (folder / "probe.py").write_text(source)
        (folder / "kernel-metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    manifest = {"schema_version": 2, "run_id": run_id, "competition": args.competition,
                "mode": args.mode, "submission_file": args.submission_file,
                "runtime_limit_hours": args.runtime_limit_hours, "calibrate": args.calibrate,
                "timing_budget": budget, "codec": asdict(codec), "probes": probes}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return {"manifest": str(root / "manifest.json"), "base": codec.base,
            "capacity": codec.capacity, "computation_allowance_seconds": str(codec.allowance_seconds),
            "buckets": codec.buckets, "max_target_seconds": str(codec.target_seconds(codec.buckets - 1)),
            "enable_gpu": args.gpu, "enable_internet": False, "timing_budget": budget,
            "submission_count": codec.digits}


def decode_digits(manifest, codec, observations, overhead, tolerance, residual_range=None):
    """Per-digit decode: a row per observation with its digit or the reason it is undecodable.

    Run-integrity problems (wrong run, unknown or duplicate index) raise; timing or outcome problems only mark
    their digit. `residual_range` (seconds past target seen on earlier runs) flags digits beyond measured noise.
    """
    expected = {p["index"]: p["description"] for p in manifest["probes"]}
    if len(manifest["probes"]) != codec.digits or set(expected) != set(range(codec.digits)):
        raise ValueError("Manifest must contain every probe index exactly once")
    rows, seen = [], set()
    for row in sorted(observations, key=lambda r: r["index"]):
        index = row["index"]
        if type(index) is not int or index not in expected or index in seen:
            raise ValueError("Unexpected or duplicate probe index")
        if row["description"] != expected[index]:
            raise ValueError("Observation description does not match this run")
        seen.add(index)
        out = {"index": index, "outcome": row.get("outcome")}
        try:
            if row.get("issues"):
                raise ValueError(f"Unresolved observation issues: {row['issues']}")
            digit = codec.observe(row["elapsed_seconds"], row["outcome"], overhead, tolerance)
            lower = row.get("elapsed_lower_seconds")
            if lower is not None:
                if decimal(lower) > decimal(row["elapsed_seconds"]):
                    raise ValueError("Observation time bounds are reversed")
                if codec.observe(lower, row["outcome"], overhead, tolerance) != digit:
                    raise ValueError("Polling time bounds do not identify the same digit")
            out["digit"] = digit
            if residual_range is not None:
                target = codec.target_seconds(codec.signal(digit)["bucket"])
                residuals = [float(decimal(v) - target) for v in (lower or row["elapsed_seconds"],
                                                                 row["elapsed_seconds"])]
                if not residual_range[0] <= residuals[0] <= residuals[1] <= residual_range[1]:
                    out["note"] = "beyond the measured noise range (still within tolerance)"
        except (ValueError, TypeError, KeyError) as exc:
            out["error"] = str(exc)
        rows.append(out)
    return rows


def decode_observations(manifest, codec, observations, overhead, tolerance):
    """All-or-nothing decode of a complete run."""
    rows = decode_digits(manifest, codec, observations, overhead, tolerance)
    failed = [f"digit {r['index']}: {r['error']}" for r in rows if "error" in r]
    if failed:
        raise ValueError("; ".join(failed))
    if len(rows) != codec.digits:
        raise ValueError("Missing probe observations")
    ordered = [r["digit"] for r in rows]
    return {"digits": ordered, **codec.decode(ordered, manifest["mode"])}


def decode(args):
    run = Path(args.manifest).parent
    manifest, codec = load_manifest(args.manifest)
    observations = json.loads(Path(args.observations).read_text())
    budget = None
    if args.noise_runs:
        # Other runs only, so a noisy digit cannot widen the tolerance that judges it.
        budget = timing_budget(timing_residuals(args.noise_runs, exclude=run), run_uses_gpu(run, manifest))
    if args.overhead_seconds is None or args.tolerance_seconds is None:
        if budget is None or not budget["digits"]:
            raise ValueError("Pass --overhead-seconds and --tolerance-seconds, or --noise-runs with measured digits "
                             "on this run's hardware")
    overhead = budget["overhead_seconds"] if args.overhead_seconds is None else args.overhead_seconds
    tolerance = budget["tolerance_seconds"] if args.tolerance_seconds is None else args.tolerance_seconds
    if not args.partial:
        return {"overhead_seconds": overhead, "tolerance_seconds": tolerance,
                **decode_observations(manifest, codec, observations, overhead, tolerance)}
    rows = decode_digits(manifest, codec, observations, overhead, tolerance,
                         budget.get("residual_range_seconds") if budget else None)
    digits = {r["index"]: r["digit"] for r in rows if "digit" in r}
    result = {"run_id": manifest["run_id"], "base": codec.base, "overhead_seconds": overhead,
              "tolerance_seconds": tolerance, "timing_budget": budget, "digits": rows,
              "undecoded": sorted(set(range(codec.digits)) - set(digits))}
    if not result["undecoded"]:
        result.update(codec.decode([digits[i] for i in range(codec.digits)], manifest["mode"]))
    return result


def noise(args):
    residuals = timing_residuals(args.runs)
    return {"residuals": residuals, "budgets": [timing_budget(residuals, gpu) for gpu in (False, True)]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare", help="Generate local standalone scripts and a manifest; no upload")
    prep.add_argument("--competition", required=True)
    prep.add_argument("--kernel", required=True, help="owner/slug prefix; digit suffixes are appended")
    prep.add_argument("--adapter", required=True)
    prep.add_argument("--output", required=True)
    prep.add_argument("--mode", choices=["fraction", "integer"], default="fraction")
    prep.add_argument("--total-hours", default="4")
    prep.add_argument("--runtime-limit-hours", help="Reject a configured window above this competition runtime cap")
    prep.add_argument("--resolution-hours", default="0.5")
    prep.add_argument("--allowance-hours", help="Computation allowance before the first deadline (default: one resolution step)")
    prep.add_argument("--digits", type=int, default=5)
    prep.add_argument("--valid-outcomes", type=int, default=1)
    prep.add_argument("--submission-file", default="submission.csv")
    prep.add_argument("--gpu", action="store_true", help="Enable a GPU in the kernel metadata (separate noise pool)")
    prep.add_argument("--noise-runs", nargs="+", default=[],
                      help="Watched run directories whose residuals set the timing budget; required unless --calibrate")
    prep.add_argument("--dataset", action="append", default=[],
                      help="owner/slug/version to attach to every digit; the adapter finds it with dataset_dir()")
    prep.add_argument("--calibrate", help="Emit one known signal, e.g. valid:0@0, instead of the adapter's quantity")
    noise_parser = sub.add_parser("noise", help="Timing residuals and CPU/GPU noise budgets of watched runs")
    noise_parser.add_argument("--runs", nargs="+", required=True)
    watch = sub.add_parser("watch", help="Read-only Kaggle polling; save snapshots and normalized observations")
    watch.add_argument("--manifest", required=True)
    watch.add_argument("--mapping", required=True, help="JSON error regexes, score ranges, and optional duration field")
    watch.add_argument("--output", required=True, help="Watch directory, resumed if already present")
    watch.add_argument("--poll-seconds", type=float, default=20)
    watch.add_argument("--timeout-seconds", type=float, required=True)
    watch.add_argument("--page-size", type=int, default=100)
    watch.add_argument("--max-pages", type=int, default=20)
    for command in ("encode", "decode"):
        child = sub.add_parser(command)
        child.add_argument("--manifest", required=True)
        if command == "encode":
            child.add_argument("--value", required=True)
        else:
            child.add_argument("--observations", required=True)
            child.add_argument("--overhead-seconds", help="Default: the --noise-runs budget")
            child.add_argument("--tolerance-seconds", help="Default: the --noise-runs budget")
            child.add_argument("--noise-runs", nargs="+", default=[], help="Watched runs (this one is excluded)")
            child.add_argument("--partial", action="store_true", help="Per-digit decode; tolerates missing digits")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(args)
        elif args.command == "noise":
            result = noise(args)
        elif args.command == "decode":
            result = decode(args)
        elif args.command == "watch":
            from probe_watch import watch_run
            result = watch_run(args)
        else:
            manifest, codec = load_manifest(args.manifest)
            digits = codec.encode(args.value, manifest["mode"])
            result = {"digits": digits, "signals": [codec.signal(d) for d in digits],
                      **codec.decode(digits, manifest["mode"])}
        print(json.dumps(result, indent=2))
    except (ValueError, KeyError, TypeError, OSError, SyntaxError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
