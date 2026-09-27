# Plan: remote batch backend (`kaggle-llm batch --remote`)

Handoff for an implementing agent. Read this whole file, then `SKILL.md`,
`references/api.md`, and `src/kaggle_llm/` before writing code. The executable
contract is `tests/test_remote.py`; it is skipped until the new modules exist.

## 1. Goal and scope

The user generates **training data for fine-tuning local models** with the best
available LLMs. Local Model Proxy tokens are restricted to a small curated model
set (Claude Sonnet 5 is currently the best local model). Kaggle-hosted task
kernels can call the **full catalog** (GPT-6 Astra, GPT-5.x, Claude Opus 5, ...).
This backend runs a batch of prompts inside a private Kaggle benchmark task and
returns results locally in the same row format as `kaggle-llm batch`.

In scope (confirmed with the user):
- Single-turn answers to user-supplied prompts (existing JSONL: `{"id", "prompt"}`).
- Under 1k rows per dataset, so one Kaggle job per dataset. No chunking or
  dataset attachment is needed (unless experiment E1 shows the payload limit is
  under 1k typical prompts).
- Quality: schema/format checks (existing local validation) and dedup/diversity
  (drop near-duplicate prompts before sending; flag near-duplicate responses).
- Output: raw result rows (same as local `batch`). Export to trainer formats is
  explicitly deferred.
- Batch latency (about 75 s overhead per job) is acceptable.

Out of scope: prompt generation, multi-turn, reasoning-trace formats, LLM-judge
filtering, multi-model mixing, export formats, parallel jobs.

## 2. Verified facts (2026-09-27, live tests on the user's account)

Test task: `zhenlanwang/kaggle-llm-runner`, version 1, private. Source is in
`docs/remote-probe-task.py`.

| Fact | Evidence |
|---|---|
| `kaggle b t push <slug> -f file.py` converts percent-format `.py` to a notebook and **creation executes the whole notebook once** on the default model (`gemini-3.7-flash`). | `status` showed a completed `gemini-3.7-flash` run right after push, with no `run` command. |
| Inside the kernel, `LLMS_AVAILABLE` lists **all 42 catalog models**, and `MODEL_PROXY_URL` and `MODEL_PROXY_API_KEY` are set. | Runner wrote the env to its results file. |
| The kernel token can call any catalog model directly, even from the creation run. | `load_model("openai/gpt-6-astra").prompt(...)` returned "OK" from both the Gemini creation run and the Opus run. |
| `kaggle b t run <slug> -m <model>` also works and sets `LLM_DEFAULT` to that model. | Opus 5 run: `llm_default = anthropic/claude-opus-5@default`. |
| Timing: push to creation-run start 51 s, run 4.5 s, completion visible about 20 s later (77 s total). `run -m`: 48 s queue, 5 s run, about 22 s to report (75 s). `download`: 2 s. | Timestamps in `status` output. |
| Per-call latency in the kernel: Opus 5 about 1.2 s, GPT-6 Astra 1.2 to 2.3 s for "Reply with OK.". | Results file. |
| `download` returns files the notebook writes to `/kaggle/working/` (our `kaggle_llm_results.jsonl`) plus Kaggle's `*.run.json`, `*.result.json`, `*.atif.json`, `*.task.json`. | Downloaded tree: `out/<slug>/<version>/<model>/<run_id>/`. |
| Kaggle's `run.json` stores **full conversations** (prompts and responses) for calls made through kbench. `result.json` includes `cost_usd`. | Inspected files. |
| **Tasks cannot be deleted** (`kaggle b t delete` prints "not supported by the server yet"). Tasks are private until `publish`. `task.isPublic` is exposed. | CLI source and `status` show `Public: False`. |
| 429 "model is currently experiencing heavy load" occurs even for 2 tiny calls (`gemini-3.7-flash`). | Results file. |
| Proxy responses include `usage.cost.{input,output}_tokens_cost_nanodollars`. | Local call envelope. |
| Run objects (`list_benchmark_task_runs`) have `id`, `modelVersionSlug`, `state`, `startTime`, and `endTime`, but **no task version**. Task info has `slug.versionNumber`, `creationState`, `isPublic`, `sourceKernelId`. | SDK `to_dict()`. |
| Creation states: QUEUED=1, RUNNING=2, COMPLETED=3, ERRORED=4, KERNEL_WITHOUT_RUN=5, VALIDATION_FAILED=6, NO_MODEL_SPECIFIED=7. Run states: QUEUED, RUNNING, COMPLETED, ERRORED, SCORE_PENDING. | `kagglesdk/benchmarks/types/benchmark_enums.py`. |
| Push is refused while the previous version's creation is QUEUED or RUNNING. | `benchmarks_tasks_push_cli` raises "creation is still pending". |
| Kaggle CLI validates that the file has a `@<x>.task(name=...)` decorator whose slugified name matches the pushed slug (AST check). | `_validate_task_in_file`. |

