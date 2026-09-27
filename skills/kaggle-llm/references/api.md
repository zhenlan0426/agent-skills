# Interface and setup

## Installation

Requires Unix (Linux/macOS), Python 3.10+, uv, and a working Kaggle account login. Windows is not supported because credential locking uses `fcntl`. Run these commands from this skill's directory to install the CLI in an isolated uv tool environment:

```bash
uv tool install --editable .
kaggle-llm auth
```

The package installs its own Kaggle CLI dependency. Place or link this directory in your agent's configured skills directory for discovery in a new session. No server or background process is required. To import the package from a project's Python environment, replace `/path/to/kaggle-llm` with this skill's directory:

```bash
uv pip install --python .venv/bin/python --editable /path/to/kaggle-llm
```

Or run a standalone script:

```bash
uv run --with /path/to/kaggle-llm python script.py
```

## Python API

```python
from kaggle_llm import Client, KaggleLLMError

with Client(timeout=120) as llm:
    print(llm.best_model())  # model used whenever model= is omitted
    result = llm.prompt("What is 2 + 2?", max_tokens=128)
    print(result["text"], result["usage"])

    extracted = llm.prompt(
        "Extract the count: there are seven apples.",
        schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
            "additionalProperties": False,
        },
    )
    print(extracted["structured_output"])

    raw = llm.chat([
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "My favorite color is blue."},
        {"role": "assistant", "content": "Understood."},
        {"role": "user", "content": "What is my favorite color?"},
    ])
    print(raw["choices"][0]["message"]["content"])
```

`Client.prompt(prompt, *, system=None, schema=None, schema_mode="prompt", model=None, max_tokens=None, temperature=None, reasoning=None)` returns a dictionary. Reasoning values are `none`, `minimal`, `low`, `medium`, and `high`; model support varies. Temperature is omitted by default because Kaggle's SDK also avoids assuming uniform support. The HTTP timeout is per network operation, not a total batch deadline.

`Client.chat(messages, ...)` returns raw Chat Completions JSON; it accepts the same generation parameters plus `response_format`. Messages contain exactly `role` and text `content`. It leaves finish reasons and refusals to the caller. `prompt` checks truncation and refusal, strips a leading `<think>...</think>` block regardless of reasoning settings, then checks for missing text and validates supplied schemas. Non-finite numbers in proxy JSON are rejected during parsing. Neither method executes code or tools. `KaggleLLMError.batch_fatal` identifies invalid model/endpoint configuration, credential refresh failures, 403, 404, 429, and persistent 401 responses so Python batch callers can also stop dispatching. `batch_transient` marks timeouts and HTTP 5xx errors; callers can count consecutive failures without parsing error messages. Model resolution checks the existing credential file before refreshing; first-use bootstrap still supplies the catalog when no credentials exist.

Schema output is parsed and validated locally with `jsonschema`. Same-document references are allowed; external references are rejected to keep validation offline. Format annotations are not checked. Prompt mode works across more providers; native mode sends a strict JSON Schema response format that unsupported models or schema shapes can reject. There is no hidden fallback or repair call.

With `model=None` (CLI: no `--model`), calls use `best_model()`. Selection ranks the Kaggle benchmark catalog (`ListBenchmarkModels`, versions with `allow_model_proxy` and a `model_proxy_slug`) plus `LLMS_AVAILABLE`, then probes candidates in rank order with a 256-token "Reply with OK." prompt. 400/403/404 mark a candidate unavailable and continue; any other failure (429, 5xx, timeout, refresh failure) aborts selection without caching. If the catalog is unreachable, only `LLMS_AVAILABLE` is ranked and the result says so in `catalog_source`. The pick, the unavailable list, and the untried candidates are cached for 24 hours in `<credential file>.model.json`. `kaggle-llm best --refresh` re-probes. The successful probe consumes a little inference quota.

`kaggle-llm models` shows `LLMS_AVAILABLE`, which comes from `kaggle b init`, whose CLI hard-codes curated IDs. It is neither exhaustive nor live-verified. Exact `provider/model` IDs are forwarded unchanged even when absent; bare aliases resolve only against the configured list, and fail if it is empty or the match is ambiguous. For a listed `anthropic/claude-sonnet-5@default`, aliases include `claude-sonnet-5`, `claude-sonnet-5@default`, and `claude-sonnet-5-default`. Obtain exact IDs from benchmark model metadata (`version.model_proxy_slug`) where possible. The proxy still enforces account access. Refreshing credentials does not discover all newly callable models. The full `kaggle b t models` benchmark catalog is also not proof of local access.

To check current account access, obtain an exact ID from the catalog or benchmark metadata and send one small prompt. Allow enough output tokens for models that spend tokens on reasoning:

```bash
kaggle-llm models
kaggle-llm prompt --model 'provider/model' --max-tokens 512 -p 'Reply with OK.'
```

