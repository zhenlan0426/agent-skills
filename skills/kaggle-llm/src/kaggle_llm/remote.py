"""Remote batch backend: run a JSONL batch inside a private Kaggle benchmark task.

Kaggle-hosted task kernels can call the full model catalog, unlike local Model
Proxy tokens. One job is one push of a generated task file to a fixed private
slug; the task's creation run executes the job (remote_runner) and writes result
lines that are downloaded and post-processed locally exactly like `batch`.
See docs/remote-plan.md.
"""
import base64
import contextlib
import fcntl
import gzip
import inspect
import io
import json
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional

from . import best, remote_runner
from .auth import KaggleLLMError
from .client import _PreparedSchema, _prepare_schema, build_request, finish, resolve_model
from .dedup import near_duplicates

SLUG = "kaggle-llm-batch"  # kaggle-llm-runner broke server-side on 2026-09-27; see the plan log
RESULTS_NAME = "kaggle_llm_results.jsonl"
DEFAULT_DEADLINE = 39600     # placeholder; set from experiment E2
DEFAULT_WAIT_TIMEOUT = 3600  # CLI wait before detaching; the job keeps running on Kaggle
# Kaggle refuses a pushed notebook of 1 MB or more (E1). The encoded spec is
# nearly all of it; about 12 KB is runner code, template and notebook JSON.
MAX_SOURCE_BYTES = 1_000_000
MAX_PAYLOAD_BYTES = 950_000
SPEC_VERSION = 1
BACKOFF_SECONDS = 5.0
MAX_CONCURRENCY = 16
RESUME_OVERRIDES = ("max_cost_usd", "concurrency", "deadline_seconds")
LOG_LINES = 200


class TaskInfo(NamedTuple):
    version: int
    state: str               # "pending" | "completed" | "errored"
    error: Optional[str]
    is_public: bool


class RunInfo(NamedTuple):
    id: int
    model: str
    state: str               # "pending" | "completed" | "errored"


def prepare_job(rows, *, catalog, system=None, schema=None, schema_mode="prompt",
                model=None, max_tokens=None, temperature=None, reasoning=None,
                concurrency=4, max_attempts=4, max_cost_usd=None,
                deadline_seconds=DEFAULT_DEADLINE, dedup=True, threshold=0.85,
                execute_in="creation"):
    """Build a job spec from {"line","id","prompt"} rows. Returns (spec, dropped duplicates)."""
    if execute_in not in ("creation", "run"):
        raise ValueError("execute_in must be creation or run")
    if type(concurrency) is not int or not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ValueError(f"concurrency must be an integer from 1 to {MAX_CONCURRENCY}")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    if max_cost_usd is not None and not max_cost_usd > 0:
        raise ValueError("max_cost_usd must be positive")
    if not rows:
        raise ValueError("No input rows")
    options, prepared = build_request("x", system=system, schema=schema, schema_mode=schema_mode,
                                      max_tokens=max_tokens, temperature=temperature, reasoning=reasoning)[1:]

    if isinstance(model, str) and "/" in model:
        candidates = [resolve_model(model, {})]
    elif catalog is None:
        raise KaggleLLMError("Kaggle catalog unreachable; pass an exact --model")
    elif model is not None:
        candidates = [resolve_model(model, {"LLMS_AVAILABLE": ",".join(catalog)})]
    else:
        candidates = best.rank(catalog)
        if not candidates:
            raise KaggleLLMError("The Kaggle catalog has no Claude, OpenAI, or Gemini model; pass an exact --model")
    if execute_in == "run":
        candidates = candidates[:1]  # A run targets one model; no fallback inside it.

    dropped, skip = [], set()
    if dedup:
        for index, kept, similarity in near_duplicates([row["prompt"] for row in rows], threshold=threshold):
            skip.add(index)
            dropped.append({"line": rows[index]["line"], "id": rows[index].get("id"),
                            "duplicate_of_line": rows[kept]["line"], "similarity": similarity})
    spec_rows = [{"line": row["line"], "id": row.get("id"),
                  "messages": build_request(row["prompt"], system=system, schema=prepared,
                                            schema_mode=schema_mode)[0]}
                 for index, row in enumerate(rows) if index not in skip]
    spec = {
        "version": SPEC_VERSION, "job_id": uuid.uuid4().hex[:12], "candidates": candidates,
        "execute_in": execute_in, "rows": spec_rows, "options": options,
        "concurrency": concurrency, "max_attempts": max_attempts, "backoff_seconds": BACKOFF_SECONDS,
        "max_cost_usd": max_cost_usd, "deadline_seconds": deadline_seconds, "dry_run": None,
        "local": {"schema": prepared.schema if isinstance(prepared, _PreparedSchema) else None,
                  "schema_mode": schema_mode},
    }
    size = len(encode_spec(spec))
    if size > MAX_PAYLOAD_BYTES:
        raise KaggleLLMError(
            f"Job too large for one Kaggle task: the compressed prompts take {size:,} bytes; the limit is "
            f"{MAX_PAYLOAD_BYTES:,} (Kaggle rejects task notebooks of {MAX_SOURCE_BYTES:,} bytes or more). "
            "Split the input into smaller files.")
    return spec, dropped