## 3. Design

### 3.1 Execution model

One job = **one push** of a generated task file to the fixed private slug
`kaggle-llm-runner`. The creation run executes the job: it ignores the default
`llm` and calls the target model itself over the proxy. There is no separate `run -m`,
so each prompt is sent exactly once.

Fallback (implemented but not the default): `execute_in: "run"`. The runner does
nothing unless `LLM_DEFAULT` equals the pinned model; after creation, the
orchestrator calls `run -m <model>`. Use this only if Kaggle stops giving creation
runs full-catalog access. Detection: every candidate returns 403/404 in the
creation-run probe. Switching can stay manual (`--execute-in run`). Do not build
automatic switching unless it is cheap.

### 3.2 Components and files

```
src/kaggle_llm/
  client.py          # refactor: build_request() + finish() shared by local and remote
  dedup.py           # new: normalize(), near_duplicates()
  remote_runner.py   # new: stdlib-only code that runs INSIDE the Kaggle kernel
  remote.py          # new: prepare/render/submit/wait/collect/resume, JobStore, SdkBackend
  cli.py             # new flags and `remote` subcommands
docs/remote-plan.md        # this file; append the experiment log (section 6)
docs/remote-probe-task.py  # reference task from the feasibility test
tests/test_remote.py       # executable contract
```

### 3.3 `client.py` refactor (no behavior change for local calls)

- `build_request(prompt, *, system=None, schema=None, schema_mode="prompt", max_tokens=None, temperature=None, reasoning=None) -> (messages, options, prepared)`
  - `messages`: the exact list `Client.prompt` sends. The schema instruction is
    appended to the prompt exactly as today.
  - `options`: Chat Completions **payload** keys, including only those set:
    `max_tokens`, `temperature`, `reasoning_effort`, and `response_format` (native
    schema mode).
  - `prepared`: a `_PreparedSchema` or `None`.
  - Performs today's argument validation and raises `ValueError`.
- `finish(raw, prepared) -> envelope`: the post-processing now inside
  `Client.prompt` (refusal and filter, `length` truncation, `<think>` strip,
  missing text, schema parse and validation). It raises `KaggleLLMError` with the
  same messages.
- `Client.prompt` = `build_request` → `chat` → `finish`. All existing tests must
  still pass unchanged.

### 3.4 `dedup.py`

- `normalize(text)`: casefold, replace punctuation with spaces, collapse whitespace, strip.
- `near_duplicates(texts, *, threshold=0.85, shingle=5) -> list[(index, kept_index, similarity)]`
  - Items are compared in order against the items kept so far. A later item
    whose word-shingle Jaccard similarity with a kept item is at least
    `threshold` is reported against the **first** such kept item and is not
    itself kept.
  - Identical normalized text always has similarity 1.0.
  - Texts with fewer than `shingle` words match only on exact normalized equality.
  - O(n²) is fine for n ≤ 1000. Use the standard library only.

### 3.5 `remote_runner.py` (runs in Kaggle; **stdlib only**, Python ≥ 3.10)

Its full source is embedded into every task file, so it must be self-contained:
no imports from `kaggle_llm`, and no third-party imports.

