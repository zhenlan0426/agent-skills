---
name: kaggle-llm
description: Call LLMs through the Kaggle Benchmarks Model Proxy from Python, a CLI, or JSONL batches. Use when the user requests Kaggle-backed API-style inference, structured extraction, or a script using Kaggle model access. Not for creating or publishing benchmark tasks, GPU notebooks, or generic batch work where another provider was chosen.
---

# Kaggle LLM

Use the installed `kaggle-llm` command or bundled `kaggle_llm.Client` Python package. This wraps the same local Model Proxy used by the official [write-kaggle-benchmarks skill](https://github.com/Kaggle/kaggle-skills/blob/main/write-kaggle-benchmarks/SKILL.md). No benchmark task or notebook is created.

Requires Unix (Linux/macOS); credential locking uses `fcntl`, so Windows is not supported.

## Quick use

```bash
kaggle-llm best
kaggle-llm prompt -p 'Reply with a short greeting.'
kaggle-llm prompt --file prompt.txt --schema schema.json
kaggle-llm batch input.jsonl > results.jsonl
```

## Model choice

Run `kaggle-llm best` (Python: `Client.best_model()`) every time you are about to use LLM API access through this skill, and report the model it returns. It picks the best model this account can actually call: Claude = OpenAI > Gemini, newest major version first, then largest size (Opus/Pro/full GPT > Sonnet/Flash/mini > Haiku/Lite/nano), then newest point release. Candidates come from Kaggle's live benchmark catalog plus the curated `LLMS_AVAILABLE`; each is probed with a tiny prompt in rank order and the first that answers wins. Other providers and open-weight models are never candidates.

The pick is cached for 24 hours next to the credential file. `prompt` and `batch` use it whenever `--model` is omitted (a batch resolves it once, so every row uses the same model), as does `Client.prompt`/`chat` with `model=None`. A 403/404 on the cached model drops the cache so the next call reselects. Use `kaggle-llm best --refresh` after Kaggle adds models or when access changes. Only pass `--model` when the user asks for a specific model. Never substitute models or route through remote benchmark tasks silently; see [references/api.md](references/api.md) for model resolution details.

Single-call stdout is a JSON envelope containing `text`, `structured_output`, `usage`, `model`, `finish_reason`, and `id`. Use `--text` for plain text. Inputs can be `-p`, `--file`, or `--stdin`. JSONL input rows contain `prompt` and optional `id`; output has one success/error row per processed nonblank input line. A batch continues after ordinary row errors, but stops after invalid model/endpoint configuration, credential refresh failure, 403, 404, 429, or a 401 that persists after refresh. It also stops after three consecutive rows fail with timeouts or HTTP 5xx errors (mixed failures count together); other row outcomes reset that count. Remaining rows are not sent or emitted. It exits 1 if any row failed; successful rows must not be replayed automatically.

For Python usage, installation, schemas, and failure handling, read [references/api.md](references/api.md).

## Operational rules

- First use bootstraps from the existing Kaggle login. Credentials live outside projects at `~/.config/kaggle-llm/credentials.env` (0600); refresh uses isolated temporary files and a process lock. `kaggle-llm auth` forces refresh. Do not print or commit this file.
- Calls consume the account's Model Proxy inference quota. Local access depends on the account and current Kaggle catalog. No unlimited-access or production-service guarantee is implied.
- Expired tokens refresh proactively; a 401 refreshes once. Custom credential files are also replaced on refresh; see the API reference before using `--env-file`. 403, 429, server errors, and timeouts are surfaced without retries. A timeout may still have consumed quota. Stop dispatching additional batches after quota exhaustion.
- `--schema` defaults to prompting for JSON and validating locally. `--schema-mode native` also requests native enforcement where supported. Validation failure and truncation are errors. JSON Schema format annotations are not enforced.
- Temperature, reasoning, and output-token parameters are sent only when requested. Tool execution, streaming, multimodal input, and an HTTP server are outside this wrapper's interface. `Client.chat` accepts explicit text history; `Client.prompt` has no persistent history.
- Respect the requested provider. Use Kaggle-backed inference when requested; preserve the provider chosen for unrelated bulk semantic work.

## Maintenance

Source is in `src/kaggle_llm/`; CLI and Python API share one client. From this skill directory run `PYTHONPATH=src python3 -m unittest discover -s tests -v` after changes. These tests need the Python dependencies but no Kaggle executable, credentials, or live inference. Keep integration smoke tests small and report whether they actually ran.
