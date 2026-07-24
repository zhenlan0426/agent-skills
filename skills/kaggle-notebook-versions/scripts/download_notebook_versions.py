#!/usr/bin/env python3
"""Download one or all exact historical source revisions of a public Kaggle notebook."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


API_BASE = "https://www.kaggle.com/api/i/kernels."
USER_AGENT = "kaggle-notebook-versions/1.0"


class KaggleRequestError(RuntimeError):
    """A Kaggle request failed or returned an unexpected response."""


def request(url: str, timeout: float) -> bytes:
    try:
        with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout) as response:
            return response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        suffix = f": {detail[:300]}" if detail else ""
        raise KaggleRequestError(f"HTTP {exc.code} for {url}{suffix}") from exc
    except URLError as exc:
        raise KaggleRequestError(f"Could not reach Kaggle: {exc.reason}") from exc


def get_json(service: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
    url = f"{API_BASE}{service}?{urlencode(params)}"
    try:
        value = json.loads(request(url, timeout))
    except json.JSONDecodeError as exc:
        raise KaggleRequestError(f"Kaggle returned non-JSON metadata from {service}") from exc
    if not isinstance(value, dict):
        raise KaggleRequestError(f"Kaggle returned unexpected metadata from {service}")
    return value


def parse_notebook(value: str) -> tuple[str, str, int | None]:
    """Return owner, slug, and an optional version hinted by a Kaggle URL."""
    if value.startswith(("https://", "http://")):
        parts = [part for part in urlparse(value).path.split("/") if part]
        try:
            code_index = parts.index("code")
            owner, slug = parts[code_index + 1 : code_index + 3]
        except (ValueError, IndexError) as exc:
            raise argparse.ArgumentTypeError(
                "URL must look like https://www.kaggle.com/code/OWNER/SLUG[/versions/N]"
            ) from exc
        try:
            version_index = parts.index("versions", code_index + 3)
        except ValueError:
            version = None
        else:
            try:
                version = int(parts[version_index + 1])
            except (IndexError, ValueError, TypeError) as exc:
                raise argparse.ArgumentTypeError("URL has /versions without a numeric version") from exc
        return owner, slug, version

    parts = [part for part in value.strip("/").split("/") if part]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("Notebook must be OWNER/SLUG or a Kaggle notebook URL")
    return parts[0], parts[1], None


def resolve_kernel_id(owner: str, slug: str, timeout: float) -> int:
    """Resolve the numeric kernel id from the always-present first revision."""
    model = get_json(
        "LegacyKernelsService/GetKernelViewModel",
        {
            "authorUserName": owner,
            "kernelSlug": slug,
            "versionNumber": 1,
            "kernelVersionId": 0,
            "tab": "code",
        },
        timeout,
    )
    try:
        return int(model["kernel"]["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise KaggleRequestError("Could not find a numeric kernel id in Kaggle metadata") from exc


def list_versions(kernel_id: int, timeout: float) -> list[dict[str, Any]]:
    payload = get_json(
        "KernelsService/ListKernelVersions",
        {"kernelId": kernel_id, "sortOption": "VERSION_ID", "pageSize": 1000},
        timeout,
    )
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise KaggleRequestError("Kaggle returned no notebook versions")
    return items


def normalized_versions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    versions: list[dict[str, Any]] = []
    for item in items:
        version = item.get("version", {})
        run = item.get("run", {})
        try:
            version_number = int(version["versionNumber"])
            session_id = int(run["id"])
        except (KeyError, TypeError, ValueError):
            continue
        versions.append(
            {
                "version_number": version_number,
                "kernel_session_id": session_id,
                "created_at": run.get("dateCreated"),
                "evaluated_at": run.get("dateEvaluated"),
                "status": run.get("status"),
                "title": run.get("title"),
            }
        )
    if not versions:
        raise KaggleRequestError("No downloadable version/session pairs were returned")
    return sorted(versions, key=lambda version: version["version_number"])


def print_versions(versions: list[dict[str, Any]]) -> None:
    print("version  session_id  evaluated_at                 status")
    for version in versions:
        print(
            f"{version['version_number']:>7}  {version['kernel_session_id']:>10}  "
            f"{str(version['evaluated_at'] or ''):<27}  {version['status'] or ''}"
        )


def download_source(session_id: int, include_output: bool, timeout: float, retries: int) -> bytes:
    params: dict[str, Any] = {"kernelSessionId": session_id}
    if include_output:
        params["includeOutputIfAvailable"] = "true"
    url = f"{API_BASE}KernelsService/GetKernelSessionSource?{urlencode(params)}"
    for attempt in range(retries + 1):
        try:
            source = request(url, timeout)
            if not source.strip():
                raise KaggleRequestError(f"Kaggle returned an empty source for session {session_id}")
            return source
        except KaggleRequestError:
            if attempt == retries:
                raise
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


def write_source(path: Path, source: bytes, overwrite: bool) -> tuple[bool, str]:
    digest = hashlib.sha256(source).hexdigest()
    if path.exists() and not overwrite:
        existing = hashlib.sha256(path.read_bytes()).hexdigest()
        if existing == digest:
            return False, digest
        raise FileExistsError(f"Refusing to replace different existing file: {path} (pass --overwrite)")
    path.write_bytes(source)
    return True, digest


def write_manifest(
    path: Path,
    owner: str,
    slug: str,
    kernel_id: int,
    include_output: bool,
    selected_count: int,
    downloaded_count: int,
    versions: list[dict[str, Any]],
    complete: bool,
) -> None:
    manifest = {
        "notebook": f"{owner}/{slug}",
        "kernel_id": kernel_id,
        "updated_at": datetime.now(UTC).isoformat(),
        "complete": complete,
        "include_output": include_output,
        "requested_source_count": selected_count,
        "downloaded_source_count": downloaded_count,
        "unavailable_source_count": len(versions) - downloaded_count,
        "versions": versions,
    }
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("notebook", type=parse_notebook, help="OWNER/SLUG or https://www.kaggle.com/code/OWNER/SLUG")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--version", type=int, help="Download one Kaggle version number")
    action.add_argument("--all", action="store_true", help="Download every listed version")
    action.add_argument("--list", action="store_true", help="List versions without downloading")
    parser.add_argument("--kernel-id", type=int, help="Skip notebook metadata lookup with this numeric Kaggle kernel id")
    parser.add_argument("--output", type=Path, help="Output directory (default: ./OWNER-SLUG-versions)")
    parser.add_argument("--include-output", action="store_true", help="Ask Kaggle to include available cell outputs")
    parser.add_argument("--overwrite", action="store_true", help="Replace downloaded files when their content differs")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds (default: 30)")
    parser.add_argument("--retries", type=int, default=0, help="Retries per source request after a transient failure (default: 0)")
    parser.add_argument(
        "--bulk-backoff",
        type=float,
        default=30.0,
        help="In --all mode, wait once after the first source failure before resuming (default: 30 seconds)",
    )
    args = parser.parse_args()
    owner, slug, hinted_version = args.notebook
    args.owner, args.slug, args.hinted_version = owner, slug, hinted_version
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.retries < 0:
        parser.error("--retries cannot be negative")
    if args.bulk_backoff < 0:
        parser.error("--bulk-backoff cannot be negative")
    if args.version is not None and args.version <= 0:
        parser.error("--version must be positive")
    if args.version is not None and hinted_version is not None and args.version != hinted_version:
        parser.error("--version conflicts with the version embedded in the notebook URL")
    if args.version is None and not args.all and not args.list:
        if hinted_version is None:
            parser.error("choose --list, --version N, or --all")
        args.version = hinted_version
    return args


def main() -> int:
    args = parse_args()
    try:
        kernel_id = args.kernel_id or resolve_kernel_id(args.owner, args.slug, args.timeout)
        versions = normalized_versions(list_versions(kernel_id, args.timeout))
        if args.list:
            print_versions(versions)
            return 0

        selected = versions if args.all else [version for version in versions if version["version_number"] == args.version]
        if not selected:
            available = f"{versions[0]['version_number']}..{versions[-1]['version_number']}"
            raise KaggleRequestError(f"Version {args.version} is unavailable (listed range: {available})")

        output = args.output or Path.cwd() / f"{args.owner}-{args.slug}-versions"
        output.mkdir(parents=True, exist_ok=True)
        manifest_path = output / "manifest.json"
        manifest_versions: list[dict[str, Any]] = []
        downloaded_count = 0
        bulk_backoff_used = False
        for index, version in enumerate(selected, start=1):
            source_path = output / f"v{version['version_number']:04d}.ipynb"
            try:
                source = download_source(
                    version["kernel_session_id"], args.include_output, args.timeout, args.retries
                )
            except KaggleRequestError as exc:
                if not args.all:
                    raise
                if not bulk_backoff_used and args.bulk_backoff:
                    bulk_backoff_used = True
                    print(
                        f"[{index}/{len(selected)}] transient failure for v{version['version_number']}; "
                        f"waiting {args.bulk_backoff:g}s before retrying bulk download",
                        file=sys.stderr,
                    )
                    time.sleep(args.bulk_backoff)
                    try:
                        source = download_source(
                            version["kernel_session_id"], args.include_output, args.timeout, args.retries
                        )
                    except KaggleRequestError as retry_exc:
                        exc = retry_exc
                    else:
                        exc = None
                if exc is not None:
                    manifest_versions.append({**version, "download_error": str(exc)})
                    print(f"[{index}/{len(selected)}] unavailable v{version['version_number']}: {exc}", file=sys.stderr)
                    write_manifest(
                        manifest_path,
                        args.owner,
                        args.slug,
                        kernel_id,
                        args.include_output,
                        len(selected),
                        downloaded_count,
                        manifest_versions,
                        complete=False,
                    )
                    continue
            wrote, digest = write_source(source_path, source, args.overwrite)
            record = {**version, "source_file": source_path.name, "sha256": digest}
            manifest_versions.append(record)
            downloaded_count += 1
            verb = "downloaded" if wrote else "verified"
            print(f"[{index}/{len(selected)}] {verb} v{version['version_number']} -> {source_path}")
            if args.all:
                write_manifest(
                    manifest_path,
                    args.owner,
                    args.slug,
                    kernel_id,
                    args.include_output,
                    len(selected),
                    downloaded_count,
                    manifest_versions,
                    complete=False,
                )

        if args.all:
            write_manifest(
                manifest_path,
                args.owner,
                args.slug,
                kernel_id,
                args.include_output,
                len(selected),
                downloaded_count,
                manifest_versions,
                complete=True,
            )
            print(f"wrote {manifest_path}")
        return 0
    except (KaggleRequestError, FileExistsError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