Replace `provider/model` with the exact ID to probe. A successful response establishes access only for that account at that time. A 404 means the requested model or endpoint is unavailable; do not infer access from catalog membership or silently try other models.

## CLI and batches

Global options go before the subcommand:

```bash
kaggle-llm --timeout 180 prompt --stdin --text < prompt.txt
kaggle-llm --env-file /private/path/proxy.env models
kaggle-llm prompt -p 'Reply with OK.'
```

An alternate credential file can be set with `KAGGLE_LLM_ENV_FILE`. The caller's `.env` and ambient `MODEL_PROXY_*` values are intentionally not loaded, preventing stale environment values shadowing refreshed credentials. Custom files require `MODEL_PROXY_URL` and `MODEL_PROXY_API_KEY`; `LLMS_AVAILABLE` and `MODEL_PROXY_EXPIRY_TIME` are recommended. `LLM_DEFAULT` is ignored; the default model is `best_model()`.

Both `--env-file` and `KAGGLE_LLM_ENV_FILE` select a writable, managed credential file. Missing or expired credentials, a 401, or an explicit `auth` command trigger `kaggle b init` using the current Kaggle login. A successful refresh atomically replaces the entire selected file with Kaggle's output, including the endpoint and model catalog, with mode 0600. Use a dedicated file; unrelated keys and custom endpoint/token values are not preserved. Refresh subprocesses cannot read batch stdin.

Input JSONL:

```json
{"id":"a","prompt":"Extract the count: seven apples."}
{"id":"b","prompt":"Extract the count: twelve pears."}
```

```bash
kaggle-llm batch input.jsonl --schema schema.json --max-tokens 256 > results.jsonl
```

Output lines contain `{ "line": 1, "id": "a", "ok": true, "result": {...} }` or `{ "line": 2, "id": "b", "ok": false, "error": "..." }`. Processing is sequential and flushes after each row. Blank lines are skipped; `line` is the original physical line number. Ordinary row errors allow processing to continue. Invalid model/endpoint configuration, a credential refresh failure, 403, 404, 429, or 401 after the single refresh retry emits the failing row, stops the batch, and reports the stop on stderr. Three consecutive rows failing with timeouts or HTTP 5xx errors also stop the batch; mixed timeouts/5xx count together. A success or any other row error resets the count; blank lines are ignored. Remaining rows are not sent and have no output rows. Errors do not replay successful rows. CLI exits 0 on success, 1 on errors, 2 on argument usage errors, and 130 on interruption.

Row IDs may be strings, integers, or null; booleans are rejected before inference. Numeric options are checked before reading input or creating a client: `--max-tokens` must be positive, `--temperature` must be finite and between 0 and 2, and `--timeout` must be positive and finite. Invalid values are usage errors (exit 2) with no output rows.

Schema files are read as UTF-8 and checked once before processing begins. An invalid schema is a usage error (exit 2), with no inference requests or result rows. The prepared validator is reused for each output.

Envelopes preserve upstream usage/cost fields without estimating absent values. Keep result files private when handling private data. No prompt/result files are created automatically. Captured bootstrap output and raw upstream error bodies are not relayed because they may contain credentials or prompt content.

## Remote batches

`kaggle-llm batch INPUT --remote` runs a batch inside the private Kaggle benchmark task `kaggle-llm-batch` in the current Kaggle login's account. Kernels there can call the whole benchmark catalog, which local tokens cannot. Design notes and the experiment log are in [docs/remote-plan.md](../docs/remote-plan.md).

```bash
kaggle-llm batch prompts.jsonl --remote --max-cost 2 > out.jsonl
kaggle-llm batch prompts.jsonl --remote --model openai/gpt-6-astra --schema schema.json --max-cost 5
kaggle-llm batch prompts.jsonl --remote --detach          # {"job_id": "..."}
kaggle-llm remote status JOB                              # summary JSON, local state only
kaggle-llm remote collect JOB [--wait-timeout S]          # waits if needed, then rows on stdout
kaggle-llm remote resume JOB [--max-cost USD] [--concurrency N] [--detach] [--wait-timeout S]
kaggle-llm remote list
```

Flags: `--model`, `--system`, `--schema`, `--schema-mode`, `--max-tokens`, `--temperature` and `--reasoning` behave as in local batch. Remote-only: `--concurrency` (1-16, default 4), `--max-cost USD` (> 0), `--no-dedup`, `--detach`, `--wait-timeout` (default 3600 s), and `--execute-in creation|run`. Remote-only flags without `--remote` are usage errors (exit 2).