def encode_spec(spec):
    """The base64 gzip payload embedded in the task file (without the local-only keys)."""
    data = json.dumps({k: v for k, v in spec.items() if k != "local"}, ensure_ascii=False).encode("utf-8")
    return base64.b64encode(gzip.compress(data, mtime=0)).decode("ascii")


def render_task(spec, *, slug=SLUG):
    """Percent-format task file that Kaggle converts to a notebook and runs on push."""
    runner = inspect.getsource(remote_runner).rstrip()
    return f'''# kaggle-llm remote batch job {spec["job_id"]}. Generated; do not edit.
# %%
{runner}


# %%
import base64
import gzip
import json
import os
from pathlib import Path

import kaggle_benchmarks as kbench

SPEC = json.loads(gzip.decompress(base64.b64decode("{encode_spec(spec)}")))


@kbench.task(name={json.dumps(slug)}, description="kaggle-llm remote batch")
def kaggle_llm_runner(llm) -> dict:
    return run_job(SPEC, globals().get("KAGGLE_LLM_CALL", proxy_call),
                   Path(os.environ.get(RESULTS_ENV, DEFAULT_RESULTS)))


# %%
kaggle_llm_runner.run(kbench.llm)
'''


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_atomic(path, text):
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        handle.write(text)
    os.replace(handle.name, path)


