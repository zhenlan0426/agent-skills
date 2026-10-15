#!/usr/bin/env python3
"""Prepare standalone Kaggle probes and encode/decode local observations."""

import argparse
import ast
from dataclasses import asdict
import json
from pathlib import Path
import re
import uuid

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
    codec = Codec(args.total_hours, args.resolution_hours, args.digits, args.valid_outcomes, args.allowance_hours)
    if args.runtime_limit_hours is not None:
        limit = decimal(args.runtime_limit_hours)
        if limit <= 0 or decimal(codec.total_hours) > limit:
            raise ValueError("Configured time window exceeds the runtime limit (or limit is not positive)")
    adapter = Path(args.adapter).read_text()
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
            "is_private": True, "enable_gpu": False, "enable_internet": False,
            "competition_sources": [args.competition], "dataset_sources": [], "kernel_sources": [],
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
                "runtime_limit_hours": args.runtime_limit_hours,
                "codec": asdict(codec), "probes": probes}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return {"manifest": str(root / "manifest.json"), "base": codec.base,
            "capacity": codec.capacity, "computation_allowance_seconds": str(codec.allowance_seconds),
            "buckets": codec.buckets, "max_target_seconds": str(codec.target_seconds(codec.buckets - 1)),
            "enable_gpu": False, "enable_internet": False,
            "submission_count": codec.digits}


def decode_observations(manifest, codec, observations, overhead, tolerance):
    expected = {p["index"]: p["description"] for p in manifest["probes"]}
    if len(manifest["probes"]) != codec.digits or set(expected) != set(range(codec.digits)):
        raise ValueError("Manifest must contain every probe index exactly once")
    digits = {}
    for row in observations:
        index = row["index"]
        if type(index) is not int or index not in expected or index in digits:
            raise ValueError("Unexpected or duplicate probe index")
        if row["description"] != expected[index]:
            raise ValueError("Observation description does not match this run")
        if row.get("issues"):
            raise ValueError(f"Digit {index} has unresolved observation issues: {row['issues']}")
        digits[index] = codec.observe(row["elapsed_seconds"], row["outcome"], overhead, tolerance)
        if row.get("elapsed_lower_seconds") is not None:
            lower = row["elapsed_lower_seconds"]
            if decimal(lower) > decimal(row["elapsed_seconds"]):
                raise ValueError("Observation time bounds are reversed")
            if codec.observe(lower, row["outcome"], overhead, tolerance) != digits[index]:
                raise ValueError("Polling time bounds do not identify the same digit")
    if set(digits) != set(expected):
        raise ValueError("Missing probe observations")
    ordered = [digits[i] for i in range(codec.digits)]
    return {"digits": ordered, **codec.decode(ordered, manifest["mode"])}


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
            child.add_argument("--overhead-seconds", required=True)
            child.add_argument("--tolerance-seconds", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(args)
        elif args.command == "watch":
            from probe_watch import watch_run
            result = watch_run(args)
        else:
            manifest, codec = load_manifest(args.manifest)
            if args.command == "encode":
                digits = codec.encode(args.value, manifest["mode"])
                result = {"digits": digits, "signals": [codec.signal(d) for d in digits],
                          **codec.decode(digits, manifest["mode"])}
            else:
                observations = json.loads(Path(args.observations).read_text())
                result = decode_observations(manifest, codec, observations, args.overhead_seconds, args.tolerance_seconds)
        print(json.dumps(result, indent=2))
    except (ValueError, KeyError, TypeError, OSError, SyntaxError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