**What happens.** Every input row is validated first (object with nonempty string `prompt` and optional string/integer `id`). Any invalid row is a usage error (exit 2) and nothing is sent; local batch instead emits error rows. Near-duplicate prompts (5-word shingle Jaccard ≥ 0.85 after casefolding and removing punctuation; exact match for prompts under 5 words) are dropped, keeping the first occurrence, and listed on stderr as `{"dropped_duplicate": {"line", "id", "duplicate_of_line", "similarity"}}`. Messages and generation options are built exactly as `Client.prompt` builds them. The job is written to `~/.cache/kaggle-llm/jobs/<job_id>/` (override with `KAGGLE_LLM_JOBS_DIR`; directories are 0700), rendered to a percent-format task file (`task.py`, with the spec gzip+base64 embedded), and pushed as a new task version. Kaggle executes a pushed version once on creation. That creation run is the job: it ignores the default model and calls the proxy directly over HTTP.

**In the kernel.** The runner probes the candidates in order with a 256-token "Reply with OK.": your `--model` (exact ID, or a bare alias resolved against the live catalog), or else the catalog ranked as in `best` (Claude = OpenAI > Gemini). 429, 5xx, and timeouts are retried up to 4 attempts with 5/10/20 s backoff; other failures move to the next candidate. The first model that answers is pinned for every row. If none answers, no rows run (`stopped_reason: no_model`). Rows use the same retry rule and then fail. Before each row starts, the runner stops dispatching if spend has reached `--max-cost` (probe included; cost is the proxy's `usage.cost` nanodollar fields) or the kernel deadline has passed. In-flight rows finish; undispatched rows are reported `not run (stopped: <reason>)`. The kernel's proxy credentials never leave Kaggle and are never written to results.

**Results.** The CLI polls (5 s growing to 10 s) until the run finishes, downloads it, checks that the results belong to this job, and post-processes each response locally with the same rules as `Client.prompt`: refusal, truncation, `<think>` stripping, schema validation. Output rows are exactly local batch rows: `{"line", "id", "ok": true, "result": {...}}` or `{"line", "id", "ok": false, "error": "..."}`, in input order, with original line numbers. Kernel failures read `HTTP 429 after 4 attempts`. Successful rows whose text is a near-duplicate of an earlier successful row get `"near_duplicate_of": <earlier id>` and `"near_duplicate_of_line"`, and are kept. A summary goes to stderr: `{"job_id", "status", "model", "cost_usd", "rows_ok", "rows_failed", "rows_not_run", "dropped_duplicates", "stopped_reason"}`. Exit codes: 0 if every row is ok, 1 otherwise or on errors, 2 for usage errors, 130 on Ctrl-C.

**Recovery.** Ctrl-C while waiting prints `job <id> continues on Kaggle; collect with: kaggle-llm remote collect <id>`. `--wait-timeout` expiry exits 1 with the same hint. The job keeps running and nothing is lost. `remote resume JOB` creates and submits a child job with only the rows that failed (including local schema failures) or never ran, with the same model candidates and options. `--max-cost` and `--concurrency` override the parent's. Collecting a child merges its whole lineage: the earliest successful result per line wins. A creation failure saves the first 200 log lines to `<job dir>/creation.log`.

**Limits.**
- Kaggle rejects task notebooks of 1 MB or more. A job holds about 950 KB of compressed prompts (roughly 1000 prompts averaging 1.9 KB); larger inputs fail before pushing and must be split into several files.
- One job at a time: a push is refused while the previous version is still being created, and the CLI reports which local job holds it.
- Latency: about 75 s of Kaggle overhead per job plus the calls.

**Privacy and permanence.** Prompts, responses, and full conversation logs are stored in the Kaggle task (Kaggle also keeps its own run files), privately and permanently: Kaggle cannot delete tasks. After every push the CLI checks that the task and its backing notebook are private and fails loudly otherwise. It never publishes. Local job directories hold the same data; delete them when no longer needed.

**Fallback.** `--execute-in run` makes the creation run a no-op and schedules a separate `kaggle b t run -m <model>` for the pinned model. Use it only if creation runs lose full-catalog access (every candidate 403/404 in the probe).

Python: `from kaggle_llm import remote`, then `remote.prepare_job(rows, catalog=...)`, `JobStore().create(...)`, `remote.submit/wait/collect/resume/summary(store, [backend,] job_id)`, with `remote.default_backend()` as the Kaggle backend.

## Scope and sources

This is a synchronous client for Kaggle-authorized local model access, not a hosted endpoint. Local calls do not upload, publish, or create tasks. Only `batch --remote` creates task versions, always private, and it never publishes.

Implementation checked against:

- [Official CLI docs](https://github.com/Kaggle/kaggle-cli/blob/main/docs/benchmarks.md): local tokens, model restrictions, expiry, and inference quota.
- [Official ModelProxy source](https://github.com/Kaggle/kaggle-benchmarks/blob/main/src/kaggle_benchmarks/kaggle/model_proxy.py): base URL plus `/openapi`, bearer credentials, compatible client.
- [Official proxy adapter](https://github.com/Kaggle/kaggle-benchmarks/blob/main/src/kaggle_benchmarks/actors/proxy_openai.py): call shape and model-dependent parameters.

The upstream skill's interactive benchmark-authoring workflow is not imported; this skill covers inference only.