class JobStore:
    """Private per-job directories: spec.json, dropped.json, state.json, task.py, raw.jsonl."""

    def __init__(self, root=None):
        root = root or os.environ.get("KAGGLE_LLM_JOBS_DIR") or "~/.cache/kaggle-llm/jobs"
        self.root = Path(root).expanduser().absolute()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)  # mkdir's mode is masked by umask.

    def path(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{12}", job_id):
            raise KaggleLLMError(f"Invalid job id {job_id!r}")
        return self.root / job_id

    def create(self, spec, dropped, *, parent=None):
        job_id = spec["job_id"]
        directory = self.path(job_id)
        directory.mkdir(mode=0o700)
        _write_atomic(directory / "spec.json", json.dumps(spec, ensure_ascii=False))
        _write_atomic(directory / "dropped.json", json.dumps(dropped, ensure_ascii=False))
        self.save_state(job_id, {"status": "created", "parent": parent, "created_at": _now()})
        return job_id

    def _read(self, job_id, name):
        try:
            return json.loads((self.path(job_id) / name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise KaggleLLMError(f"Unknown job {job_id}; see kaggle-llm remote list") from None

    def spec(self, job_id):
        return self._read(job_id, "spec.json")

    def dropped(self, job_id):
        return self._read(job_id, "dropped.json")

    def load_state(self, job_id):
        return self._read(job_id, "state.json")

    def save_state(self, job_id, state):
        _write_atomic(self.path(job_id) / "state.json", json.dumps(state))

    def list(self):
        jobs = []
        for directory in self.root.iterdir():
            if (directory / "state.json").exists() and re.fullmatch(r"[0-9a-f]{12}", directory.name):
                state = self.load_state(directory.name)
                jobs.append({"job_id": directory.name, "status": state.get("status"),
                             "created_at": state.get("created_at"), "parent": state.get("parent"),
                             "rows": len(self.spec(directory.name)["rows"])})
        return sorted(jobs, key=lambda job: job["created_at"] or "")


def _fail(store, job_id, state, reason):
    state.update(status="failed", error=reason)
    store.save_state(job_id, state)


def submit(store, backend, job_id, *, slug=SLUG):
    """Push the job's task file. Refuses while another creation is pending; never waits."""
    state = store.load_state(job_id)
    if state["status"] != "created":
        raise KaggleLLMError(f"Job {job_id} is already {state['status']}; use kaggle-llm remote collect or resume")
    source = render_task(store.spec(job_id), slug=slug)
    lock_fd = os.open(store.root / "runner.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        info = backend.task(slug)
        if info is not None and info.state == "pending":
            running = [job["job_id"] for job in store.list() if job["status"] in ("submitted", "running")]
            raise KaggleLLMError(f"Another job is still running on Kaggle: {running[-1] if running else 'unknown'}. "
                                 "Kaggle accepts one push at a time; try again when it finishes.")
        known_runs = sorted(run.id for run in backend.runs(slug))
        (store.path(job_id) / "task.py").write_text(source, encoding="utf-8")
        version = backend.push(slug, source)
        state.update(status="submitted", version=version, known_runs=known_runs, submitted_at=_now())
        store.save_state(job_id, state)
    info = backend.task(slug)
    if info is None or info.is_public is not False:
        _fail(store, job_id, state, "task is not verifiably private")
        raise KaggleLLMError(f"Kaggle task {slug} is public or its visibility could not be verified; "
                             "prompts and results may be exposed. Check it on kaggle.com now.")


def _same_model(run_model, model):
    def norm(slug):
        return slug.split("/", 1)[-1].replace("@", "-")
    return norm(run_model) == norm(model)


def _read_lines(path):
    lines = []
    for text in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(text)
        except ValueError:
            continue  # A kernel killed mid-write can leave a partial last line.
        if isinstance(record, dict):
            lines.append(record)
    return lines


def _save_log(store, job_id, backend, slug, name):
    path = store.path(job_id) / name
    path.write_text("\n".join(backend.log(slug).splitlines()[:LOG_LINES]) + "\n", encoding="utf-8")
    return path


def _download(store, backend, job_id, state, run, slug):
    directory = store.path(job_id)
    target = directory / "download"
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir()
    backend.download(run.id, target)
    found = sorted(target.rglob(RESULTS_NAME))
    if not found:
        log = _save_log(store, job_id, backend, slug, "run.log")
        _fail(store, job_id, state, f"run {run.id} {run.state} without results")
        raise KaggleLLMError(f"Kaggle run {run.id} for job {job_id} ended ({run.state}) without results; see {log}")
    shutil.copyfile(found[0], directory / "raw.jsonl")
    job_line = next((line for line in _read_lines(directory / "raw.jsonl") if line.get("kind") == "job"), None)
    if job_line is None or job_line.get("job_id") != job_id:
        _fail(store, job_id, state, "downloaded results belong to another job")
        raise KaggleLLMError(f"Kaggle run {run.id} returned results for another job, not {job_id}")
    state.update(status="downloaded", run_id=run.id, run_state=run.state, downloaded_at=_now())
    store.save_state(job_id, state)
    return state


def wait(store, backend, job_id, *, timeout=3600, interval=10, sleep=None, clock=None, slug=SLUG):
    """Poll Kaggle until the job's run ends, then download its results. Returns the job state."""
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    state = store.load_state(job_id)
    if state["status"] == "downloaded":
        return state
    if state["status"] not in ("submitted", "running"):
        raise KaggleLLMError(f"Job {job_id} is {state['status']}; nothing to wait for")
    spec = store.spec(job_id)
    known = set(state["known_runs"])
    pinned = spec["candidates"][0] if spec["execute_in"] == "run" else None
    start, delay = clock(), min(5, interval)
    while True:
        new = [run for run in backend.runs(slug)
               if run.id not in known and (pinned is None or _same_model(run.model, pinned))]
        if new:
            run = min(new, key=lambda r: r.id)
            if (state["status"], state.get("run_id")) != ("running", run.id):
                state.update(status="running", run_id=run.id)
                store.save_state(job_id, state)
            if run.state != "pending":
                return _download(store, backend, job_id, state, run, slug)
        else:
            info = backend.task(slug)
            if info is not None and info.version == state["version"]:
                if info.state == "errored":
                    log = _save_log(store, job_id, backend, slug, "creation.log")
                    _fail(store, job_id, state, f"task creation failed: {info.error or 'unknown error'}")
                    raise KaggleLLMError(f"Kaggle task creation failed for job {job_id}: "
                                         f"{info.error or 'unknown error'}; see {log}")
                if pinned and info.state == "completed" and not state.get("run_requested"):
                    backend.run(slug, pinned)  # execute_in "run": the creation run skipped the job.
                    state["run_requested"] = True
                    store.save_state(job_id, state)
        elapsed = clock() - start
        if elapsed >= timeout:
            raise KaggleLLMError(f"Timed out waiting for job {job_id}; it continues on Kaggle. "
                                 f"Collect later with: kaggle-llm remote collect {job_id}")
        sleep(min(delay, timeout - elapsed))
        delay = min(delay * 2, interval)


def _own_rows(store, job_id):
    """This job's result rows (no lineage, no output dedup) and its kernel metadata."""
    state = store.load_state(job_id)
    if state["status"] != "downloaded":
        raise KaggleLLMError(f"Job {job_id} is {state['status']}, not downloaded; "
                             f"run kaggle-llm remote collect {job_id} to wait for it")
    spec = store.spec(job_id)
    local = spec.get("local") or {}
    prepared = _prepare_schema(local["schema"]) if local.get("schema") is not None else None
    lines = _read_lines(store.path(job_id) / "raw.jsonl")
    job = next((line for line in lines if line.get("kind") == "job"), {})
    end = next((line for line in lines if line.get("kind") == "end"), None)
    kernel_rows = {line.get("line"): line for line in lines if line.get("kind") == "row"}
    stopped = (end or {}).get("stopped_reason") or ("unknown" if end is None else None)
    rows, not_run = [], 0
    for row in sorted(spec["rows"], key=lambda r: r["line"]):
        base = {"line": row["line"], "id": row.get("id")}
        kernel = kernel_rows.get(row["line"])
        if kernel is None:
            not_run += 1
            rows.append({**base, "ok": False, "error": f"not run (stopped: {stopped or 'unknown'})"})
        elif kernel.get("ok"):
            try:
                envelope = finish(kernel.get("raw"), prepared)
            except KaggleLLMError as exc:
                rows.append({**base, "ok": False, "error": str(exc)})
                continue
            if envelope["model"] is None:
                envelope["model"] = job.get("model")
            rows.append({**base, "ok": True, "result": envelope})
        else:
            rows.append({**base, "ok": False,
                         "error": f"{kernel.get('error')} after {kernel.get('attempts')} attempts"})
    meta = {"model": job.get("model"), "cost_usd": (end or {}).get("cost_usd"),
            "stopped_reason": (end or {}).get("stopped_reason"), "rows_not_run": not_run}
    return rows, meta


def collect(store, job_id, *, dedup_outputs=True, threshold=0.85):
    """Result rows in the local `batch` format, merged with the job's parents (resume lineage)."""
    rows, _ = _own_rows(store, job_id)
    parent = store.load_state(job_id).get("parent")
    if parent:
        merged = {row["line"]: row for row in collect(store, parent, dedup_outputs=False)}
        for row in rows:
            if not merged.get(row["line"], {}).get("ok"):  # Keep the earliest success.
                merged[row["line"]] = row
        rows = [merged[line] for line in sorted(merged)]
    if dedup_outputs:
        ok = [row for row in rows if row["ok"]]
        for index, kept, _ in near_duplicates([row["result"]["text"] for row in ok], threshold=threshold):
            ok[index]["near_duplicate_of"] = ok[kept]["id"]
            ok[index]["near_duplicate_of_line"] = ok[kept]["line"]
    return rows


def resume(store, job_id, **overrides):
    """Create (without submitting) a child job holding the rows that failed or never ran."""
    unknown = set(overrides) - set(RESUME_OVERRIDES)
    if unknown:
        raise ValueError(f"Cannot override {', '.join(sorted(unknown))} on resume")
    spec = store.spec(job_id)
    state = store.load_state(job_id)
    if state["status"] == "downloaded":
        retry = {row["line"] for row in collect(store, job_id, dedup_outputs=False) if not row["ok"]}
    elif state["status"] == "failed" and not state.get("version"):
        retry = {row["line"] for row in spec["rows"]}  # Never reached Kaggle.
    else:
        raise KaggleLLMError(f"Job {job_id} is {state['status']}; collect it before resuming")
    if not retry:
        raise KaggleLLMError(f"Job {job_id} has no failed or unrun rows")
    child = {**spec, **overrides, "job_id": uuid.uuid4().hex[:12],
             "rows": [row for row in spec["rows"] if row["line"] in retry]}
    return store.create(child, [], parent=job_id)


def summary(store, job_id):
    """Job status from local state only (no Kaggle calls)."""
    state = store.load_state(job_id)
    result = {"job_id": job_id, "status": state["status"], "model": None, "cost_usd": None,
              "rows_ok": None, "rows_failed": None, "rows_not_run": None,
              "dropped_duplicates": len(store.dropped(job_id)), "stopped_reason": None}
    if state.get("error"):
        result["error"] = state["error"]
    if state["status"] == "downloaded":
        rows, meta = _own_rows(store, job_id)
        ok = sum(row["ok"] for row in rows)
        result.update(model=meta["model"], cost_usd=meta["cost_usd"], stopped_reason=meta["stopped_reason"],
                      rows_ok=ok, rows_not_run=meta["rows_not_run"],
                      rows_failed=len(rows) - ok - meta["rows_not_run"])
    return result


@contextlib.contextmanager
def _quiet():
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


def _state(enum_value):
    name = getattr(enum_value, "name", str(enum_value))
    if name.endswith(("QUEUED", "RUNNING", "SCORE_PENDING", "UNSPECIFIED")):
        return "pending"
    return "completed" if name.endswith("COMPLETED") else "errored"


class SdkBackend:
    """The only code touching the kaggle SDK (private helpers of kaggle>=2.2.2,<3)."""

    def __init__(self):
        try:
            # The Kaggle client authenticates on import and may print; keep stdout clean.
            with _quiet():
                from kaggle.api.kaggle_api_extended import KaggleApi
                self.api = KaggleApi()
                self.api.authenticate()
        except (Exception, SystemExit):
            raise KaggleLLMError("Kaggle login failed; check `kaggle` CLI authentication.") from None

    @contextlib.contextmanager
    def _client(self, action):
        try:
            with _quiet(), self.api.build_kaggle_client() as kaggle:
                yield kaggle
        except KaggleLLMError:
            raise
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            detail = f"HTTP {status}" if status else type(exc).__name__
            hint = " (benchmark task access denied for this account)" if status == 403 else ""
            raise KaggleLLMError(f"Kaggle {action} failed: {detail}{hint}", status=status) from None

    def push(self, slug, source):
        from kagglesdk.benchmarks.types.benchmark_tasks_api_service import ApiCreateBenchmarkTaskRequest
        notebook = self.api._convert_py_to_notebook(source)
        size = len(notebook.encode("utf-8"))
        if size >= MAX_SOURCE_BYTES:
            raise KaggleLLMError(f"Task notebook is {size:,} bytes; Kaggle requires under {MAX_SOURCE_BYTES:,}")
        with self._client("push") as kaggle:
            request = ApiCreateBenchmarkTaskRequest()
            request.slug = slug
            request.text = notebook
            response = kaggle.benchmarks.benchmark_tasks_api_client.create_benchmark_task(request)
        if getattr(response, "error", None):
            raise KaggleLLMError(f"Kaggle refused the task push: {response.error}")
        return response.slug.version_number

    def task(self, slug):
        from kagglesdk.benchmarks.types.benchmark_tasks_api_service import ApiGetBenchmarkTaskRequest
        # Not _get_benchmark_task(allow_not_found=True): it maps 403 to "missing" too.
        request = ApiGetBenchmarkTaskRequest()
        request.slug = self.api._make_task_slug(slug)
        try:
            with self._client("task lookup") as kaggle:
                info = kaggle.benchmarks.benchmark_tasks_api_client.get_benchmark_task(request)
        except KaggleLLMError as exc:
            if exc.status == 404:
                return None
            raise
        is_public = bool(info.is_public) or bool(getattr(info, "is_backing_notebook_published", False))
        return TaskInfo(info.slug.version_number, _state(info.creation_state),
                        info.creation_error_message or info.error or None, is_public)

    def runs(self, slug):
        with self._client("run listing") as kaggle:
            runs = self.api._fetch_task_runs(kaggle, slug)
        return [RunInfo(run.id, run.model_version_slug, _state(run.state)) for run in runs]

    def run(self, slug, model):
        with self._client("run scheduling"):
            self.api.benchmarks_tasks_run_cli(slug, model=[model])

    def download(self, run_id, dest):
        from kagglesdk.benchmarks.types.benchmark_tasks_api_service import ApiDownloadBenchmarkTaskRunOutputRequest
        dest = Path(dest)
        archive = dest / f"{run_id}.zip"
        with self._client("download") as kaggle:
            request = ApiDownloadBenchmarkTaskRunOutputRequest()
            request.run_id = run_id
            response = kaggle.benchmarks.benchmark_tasks_api_client.download_benchmark_task_run_output(request)
            self.api.download_file(response, str(archive), kaggle.http_client(), quiet=True)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(dest / str(run_id))
        archive.unlink()

    def log(self, slug):
        """Creation error plus the newest finished run's log (as `kaggle b t log` prints it)."""
        from kagglesdk.benchmarks.types.benchmark_tasks_api_service import ApiGetBenchmarkTaskRunLogsRequest
        info = self.task(slug)
        parts = [f"creation: {info.state if info else 'missing'}; error: {info.error if info else None}"]
        finished = [run for run in self.runs(slug) if run.state != "pending"]
        if finished:
            run = max(finished, key=lambda r: r.id)
            with self._client("log download") as kaggle:
                request = ApiGetBenchmarkTaskRunLogsRequest()
                request.run_id = run.id
                response = kaggle.benchmarks.benchmark_tasks_api_client.get_benchmark_task_run_logs(request)
                text = response.text
            try:
                entries = json.loads(text)
                text = "".join(e["data"] if isinstance(e, dict) and "data" in e else json.dumps(e) + "\n"
                               for e in entries)
            except (ValueError, TypeError):
                pass
            parts.append(f"newest finished run {run.id} ({run.model}, {run.state}):\n{text}")
        return "\n".join(parts)


def default_backend():
    return SdkBackend()
