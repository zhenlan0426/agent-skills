"""Read-only Kaggle watcher. Transport and clocks are injectable for offline tests."""

from datetime import datetime, timezone
from decimal import InvalidOperation
import json
import math
from pathlib import Path
import re
import time
import uuid

from probe_runtime import decimal


PENDING = {"PENDING", "QUEUED", "RUNNING", "SUBMITTED"}
TERMINAL = {"COMPLETE", "ERROR", "CANCELLED", "CANCELED", "TIMEOUT", "TIMED_OUT"}


def timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    # Kaggle's submission date is UTC; some clients serialize it without a zone.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_mapping(mapping, codec):
    if not isinstance(mapping, dict):
        raise ValueError("Mapping must be a JSON object")
    for rule in mapping.get("error_rules", []):
        if rule["outcome"] not in {"exception", "missing", "format"}:
            raise ValueError("Error rules must map to exception, missing, or format")
        try:
            re.compile(rule["pattern"], re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"Invalid error pattern: {exc}") from exc
    valid = {f"valid:{i}" for i in range(codec.valid_outcomes)}
    for rule in mapping.get("valid_rules", []):
        if rule["outcome"] not in valid or decimal(rule["score_min"]) > decimal(rule["score_max"]):
            raise ValueError("Invalid valid-outcome score range")
    if codec.valid_outcomes > 1 and not mapping.get("valid_rules"):
        raise ValueError("Multiple valid outcomes require calibrated score ranges")
    field = mapping.get("duration_field")
    if field is not None and (not isinstance(field, str) or not field):
        raise ValueError("duration_field must name a calibrated API field measured in seconds")


def classify(row, mapping, codec):
    status = str(row.get("status", "")).upper()
    error = row.get("errorDescription") or ""
    if status == "ERROR":
        matches = [r["outcome"] for r in mapping.get("error_rules", [])
                   if re.search(r["pattern"], error, re.IGNORECASE)]
    elif status == "COMPLETE" and not error:
        try:
            score = decimal(row["publicScore"])
        except (KeyError, ValueError, InvalidOperation):
            raise ValueError("Completed submission has no finite public score") from None
        rules = mapping.get("valid_rules", [])
        if not rules and codec.valid_outcomes == 1:
            return "valid:0"
        matches = [r["outcome"] for r in rules
                   if decimal(r["score_min"]) <= score <= decimal(r["score_max"])]
    else:
        raise ValueError(f"Unrecognized terminal status/error: {status!r} / {error!r}")
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one outcome rule match; found {len(matches)}")
    return matches[0]


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


class KaggleSource:
    """Use the installed SDK's paginated list and full per-ref detail responses."""

    def __init__(self):
        from kaggle.api.kaggle_api_extended import KaggleApi
        self.api = KaggleApi()
        self.api.authenticate()

    def fetch(self, competition, descriptions, page_size, max_pages):
        from kagglesdk.competitions.types.competition_api_service import (
            ApiGetSubmissionRequest, ApiListSubmissionsRequest,
        )
        rows, seen, token = [], set(), ""
        exhausted = False
        with self.api.build_kaggle_client() as client:
            service = client.competitions.competition_api_client
            for page in range(max_pages):
                request = ApiListSubmissionsRequest()
                request.competition_name = competition
                request.page_size = page_size
                request.page_token = token
                # Use tokens when available; older responses expose page numbers.
                request.page = -1 if token else page
                response = service.list_submissions(request)
                batch = [item for item in (response.submissions or []) if item is not None]
                for item in batch:
                    if item.description in descriptions and item.ref not in seen:
                        seen.add(item.ref)
                        detail = ApiGetSubmissionRequest()
                        detail.ref = item.ref
                        full = service.get_submission(detail).to_dict(ignore_defaults=False)
                        if str(full["ref"]) != str(item.ref) or full["description"] != item.description:
                            raise ValueError("Kaggle submission detail does not match the listed ref/description")
                        rows.append(full)
                next_token = getattr(response, "next_page_token", "") or ""
                if not batch or (not next_token and len(batch) < page_size):
                    exhausted = True
                    break
                if next_token and next_token == token:
                    raise ValueError("Kaggle pagination returned a repeated token")
                token = next_token
        return {"submissions": rows, "pagination_exhausted": exhausted}