```python
RESULTS_ENV = "KAGGLE_LLM_RESULTS"
DEFAULT_RESULTS = "/kaggle/working/kaggle_llm_results.jsonl"
PROBE_MESSAGES = [{"role": "user", "content": "Reply with OK."}]

class CallError(Exception):          # .status: int | None (None = timeout/connection)
    def __init__(self, status, message): ...

def proxy_call(model, payload, *, environ=os.environ, timeout=600) -> dict
def run_job(spec, call, out_path, *, environ=os.environ, sleep=time.sleep, clock=time.monotonic) -> dict
```

`proxy_call`:
- POST JSON `{"model": model, "stream": false, **payload}` to
  `<MODEL_PROXY_URL without trailing /, /openapi or /genai>/openapi/chat/completions`
  with `Authorization: Bearer <MODEL_PROXY_API_KEY>`, using `urllib.request`.
- Returns the parsed dict.
- Raises `CallError(http_status, "HTTP <status>")` on HTTP errors and
  `CallError(None, "timeout"|"connection error")` on network errors.
- Never puts the key, URL, or response body in messages.

Spec (JSON, `version: 1`):
```json
{"version": 1, "job_id": "a1b2c3d4e5f6", "candidates": ["openai/gpt-6-astra", "..."],
 "execute_in": "creation", "rows": [{"line": 1, "id": "a", "messages": [...]}],
 "options": {"max_tokens": 2048}, "concurrency": 4, "max_attempts": 4,
 "backoff_seconds": 5.0, "max_cost_usd": null, "deadline_seconds": 39600,
 "dry_run": null}
```

`run_job` behavior. It writes JSON lines to `out_path`, appending and flushing
each line under a lock:
1. `{"kind":"job","job_id","model","probe":[{"model","status"}],"row_count","skipped"}` is written once, after the probe.
2. **Skip rule.** If `execute_in == "run"` and `environ["LLM_DEFAULT"] != candidates[0]`,
   write the job line (`skipped: true`, `model: null`) and
   `end{stopped_reason:"skipped"}`, make **no calls**, and return.
3. **Dry run.** If `dry_run`, make no model calls. Write heartbeat lines
   `{"kind":"heartbeat","elapsed"}` every `heartbeat_seconds` until
   `sleep_seconds`, then write `end{stopped_reason:"dry_run"}`. The job line has
   `model: null` and the correct `row_count`. (This is used by experiments E1/E2.)
4. **Probe.** Try candidates in order with `PROBE_MESSAGES` and `max_tokens: 256`.
   - Success: pin that model and record probe status 200.
   - Retryable failure (status 429, ≥ 500, or `None`): retry up to
     `max_attempts` with backoff, then move to the next candidate.
   - Any other failure: move on immediately.
   - Record the last status per candidate tried.
   - If nothing answers, write a job line with `model: null` and
     `end{stopped_reason:"no_model"}`.
   - Probe cost counts toward the running cost.
5. **Rows.** Use a thread pool of `concurrency`. Before **dispatching** each row,
   stop if running cost ≥ `max_cost_usd` (`stopped_reason:"max_cost"`) or if
   elapsed ≥ `deadline_seconds` (`"deadline"`). In-flight rows finish.
   Rows never dispatched get **no line**. Per row:
   - retry retryable failures up to `max_attempts`
     (`sleep(backoff_seconds * 2**(attempt-1))`)
   - fail immediately on other statuses
   - write `{"kind":"row","line","id","ok":true,"raw":<completion>,"attempts"}` or
     `{"kind":"row","line","id","ok":false,"status","error","attempts"}`
6. **Cost.** Sum all numeric values under `raw["usage"]["cost"]` (nanodollars)
   ÷ 1e9.
7. Write `{"kind":"end","cost_usd","rows_ok","rows_failed","stopped_reason"}`
   (`null` when all rows were dispatched).
   Return `{"rows_ok", "rows_failed", "cost_usd"}`. Numbers only: the value
   becomes Kaggle's `result.json`.
8. Never write any `environ` value to the output.

### 3.6 `remote.py`

