---
name: kaggle-llm
description: >-
  Default for programmatic, API-style LLM access: any repeated job whose per-item work
  needs semantic understanding (classifying, extracting, parsing, summarizing,
  labelling, judging, solving many files, rows, documents, or records), any LLM call
  embedded in a script, pipeline, or cron job, one-shot headless prompts, and
  structured JSON output against a schema. Reach for it INSTEAD of doing that work
  turn-by-turn inside the coding agent, whose quota is reserved for coding. Calls
  Claude/GPT models through the Kaggle Benchmarks Model Proxy from Python, a CLI, or
  JSONL batches, locally or as a remote batch on Kaggle's full model catalog (e.g.
  fine-tuning data). Falls back to the agy skill when Kaggle quota runs out or the
  user asks for Gemini. Not for authoring or publishing benchmark tasks or GPU
  notebooks.
---

# Kaggle LLM

Use the installed `kaggle-llm` command or bundled `kaggle_llm.Client` Python package. This wraps the same local Model Proxy used by the official [write-kaggle-benchmarks skill](https://github.com/Kaggle/kaggle-skills/blob/main/write-kaggle-benchmarks/SKILL.md). Local calls create no benchmark task or notebook; only the opt-in `batch --remote` does (see below).

Requires Unix (Linux/macOS); credential locking uses `fcntl`, so Windows is not supported.

## Choosing a backend

1. **Local `kaggle-llm` (default).** Current pick `anthropic/claude-sonnet-5@default`:
   18/18 on the [eval set](eval/README.md), about 5 s and $0.011 per item.
2. **`batch --remote`** only when the job needs a catalog model local access cannot
   reach (Opus 5, GPT-6 Astra, open-weight models) and the user has agreed to the
   permanent storage described below. About 1000 prompts per job, one job at a time.
3. **agy** (the `agy` skill, Gemini) as the fallback: when a local batch stops on
   403 or 429 (quota), for very large jobs where Kaggle spend matters (the Model
   Proxy quota cannot be queried; 10,000 items is roughly $100 of it), or when the
   user asks for Gemini or agy.

Before changing the default backend or model, run the eval set on the candidate.

## Quick use

```bash
kaggle-llm best
kaggle-llm prompt -p 'Reply with a short greeting.'
kaggle-llm prompt --file prompt.txt --schema schema.json
kaggle-llm batch input.jsonl > results.jsonl
```

## Model choice

Run `kaggle-llm best` (Python: `Client.best_model()`) every time you are about to use LLM API access through this skill, and report the model it returns. It picks the best model this account can actually call: Claude = OpenAI > Gemini, newest major version first, then largest size (Opus/Pro/full GPT > Sonnet/Flash/mini > Haiku/Lite/nano), then newest point release. Candidates come from Kaggle's live benchmark catalog plus the curated `LLMS_AVAILABLE`; each is probed with a tiny prompt in rank order and the first that answers wins. Other providers and open-weight models are never candidates.

The pick is cached for 24 hours next to the credential file. `prompt` and `batch` use it whenever `--model` is omitted (a batch resolves it once, so every row uses the same model), as does `Client.prompt`/`chat` with `model=None`. A 403/404 on the cached model drops the cache so the next call reselects. Use `kaggle-llm best --refresh` after Kaggle adds models or when access changes. Only pass `--model` when the user asks for a specific model. Never substitute models or use `--remote` without the user choosing it; see [references/api.md](references/api.md) for model resolution details.

Single-call stdout is a JSON envelope containing `text`, `structured_output`, `usage`, `model`, `finish_reason`, and `id`. Use `--text` for plain text. Inputs can be `-p`, `--file`, or `--stdin`. JSONL input rows contain `prompt` and optional `id`; output has one success/error row per processed nonblank input line. A batch continues after ordinary row errors, but stops after invalid model/endpoint configuration, credential refresh failure, 403, 404, 429, or a 401 that persists after refresh. It also stops after three consecutive rows fail with timeouts or HTTP 5xx errors (mixed failures count together); other row outcomes reset that count. Remaining rows are not sent or emitted. It exits 1 if any row failed; successful rows must not be replayed automatically.

For Python usage, installation, schemas, and failure handling, read [references/api.md](references/api.md).

## Remote batch (top models)

Local tokens reach only a small curated model set. `kaggle-llm batch input.jsonl --remote` instead runs the batch inside a private Kaggle benchmark task (`kaggle-llm-batch`), whose kernel can call the full catalog (GPT-6 Astra, Claude Opus 5, ...). Use it only when the user wants catalog models that local access cannot reach, typically for fine-tuning data; otherwise use local `batch`.

- **Tell the user first:** prompts and responses are stored **permanently** in their Kaggle account. The task is private, but Kaggle cannot delete tasks. The CLI refuses to push to a task that is not verifiably private, re-checks after every push, and fails loudly if not; never run `kaggle b t publish` on it.
- Model: without `--model`, the kernel probes the ranked catalog and pins the first model that answers; with it, only that model is tried. The pinned model is reported in the summary and every envelope; there is no substitution mid-job. Provider terms (OpenAI, Anthropic) restrict using outputs to train competing models; open-weight catalog models (Qwen, DeepSeek, GLM) are an alternative. That is the user's call.
- Expect about 75 s of Kaggle overhead per job plus the calls, which run with `--concurrency` (default 8). One job at a time: a push is refused while another job's task is still being created. A job holds up to about 950 KB of compressed prompts (roughly 1000 prompts averaging 1.9 KB); split larger files.
- Every prompt is sent by default. `--dedup` drops near-duplicate prompts before sending (listed on stderr; they get no output row); avoid it for templated prompts that differ only in a short input. A row still rate-limited (429) after its retries stops the job; remaining rows come back as `not run (stopped: rate_limited)` and can be resumed later. Near-duplicate responses are flagged with `near_duplicate_of`, never dropped. Output rows match local `batch`; exit 0 only if every row is ok.
- Remote jobs default to `--max-cost 10` and `--max-tokens 16000` (local calls send no cap). Set `--max-cost USD` to fit the job; when spend reaches it, remaining rows are not sent and come back as `not run (stopped: max_cost)`. Keep a token cap: Kaggle's proxy refuses (403) calls whose reserved spend (`max_tokens` × price, summed over in-flight calls) exceeds about $10, so uncapped jobs run one call at a time. Rows retry such 403s with backoff.
- Recovery: `--detach` prints `{"job_id": ...}` right after the push. Ctrl-C or `--wait-timeout` leaves the job running. `kaggle-llm remote status JOB` shows the summary; `kaggle-llm remote collect JOB` waits and prints the rows; `kaggle-llm remote resume JOB --max-cost USD` resubmits only failed or unrun rows and prints the merged result, keeping the original line numbers. `kaggle-llm remote list` shows local job records (`~/.cache/kaggle-llm/jobs`, private).

## Operational rules

- First use bootstraps from the existing Kaggle login. Credentials live outside projects at `~/.config/kaggle-llm/credentials.env` (0600); refresh uses isolated temporary files and a process lock. `kaggle-llm auth` forces refresh. Do not print or commit this file.
- Calls consume the account's Model Proxy inference quota. Local access depends on the account and current Kaggle catalog. No unlimited-access or production-service guarantee is implied.
- Expired tokens refresh proactively; a 401 refreshes once. Custom credential files are also replaced on refresh; see the API reference before using `--env-file`. 403, 429, server errors, and timeouts are surfaced without retries. A timeout may still have consumed quota. Stop dispatching additional batches after quota exhaustion.
- `--schema` requests JSON in the prompt, sends the schema as a native `response_format` (`--schema-mode native`, the default), and validates locally. Native mode lets the model reason before answering; with `--schema-mode prompt`, models tend to write their working and then the JSON, which fails validation (8 of 10 math rows in the eval). Use prompt mode only for a model or schema shape the native format rejects. Validation failure and truncation are errors. JSON Schema format annotations are not enforced.
- Temperature, reasoning, and output-token parameters are sent only when requested. Tool execution, streaming, multimodal input, and an HTTP server are outside this wrapper's interface. `Client.chat` accepts explicit text history; `Client.prompt` has no persistent history.
- Respect an explicitly requested provider: if the user or an existing script chose agy or another endpoint, keep it.

## Maintenance

Source is in `src/kaggle_llm/`; CLI and Python API share one client. From this skill directory run `PYTHONPATH=src python3 -m unittest discover -s tests -v` after changes. These tests need the Python dependencies but no Kaggle executable, credentials, or live inference. Keep integration smoke tests small and report whether they actually ran. `eval/` holds the backend eval set (live calls, about $0.20 per run).
