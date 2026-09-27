"""Runs one kaggle-llm remote batch job INSIDE a Kaggle benchmark task kernel.

This whole file is embedded into every generated task file (see remote.render_task),
so it must stay self-contained: standard library only, no relative imports, no
dataclasses (the source is exec'd as __main__). It never writes environment
values, the proxy URL, or upstream error bodies to its output.
"""
import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

RESULTS_ENV = "KAGGLE_LLM_RESULTS"
DEFAULT_RESULTS = "/kaggle/working/kaggle_llm_results.jsonl"
PROBE_MESSAGES = [{"role": "user", "content": "Reply with OK."}]
PROBE_MAX_TOKENS = 256


class CallError(Exception):
    """A failed proxy call. status is the HTTP status, or None for a timeout or connection error."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _retryable(status):
    return status is None or status == 429 or status >= 500


def proxy_call(model, payload, *, environ=os.environ, timeout=600):
    """POST one Chat Completions request to the Model Proxy and return the parsed response."""
    base = environ["MODEL_PROXY_URL"].rstrip("/")
    for suffix in ("/openapi", "/genai"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    body = json.dumps({"model": model, "stream": False, **payload}).encode("utf-8")
    request = urllib.request.Request(
        base + "/openapi/chat/completions", data=body, method="POST",
        headers={"Authorization": "Bearer " + environ["MODEL_PROXY_API_KEY"],
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            text = response.read()
    except urllib.error.HTTPError as exc:
        exc.close()
        raise CallError(exc.code, f"HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        timed_out = isinstance(exc.reason, TimeoutError)
        raise CallError(None, "timeout" if timed_out else "connection error") from None
    except TimeoutError:
        raise CallError(None, "timeout") from None
    except (OSError, http.client.HTTPException):
        raise CallError(None, "connection error") from None
    try:
        return json.loads(text)
    except ValueError:
        raise CallError(status, "invalid JSON response") from None


def _cost_usd(raw):
    usage = raw.get("usage") if isinstance(raw, dict) else None
    cost = usage.get("cost") if isinstance(usage, dict) else None
    if not isinstance(cost, dict):
        return 0.0
    return sum(v for v in cost.values() if isinstance(v, (int, float)) and not isinstance(v, bool)) / 1e9


def run_job(spec, call, out_path, *, environ=os.environ, sleep=time.sleep, clock=time.monotonic):
    """Execute spec's rows with `call(model, payload)` and write JSON lines to out_path.

    Returns {"rows_ok", "rows_failed", "cost_usd"}: numbers only, because the
    return value becomes Kaggle's result.json.
    """
    start = clock()
    lock = threading.Lock()
    state = {"cost": 0.0, "ok": 0, "failed": 0}
    rows = spec["rows"]
    candidates = spec["candidates"]
    max_attempts = max(1, int(spec.get("max_attempts") or 1))
    backoff = float(spec.get("backoff_seconds") or 0)
    max_cost = spec.get("max_cost_usd")
    deadline = spec.get("deadline_seconds")

    with open(out_path, "a", encoding="utf-8") as out:
        def write(record):
            with lock:
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                out.flush()

        def job_line(model, probe, skipped=False):
            write({"kind": "job", "job_id": spec["job_id"], "model": model, "probe": probe,
                   "row_count": len(rows), "skipped": skipped})

        def end(stopped_reason):
            summary = {"rows_ok": state["ok"], "rows_failed": state["failed"], "cost_usd": state["cost"]}
            write({"kind": "end", "cost_usd": state["cost"], "rows_ok": state["ok"],
                   "rows_failed": state["failed"], "stopped_reason": stopped_reason})
            return summary

        def attempt_call(model, payload):
            """Return (raw, None, attempts) or (None, error with .status, attempts). Sleeps only between attempts."""
            for attempt in range(1, max_attempts + 1):
                try:
                    raw = call(model, payload)
                except Exception as exc:
                    # Match on .status, not the class: an exec'd copy of this file
                    # defines its own CallError, distinct from an injected caller's.
                    if not hasattr(exc, "status"):
                        raise
                    if not _retryable(exc.status) or attempt == max_attempts:
                        return None, exc, attempt
                    sleep(backoff * 2 ** (attempt - 1))
                    continue
                with lock:
                    state["cost"] += _cost_usd(raw)
                return raw, None, attempt

        if spec.get("execute_in") == "run" and environ.get("LLM_DEFAULT") != candidates[0]:
            job_line(None, [], skipped=True)
            return end("skipped")

        dry_run = spec.get("dry_run")
        if dry_run:
            job_line(None, [])
            limit = float(dry_run.get("sleep_seconds") or 0)
            beat = max(float(dry_run.get("heartbeat_seconds") or 60), 0.001)
            while True:
                elapsed = clock() - start
                write({"kind": "heartbeat", "elapsed": elapsed})
                if elapsed >= limit:
                    break
                sleep(min(beat, limit - elapsed))
            return end("dry_run")

        model, probe = None, []
        for candidate in candidates:
            raw, error, _ = attempt_call(candidate, {"messages": PROBE_MESSAGES, "max_tokens": PROBE_MAX_TOKENS})
            probe.append({"model": candidate, "status": 200 if error is None else error.status})
            if error is None:
                model = candidate
                break
        job_line(model, probe)
        if model is None:
            return end("no_model")

        def run_row(row):
            try:
                payload = {"messages": row["messages"], **spec.get("options", {})}
                try:
                    raw, error, attempts = attempt_call(model, payload)
                except Exception as exc:  # A bug or odd response must not lose the other rows.
                    raw, error, attempts = None, CallError(None, type(exc).__name__), 1
                base = {"kind": "row", "line": row["line"], "id": row.get("id")}
                if error is None:
                    with lock:
                        state["ok"] += 1
                    write({**base, "ok": True, "raw": raw, "attempts": attempts})
                else:
                    with lock:
                        state["failed"] += 1
                    write({**base, "ok": False, "status": error.status, "error": str(error),
                           "attempts": attempts})
            finally:
                slots.release()

        # A slot is taken before each dispatch, so the cost and deadline checks
        # happen immediately before the row starts, not when it is queued.
        concurrency = max(1, int(spec.get("concurrency") or 1))
        slots = threading.Semaphore(concurrency)
        stopped = None
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for row in rows:
                slots.acquire()
                with lock:
                    cost = state["cost"]
                if max_cost is not None and cost >= max_cost:
                    stopped = "max_cost"
                elif deadline is not None and clock() - start >= deadline:
                    stopped = "deadline"
                if stopped:
                    slots.release()
                    break
                pool.submit(run_row, row)
        return end(stopped)