```python
SLUG = "kaggle-llm-runner"
RESULTS_NAME = "kaggle_llm_results.jsonl"
DEFAULT_DEADLINE = 39600     # placeholder; set from experiment E2

class TaskInfo(NamedTuple):  version: int; state: str; error: str | None; is_public: bool
class RunInfo(NamedTuple):   id: int; model: str; state: str
# state values: "pending" | "completed" | "errored"

class SdkBackend:            # real Kaggle calls; the only code touching the kaggle SDK
    def push(self, slug, source) -> int            # returns new version number
    def task(self, slug) -> TaskInfo | None        # None if the task does not exist
    def runs(self, slug) -> list[RunInfo]
    def download(self, run_id, dest: Path) -> None # extracts the run's zip into dest
    def log(self, slug) -> str
def default_backend() -> SdkBackend

class JobStore:
    def __init__(self, root=None)   # default $KAGGLE_LLM_JOBS_DIR or ~/.cache/kaggle-llm/jobs (0700)
    def create(self, spec, dropped, *, parent=None) -> str   # job_id = spec["job_id"]
    def path(self, job_id) -> Path
    def spec(self, job_id) -> dict
    def load_state(self, job_id) -> dict
    def save_state(self, job_id, state) -> None               # atomic write
    def list(self) -> list[dict]

def prepare_job(rows, *, catalog, system=None, schema=None, schema_mode="prompt",
                model=None, max_tokens=None, temperature=None, reasoning=None,
                concurrency=4, max_attempts=4, max_cost_usd=None,
                deadline_seconds=DEFAULT_DEADLINE, dedup=True, threshold=0.85,
                execute_in="creation") -> tuple[dict, list[dict]]
def render_task(spec, *, slug=SLUG) -> str
def submit(store, backend, job_id, *, slug=SLUG) -> None
def wait(store, backend, job_id, *, timeout=3600, interval=10, sleep=time.sleep, clock=time.monotonic) -> dict
def collect(store, job_id, *, dedup_outputs=True, threshold=0.85) -> list[dict]
def resume(store, job_id) -> str
def summary(store, job_id) -> dict
```

**`prepare_job`**
- `rows` are `{"line","id","prompt"}`.
- Messages and options come from `client.build_request`.
- `candidates`:
  - `[model]` if `model` contains `/`
  - a bare alias resolved against `catalog` via `resolve_model`
  - otherwise `best.rank(catalog)`
  - if `catalog is None` and no exact model: raise
    `KaggleLLMError("Kaggle catalog unreachable; pass an exact --model")`.
- With `dedup`, run `near_duplicates` over prompts. The spec keeps first
  occurrences; `dropped` lists `{"line","id","duplicate_of_line","similarity"}`.
- `spec["local"] = {"schema": schema, "schema_mode": schema_mode}` (for `collect`).
- `job_id = uuid4().hex[:12]`.

**`render_task`**
- Output is a percent-format `.py` (`# %%` cells):
  - `inspect.getsource(remote_runner)`
  - `SPEC = json.loads(gzip.decompress(base64.b64decode("...")))` (the spec
    **without** `"local"`)
  - `@kbench.task(name="<slug>", description="kaggle-llm remote batch")`
    `def kaggle_llm_runner(llm) -> dict:`
    `return run_job(SPEC, globals().get("KAGGLE_LLM_CALL", proxy_call), Path(os.environ.get(RESULTS_ENV, DEFAULT_RESULTS)))`
  - `kaggle_llm_runner.run(kbench.llm)`
- `KAGGLE_LLM_CALL` exists only so tests can inject a fake. It is never set in Kaggle.

**`submit`**
1. Take an `fcntl` lock on `<jobs root>/runner.lock` for the check and push only.
2. If `backend.task(slug)` is pending, raise
   `KaggleLLMError("Another job is still running on Kaggle: <job_id or 'unknown'>")`.
   Find that job via `JobStore.list()` status `submitted`/`running`. Do not wait.
3. Record `known_runs = {r.id for r in backend.runs(slug)}`.
4. Write `task.py` to the job dir and call `version = backend.push(slug, source)`.
5. Save state `{"status":"submitted","version","known_runs","submitted_at"}`.
6. Then assert `backend.task(slug).is_public is False`. If public, mark the job
   `failed` and raise; it must never happen silently.