def update_records(state, raw_rows, probes, now, mapping, codec, pending_at=None):
    by_description = {p["description"]: p for p in probes}
    seen = set()
    for raw in raw_rows:
        description = raw.get("description")
        if description not in by_description:
            continue
        probe = by_description[description]
        index = str(probe["index"])
        if index in seen:
            raise ValueError(f"Multiple submissions have the same manifest description: {description}")
        seen.add(index)
        ref = str(raw["ref"])
        record = state["records"].setdefault(index, {"submission_ref": ref})
        if record["submission_ref"] != ref:
            raise ValueError(f"Submission ref changed for digit {index}; inspect duplicate submissions")
        if record.get("terminal"):
            continue  # Preserve the first terminal timestamp and elapsed time.
        status = str(raw.get("status", "")).upper()
        record["raw"] = raw
        record["last_seen_at"] = now.isoformat()
        if status in PENDING:
            record["last_pending_at"] = (pending_at or now).isoformat()
            continue
        if status not in TERMINAL:
            record["issue"] = f"Unknown status: {status!r}"
            continue
        record.update(terminal=True, first_terminal_at=now.isoformat())
        row = {"index": probe["index"], "description": description,
               "submission_ref": ref, "status": status,
               "error_description": raw.get("errorDescription", ""),
               "public_score": raw.get("publicScore"),
               "first_terminal_at": now.isoformat(), "outcome": None,
               "elapsed_seconds": None}
        if "kernel_version" in probe:
            row["kernel_version"] = probe["kernel_version"]
        issues = []
        try:
            row["outcome"] = classify(raw, mapping, codec)
        except ValueError as exc:
            issues.append(str(exc))
        field = mapping.get("duration_field")
        try:
            if field:
                elapsed = decimal(raw[field])
                if elapsed < 0:
                    raise ValueError("API duration is negative")
                row.update(elapsed_seconds=str(elapsed), timing_source=f"api:{field}")
            elif record.get("last_pending_at"):
                created = timestamp(raw["date"])
                pending = timestamp(record["last_pending_at"])
                if not created <= pending <= now:
                    raise ValueError("Submission timestamps are inconsistent")
                row.update(elapsed_seconds=str((now - created).total_seconds()),
                           elapsed_lower_seconds=str((pending - created).total_seconds()),
                           timing_source="submission-date-to-first-terminal-poll",
                           submission_created_at=created.isoformat(),
                           last_pending_at=pending.isoformat())
            else:
                issues.append("Already terminal on first observation; no trustworthy elapsed time")
        except (KeyError, ValueError, InvalidOperation) as exc:
            issues.append(f"Cannot establish elapsed time: {exc}")
        if issues:
            row["issues"] = issues
        record["observation"] = row


def watch_run(args, *, source=None, now=None, clock=time.monotonic, sleep=time.sleep):
    from probe import load_manifest
    manifest, codec = load_manifest(args.manifest)
    mapping = json.loads(Path(args.mapping).read_text())
    validate_mapping(mapping, codec)
    if (not math.isfinite(args.poll_seconds) or not 0 < args.poll_seconds <= 60
            or not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0
            or args.page_size < 1 or args.max_pages < 1):
        raise ValueError("Use poll interval in (0, 60], positive deadline, page size and page count")
    probes = manifest["probes"]
    if (len(probes) != codec.digits or {p["index"] for p in probes} != set(range(codec.digits))
            or len({p["description"] for p in probes}) != codec.digits):
        raise ValueError("Manifest must have unique indices and descriptions for every digit")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    snapshots = output / "snapshots"
    snapshots.mkdir(exist_ok=True)
    state_path = output / "state.json"
    identity = {"manifest": manifest, "mapping": mapping}
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if state["identity"] != identity:
            raise ValueError("Watch directory belongs to a different manifest or mapping")
    else:
        state = {"identity": identity, "records": {}}
    now = now or (lambda: datetime.now(timezone.utc))
    source = source or KaggleSource()
    deadline = clock() + args.timeout_seconds
    descriptions = {p["description"] for p in probes}
    while True:
        poll_started_at = now()
        snapshot = source.fetch(manifest["competition"], descriptions, args.page_size, args.max_pages)
        observed_at = now()
        # Persist the raw response before matching, timing, or classification.
        write_json(snapshots / f"{uuid.uuid4().hex}.json",
                   {"poll_started_at": poll_started_at.isoformat(),
                    "observed_at": observed_at.isoformat(), **snapshot})
        update_records(state, snapshot["submissions"], probes, observed_at, mapping, codec, poll_started_at)
        observations = [r["observation"] for r in state["records"].values() if "observation" in r]
        observations.sort(key=lambda r: r["index"])
        write_json(state_path, state)
        write_json(output / "observations.json", observations)
        terminal = {int(i) for i, r in state["records"].items() if r.get("terminal")}
        remaining = deadline - clock()
        if len(terminal) == codec.digits or remaining <= 0:
            break
        sleep(min(args.poll_seconds, remaining))
    return {"observations": str(output / "observations.json"),
            "terminal_count": len(terminal), "submission_count": codec.digits,
            "undecodable_indices": [r["index"] for r in observations if r.get("issues")],
            "missing_indices": sorted(set(range(codec.digits)) - terminal),
            "pagination_exhausted": snapshot["pagination_exhausted"]}
