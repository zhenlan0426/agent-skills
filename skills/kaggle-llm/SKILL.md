---
name: kaggle-llm
description: Call LLMs through the Kaggle Benchmarks Model Proxy from Python, a CLI, or JSONL batches. Use when the user requests Kaggle-backed API-style inference, structured extraction, or a script using Kaggle model access. Not for creating or publishing benchmark tasks, GPU notebooks, or generic batch work where another provider was chosen.
---

# Kaggle LLM

Use the installed `kaggle-llm` command or bundled `kaggle_llm.Client` Python package. This wraps the same local Model Proxy used by the official [write-kaggle-benchmarks skill](https://github.com/Kaggle/kaggle-skills/blob/main/write-kaggle-benchmarks/SKILL.md). No benchmark task or notebook is created.

## Quick use

```bash
kaggle-llm models
kaggle-llm prompt -p 'Reply with a short greeting.'
kaggle-llm prompt --file prompt.txt --schema schema.json
kaggle-llm batch input.jsonl > results.jsonl
```

`models` returns the CLI's curated local list and default, not an exhaustive or live-verified catalog. Exact provider-qualified IDs are sent unchanged even when absent from that list; unique bare slugs resolve only from the list. Obtain exact IDs from benchmark model metadata (`version.model_proxy_slug`) where possible. The proxy determines access. The full `kaggle b t models` catalog includes models unavailable to local tokens. Never substitute models or route through remote benchmark tasks silently.

Live-tested on 2026-09-27: `google/gemini-3.7-flash` works despite being absent from the curated list; `anthropic/claude-sonnet-5@default` also works. For Gemini 3.7 allow enough output tokens for reasoning, e.g. `--reasoning low --max-tokens 512` for a tiny smoke test. See [references/api.md](references/api.md) for probe limits and results.

Single-call stdout is a JSON envelope containing `text`, `structured_output`, `usage`, `model`, `finish_reason`, and `id`. Use `--text` for plain text. Inputs can be `-p`, `--file`, or `--stdin`. JSONL input rows contain `prompt` and optional `id`; output has one success/error row per nonblank input line. A batch continues after row errors and exits 1 if any failed; successful rows must not be replayed automatically.

For Python usage, installation, schemas, and failure handling, read [references/api.md](references/api.md).

## Operational rules

- First use bootstraps from the existing Kaggle login. Credentials live outside projects at `~/.config/kaggle-llm/credentials.env` (0600); refresh uses isolated temporary files and a process lock. `kaggle-llm auth` forces refresh. Do not print or commit this file.
- Calls consume the account's Model Proxy inference quota. Local access depends on the account and current Kaggle catalog. No unlimited-access or production-service guarantee is implied.
- Expired tokens refresh proactively; a 401 refreshes once. 403, 429, server errors, and timeouts are surfaced without retries. A timeout may still have consumed quota. Stop dispatching additional batches after quota exhaustion.
- `--schema` defaults to prompting for JSON and validating locally. `--schema-mode native` also requests native enforcement where supported. Validation failure and truncation are errors. JSON Schema format annotations are not enforced.
- Temperature, reasoning, and output-token parameters are sent only when requested. Tool execution, streaming, multimodal input, and an HTTP server are outside this wrapper's interface. `Client.chat` accepts explicit text history; `Client.prompt` has no persistent history.
- Respect the requested provider. This skill does not replace the agy default for unrelated bulk semantic work.

## Maintenance

Source is in `src/kaggle_llm/`; CLI and Python API share one client. From this skill directory run `PYTHONPATH=src python3 -m unittest discover -s tests -v` after changes. These tests need no credentials or live inference. Keep integration smoke tests small and report whether they actually ran.