**`wait`**
- Poll until the job reaches a terminal state or `timeout` (then raise
  `KaggleLLMError` naming the job id; the state stays resumable):
  - Task creation errored for our version and no new run exists: save the first
    200 lines of `backend.log(slug)` to `creation.log` in the job dir, mark it
    `failed`, and raise with a pointer to that file.
  - The new run is the one whose id is not in `known_runs`. For `execute_in: run`,
    the one whose `model` matches the pinned model.
  - When it is `completed` or `errored`:
    1. `download` into `<job>/download/`.
    2. Find `RESULTS_NAME` recursively and copy it to `<job>/raw.jsonl`.
    3. Verify the job line's `job_id` matches. On mismatch, mark `failed` and raise.
    4. Set status `downloaded`.
  - Also download an errored run's partial results.
- `interval` is the maximum poll sleep. Start at 5 s and grow to `interval`.

**`collect`**
- For each spec row, in `line` order:
  - `ok` raw → `client.finish(raw, prepared)`. `prepared` is rebuilt from
    `spec["local"]`, and `finish` errors become failed rows.
  - Failed kernel row → `{"ok": false, "error": "<error> after <n> attempts"}`.
  - No line → `{"ok": false, "error": "not run (stopped: <stopped_reason>)"}`.
- Output rows match local `batch`:
  `{"line","id","ok":true,"result":<envelope>}` or `{"line","id","ok":false,"error"}`.
- With `dedup_outputs`, successful rows whose `result.text` is a near-duplicate
  of an earlier successful row get `"near_duplicate_of": <earlier id>`. Rows are
  flagged, never dropped.
- **Lineage.** If the job has a `parent`, collect the parent recursively and
  overlay this job's rows by `line`, preferring `ok` rows.

**`resume`**
- Creates a child job, with `parent = job_id`, containing only rows that failed
  or never ran, with the same candidates and options.
- Line numbers are preserved.
- Returns the new `job_id` without submitting it.

**`summary`**
- `{"job_id","status","model","cost_usd","rows_ok","rows_failed","rows_not_run","dropped_duplicates","stopped_reason"}`.

**`SdkBackend`**
- Wrap the private helpers of `kaggle>=2.2.2,<3`, all in this one class:
  - `KaggleApi._convert_py_to_notebook`
  - `_get_benchmark_task(slug, kaggle, allow_not_found=True)`
  - `_fetch_task_runs(kaggle, slug)`
  - `benchmark_tasks_api_client.create_benchmark_task(ApiCreateBenchmarkTaskRequest(slug=..., text=notebook))`
  - `download_benchmark_task_run_output(ApiDownloadBenchmarkTaskRunOutputRequest(run_id=...))`
    plus `api.download_file(response, zip, kaggle.http_client(), quiet=True)`, then unzip.
  - Use `benchmarks_tasks_log_cli`'s underlying call for `log`.
- Map enum names by suffix:
  - QUEUED, RUNNING, SCORE_PENDING → `pending`
  - COMPLETED → `completed`
  - anything else → `errored`
- Import the SDK the same way `best.fetch_catalog` does: redirect stdout and
  stderr, because the kaggle package authenticates on import.

### 3.7 CLI

```
kaggle-llm batch INPUT --remote [--model M] [--system S] [--schema F] [--schema-mode prompt|native]
    [--max-tokens N] [--temperature T] [--reasoning R] [--concurrency 4] [--max-cost USD]
    [--no-dedup] [--detach] [--wait-timeout 3600] [--execute-in creation|run]
kaggle-llm remote status JOB      # summary JSON on stdout
kaggle-llm remote collect JOB [--wait-timeout S]   # waits if needed, then result rows on stdout
kaggle-llm remote resume JOB [--detach] [--wait-timeout S]
kaggle-llm remote list
```
- Input rows are validated exactly like local `batch`: object with `prompt` and
  optional `id`, non-boolean `id`. Invalid rows are usage errors before anything
  is sent (exit 2). This differs from local batch, which emits error rows, because
  a remote job is all-or-nothing to submit.
- `--remote` without `--model` uses `best.fetch_catalog()` for the candidates.
- `--detach` prints `{"job_id": ...}` on stdout and exits 0 after the push.
- Without `--detach`: `wait` → `collect` → emit rows on stdout. A summary JSON
  goes to stderr, with dropped duplicates listed on stderr. Exit 0 if every row is
  ok, otherwise 1.
