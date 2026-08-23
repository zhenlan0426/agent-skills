#!/usr/bin/env python3
"""Download/refresh Kaggle notebooks and convert them without running cells.

Examples:
    download_and_convert.py path/to/notebook.ipynb notebooks/
    download_and_convert.py --refresh --destination notebooks --top-n 30
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

import ipynb_to_py


DEFAULT_COMPETITION = "ai-agent-security-multi-step-tool-attacks"
DEFAULT_TOP_N = 30
DEFAULT_SORT_ORDERS = ("hotness", "voteCount", "scoreDescending")
RunFn = Callable[..., subprocess.CompletedProcess[str]]


def positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def parse_kernel_refs(csv_text: str) -> list[str]:
    """Return distinct kernel refs in Kaggle's reported order."""

    refs: list[str] = []
    seen: set[str] = set()
    for row in csv.DictReader(csv_text.splitlines()):
        ref = (row.get("ref") or "").strip()
        if ref and ref not in seen:
            refs.append(ref)
            seen.add(ref)
    return refs


def list_top_refs(
    *,
    competition: str,
    top_n: int,
    sort_orders: Sequence[str],
    kaggle: str,
    run: RunFn = subprocess.run,
) -> list[str]:
    """Return an ordered union of the top-N results for every requested sort."""

    refs: list[str] = []
    seen: set[str] = set()
    for sort_by in sort_orders:
        completed = run(
            [
                kaggle,
                "kernels",
                "list",
                "--competition",
                competition,
                "--sort-by",
                sort_by,
                "--page-size",
                str(top_n),
                "--csv",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        for ref in parse_kernel_refs(completed.stdout):
            if ref not in seen:
                refs.append(ref)
                seen.add(ref)
    return refs


def ref_slug(ref: str) -> str:
    return ref.replace("/", "__")


def folder_for_ref(destination: Path, rank: int, ref: str) -> Path:
    """Reuse a previous folder even if a notebook's rank changes."""

    suffix = f"__{ref_slug(ref)}"
    matches = sorted(
        path for path in destination.iterdir() if path.is_dir() and path.name.endswith(suffix)
    )
    return matches[0] if matches else destination / f"{rank:02d}{suffix}"


def refresh_notebooks(
    *,
    destination: Path,
    competition: str,
    top_n: int,
    sort_orders: Sequence[str],
    kaggle: str,
    run: RunFn = subprocess.run,
) -> tuple[list[Path], list[str]]:
    """Pull the current selection and return successful source folders and failures."""

    refs = list_top_refs(
        competition=competition,
        top_n=top_n,
        sort_orders=sort_orders,
        kaggle=kaggle,
        run=run,
    )
    if not refs:
        raise RuntimeError("Kaggle returned no notebook references")

    destination.mkdir(parents=True, exist_ok=True)
    successful: list[Path] = []
    failures: list[str] = []
    for rank, ref in enumerate(refs, start=1):
        folder = folder_for_ref(destination, rank, ref)
        folder.mkdir(parents=True, exist_ok=True)
        completed = run(
            [kaggle, "kernels", "pull", ref, "-p", str(folder), "-m"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            successful.append(folder)
            continue
        failures.append(ref)
        detail = (completed.stderr or completed.stdout).strip()
        print(f"Failed to refresh {ref}: {detail[-1000:]}", file=sys.stderr)
    return successful, failures


def iter_notebooks(paths: Iterable[Path]) -> list[Path]:
    """Find notebook files in explicit files or recursively in directories."""

    notebooks: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path.is_file():
            candidates = [path] if path.suffix == ".ipynb" else []
        elif path.is_dir():
            candidates = sorted(candidate for candidate in path.rglob("*.ipynb") if candidate.is_file())
        else:
            raise FileNotFoundError(path)
        for notebook in candidates:
            resolved = notebook.resolve()
            if resolved not in seen:
                notebooks.append(notebook)
                seen.add(resolved)
    return notebooks


def convert_notebooks(notebooks: Iterable[Path]) -> list[tuple[Path, Path]]:
    """Write adjacent review scripts while retaining every source notebook."""

    converted: list[tuple[Path, Path]] = []
    for notebook_path in notebooks:
        with notebook_path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        output_path = notebook_path.with_suffix(".py")
        output_path.write_text(ipynb_to_py.convert(payload), encoding="utf-8")
        converted.append((notebook_path, output_path))
    return converted


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Existing .ipynb files or folders to convert recursively.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Download the current top-rated notebooks before converting them.",
    )
    parser.add_argument("--destination", type=Path, default=Path("notebooks"))
    parser.add_argument("--competition", default=DEFAULT_COMPETITION)
    parser.add_argument("--top-n", type=positive_int, default=DEFAULT_TOP_N)
    parser.add_argument(
        "--sort-by",
        dest="sort_orders",
        action="append",
        metavar="ORDER",
        help=(
            "Kaggle ranking to include; repeat to choose the union. Defaults to "
            "hotness, voteCount, and scoreDescending."
        ),
    )
    parser.add_argument("--kaggle", default="kaggle", help="Kaggle CLI executable.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not args.refresh and not args.paths:
        raise SystemExit("Provide one or more notebook paths, or pass --refresh.")
    if args.refresh and shutil.which(args.kaggle) is None:
        raise SystemExit(
            f"Kaggle CLI {args.kaggle!r} is not on PATH; install and authenticate it before refresh."
        )

    source_paths = list(args.paths)
    failures: list[str] = []
    if args.refresh:
        sort_orders = tuple(args.sort_orders or DEFAULT_SORT_ORDERS)
        refreshed, failures = refresh_notebooks(
            destination=args.destination,
            competition=args.competition,
            top_n=args.top_n,
            sort_orders=sort_orders,
            kaggle=args.kaggle,
        )
        source_paths.extend(refreshed)
        print(
            f"Refreshed {len(refreshed)} notebook source folders in {args.destination} "
            f"from the top {args.top_n} of: {', '.join(sort_orders)}"
        )

    notebooks = iter_notebooks(source_paths)
    if not notebooks:
        print("No .ipynb files found to convert.", file=sys.stderr)
        return 1
    for source, output in convert_notebooks(notebooks):
        print(f"Converted {source} -> {output}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