- On Ctrl-C during the wait, print to stderr
  `job <id> continues on Kaggle; collect with: kaggle-llm remote collect <id>` and exit 130.
- Numeric flags are validated like the existing ones: `--concurrency` 1–16,
  `--max-cost` > 0.

### 3.8 Details the tests depend on
- `remote_runner`:
  - Call `urllib.request.urlopen` through the module attribute, not
    `from urllib.request import urlopen`, so tests can patch it.
  - No `dataclasses`, no relative imports, stdlib only. The source is `exec`'d
    as `__main__`.
  - `run_job` reads `clock()` once at start. The deadline is measured from there,
    and the cost and deadline checks happen immediately before each row starts.
  - Sleep only **between** attempts, never after the last one.
- Remote code must look up `best.fetch_catalog` and `time.sleep` at call time
  (`best.fetch_catalog()`, and `sleep=None` → `time.sleep`), so that tests can
  patch them.
- `JobStore` must `chmod 0o700` its root explicitly, because `mkdir` modes are
  masked by umask.
- Required error text:
  - `submit`'s "still running" error contains `still running`.
  - The public-task error contains `public`.
  - The `wait` timeout error contains the job id.
  - The creation-error message contains `creation.log`.
- `resume(store, job_id, **overrides)`: the overrides replace spec keys
  (`max_cost_usd`, `concurrency`, `deadline_seconds`). The CLI's `remote resume`
  passes `--max-cost` and the others through.
- CLI: `--detach`, `--max-cost`, `--concurrency`, `--no-dedup`, `--wait-timeout`,
  and `--execute-in` without `--remote` are usage errors (exit 2).
- The tests were written before any implementation. A test that contradicts this
  plan, or is internally inconsistent, is a test bug: fix it and log it in
  section 6. A test that contradicts a live experiment means the plan changes:
  update both and log it.

### 3.9 Non-negotiable rules
- Never call `publish`. Assert the task is private after each push.
- Never write `MODEL_PROXY_API_KEY` or `MODEL_PROXY_URL` to any output. Never
  export the kernel's credentials for local use: that would bypass Kaggle's local
  model restriction.
- No silent model substitution. The model is pinned by the in-kernel probe
  before any row runs, and it is reported in `summary` and in every envelope.
- Remote is opt-in (`--remote`). The docs must say that prompts and responses
  are stored permanently in the user's Kaggle account (private; tasks cannot be
  deleted).
- Keep local `batch` behavior and all existing tests unchanged.

## 4. Phases

Each phase ends with `PYTHONPATH=src python3 -m unittest discover -s tests -v`
green. Run it from the skill dir with the tool's Python:
`~/.local/share/uv/tools/kaggle-llm/bin/python`.

**Phase 1: pure code.**
1. Remove the skip guard at the top of `tests/test_remote.py`.
2. Implement the `client.build_request` and `finish` refactor, `dedup.py`,
   `remote_runner.py`, and `remote.render_task` and `prepare_job`.
3. Done when these classes pass: `ClientRefactorTests`, `DedupTests`,
   `RunnerTests`, `ProxyCallTests`, `RenderTests`, `PrepareJobTests`.

**Phase 2: live experiments** (section 5). These use `render_task` and push with
`kaggle b t push`. Record the results in section 6 and update the constants and
defaults. If a result contradicts an assumption in `tests/test_remote.py`, change
the test and note why in the log.

**Phase 3: orchestration.** Implement `JobStore`, `SdkBackend`, `submit`, `wait`,
`collect`, `resume`, and `summary`. Done when `OrchestratorTests` pass. Then run
a live smoke test: a 3-row job via Python, where `collect` returns 3 ok rows.

**Phase 4: CLI and docs.**
1. CLI flags and subcommands. Done when `CliRemoteTests` pass.
2. Update `SKILL.md`: add a short "Remote batch (top models)" section with when
   to use it, the latency, the permanent-storage warning, `--max-cost`, and the
   `remote collect`/`resume` recovery flow.
3. Update `references/api.md` with the full details.
4. Update `agents/openai.yaml` only if the short description no longer fits.

**Phase 5: acceptance (live, small).** Run every step in section 7 and paste
the summaries into the log.

## 5. Live experiments (Phase 2)

Push with `kaggle b t push kaggle-llm-runner -f task.py --wait 7200 --poll-interval 10`,
where `task.py = render_task(spec)`, then run `kaggle b t download kaggle-llm-runner -o <dir>`.
Every push adds a permanent private version. Keep experiments minimal.

| # | Question | Procedure | Record / decide |
|---|---|---|---|
| E1 | Maximum payload embedded in the task file | `dry_run` spec with 1000 rows of about 4 KB of **random** words each (so gzip can't shrink it; about 4 MB encoded). If push or creation fails, halve until it passes. | Largest passing encoded size → `MAX_PAYLOAD_BYTES` in `remote.py`. `prepare_job` raises a clear error above it. If below about 2 MB, add a note that chunking is needed, and ask the user before building it. |
| E2 | Kernel wall-clock limit for creation runs | `dry_run {"sleep_seconds": 43200, "heartbeat_seconds": 300}`, 1 row. Wait up to 13 h, detached; check back with `status` and `download`. | Last heartbeat `elapsed` = limit. Set `DEFAULT_DEADLINE` = limit − 15 min and the CLI `--wait-timeout` default ≥ typical job length. |
| E3 | Direct proxy HTTP from the kernel gives raw completions | 2 real rows, `candidates=["openai/gpt-6-astra","anthropic/claude-opus-5@default"]`. | `raw` has `choices[0].finish_reason`, `usage.cost`. If direct HTTP fails with 401/403 while kbench works, switch `proxy_call` to kbench's `ModelProxy` client and document the parity loss. |
| E4 | Rate limits at concurrency | 40 short rows on the top candidate, `concurrency` 4, then 8. | Count `attempts > 1` and failures. The default `concurrency` = highest with < 10% retries. |
| E5 | Re-push behavior | Push the E3 spec again (new job_id). | The new creation run has a new run id and the job line `job_id` matches. `push` while pending is refused. Confirm `wait()`'s run matching works. |

## 6. Experiment log

(Append results here: date, spec summary, job_id, numbers, decision.)

## 7. Acceptance (Phase 5)

1. `kaggle-llm batch prompts50.jsonl --remote --max-cost 2 > out.jsonl`. Use 50
   varied real prompts, 3 of them near-duplicates. Expect:
   - 47 rows, all `ok`, `model` = top-ranked available catalog model
   - summary on stderr: `dropped_duplicates: 3`, `cost_usd` > 0 and ≤ 2
   - exit 0
2. The same run with `--schema` and a 2-field object schema. Expect:
   `structured_output` on every ok row; any invalid row reports a schema error.
3. `--detach`, then `remote status <id>` (submitted/running), then
   `remote collect <id>`. Output is identical in format to step 1.
4. `--max-cost 0.001`. Expect: `stopped_reason: max_cost`, most rows
   "not run", exit 1. Then `remote resume <id>` with a higher cap; `collect`
   returns all rows ok with the original line numbers.
5. `kaggle b t status kaggle-llm-runner` still shows `Public: False`.
6. Local mode is unchanged: `kaggle-llm batch small.jsonl` (no `--remote`)
   behaves as before.

## 8. Risks and open questions
- **Undocumented behavior.** Creation-run full-catalog access is observed, not
  documented. The `execute_in: "run"` fallback covers it.
- **Private SDK methods.** `SdkBackend` depends on private `kaggle` methods. Keep
  it the only place that touches the SDK, and keep the `kaggle<3` pin.
- **Quota.** Inference spend comes from the user's Kaggle Model Proxy quota,
  which the CLI cannot query in this version (`kaggle b quota` is absent). Report
  `cost_usd` always.
- **Model output terms.** Provider terms (OpenAI, Anthropic) restrict using
  output to train competing models. This is the user's decision, already
  surfaced to them. Don't block on it; mention it once in SKILL.md next to
  `--model`, noting that open-weight models (Qwen, DeepSeek, GLM) are available.
