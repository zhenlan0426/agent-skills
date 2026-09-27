# Interface and setup

## Installation

Source: `~/agent-skills/skills/kaggle-llm`. The global Codex link is `~/.codex/skills/kaggle-llm`; the existing Claude Code skills directory exposes the same source. The CLI is installed at `~/.local/bin/kaggle-llm` in an isolated uv tool environment.

Install on another Unix machine (requires uv and a working Kaggle account login):

```bash
uv tool install --editable "$HOME/agent-skills/skills/kaggle-llm"
ln -s "$HOME/agent-skills/skills/kaggle-llm" "$HOME/.codex/skills/kaggle-llm"
kaggle-llm auth
```

The package installs its own Kaggle CLI dependency. Global skill discovery occurs in a new agent session. No server or background process is required. To import the package from a project's Python environment:

```bash
uv pip install --python .venv/bin/python --editable "$HOME/agent-skills/skills/kaggle-llm"
```

Or run a standalone script:

```bash
uv run --with "$HOME/agent-skills/skills/kaggle-llm" python script.py
```

## Python API

```python
from kaggle_llm import Client, KaggleLLMError

with Client(timeout=120) as llm:
    print(llm.models())
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

`Client.chat(messages, ...)` returns raw Chat Completions JSON; it accepts the same generation parameters plus `response_format`. Messages contain exactly `role` and text `content`. It leaves finish reasons and refusals to the caller. `prompt` checks truncation, refusal, missing text, and supplied schemas. Neither method executes code or tools.

Schema output is parsed and validated locally with `jsonschema`. Same-document references are allowed; external references are rejected to keep validation offline. Format annotations are not checked. Prompt mode works across more providers; native mode sends a strict JSON Schema response format that unsupported models or schema shapes can reject. There is no hidden fallback or repair call.

The list comes from `kaggle b init`, whose CLI supplies curated IDs. It is neither exhaustive nor live-verified. Exact `provider/model` IDs are forwarded unchanged even when absent; bare aliases resolve only against the configured list. The proxy still enforces account access. Refreshing credentials does not discover all newly callable models. The full benchmark catalog is also not proof of local access.

Local probes on 2026-09-27 verified `google/gemini-3.7-flash` (unlisted) and `anthropic/claude-sonnet-5@default`. Gemini 3.7 exhausted a 64-token budget on reasoning; use an adequate output budget. Example:

```bash
kaggle-llm prompt --model google/gemini-3.7-flash --reasoning low --max-tokens 512 -p 'Reply with OK.'
```

The catalog's exact IDs for Gemini 3.8 Flash, GPT-6 Astra, GPT-5.6 Terra/Luna, GPT-5.5, and Opus 5 returned 404 with this account's local credentials. The inferred Opus 5.5 ID `anthropic/claude-opus-5-5@default` also returned 404; that ID was not supplied by the catalog, so this does not rule out every possible Opus 5.5 identifier. These observations are account/time-specific. The local raw probe report is `~/.config/kaggle-llm/local-model-probe.json` on the machine where testing ran.

## CLI and batches

Global options go before the subcommand:

```bash
kaggle-llm --timeout 180 prompt --stdin --text < prompt.txt
kaggle-llm --env-file /private/path/proxy.env models
kaggle-llm prompt --model google/gemini-3.1-flash-lite-preview -p 'Reply with OK.'
```

The example model was tested on 2026-09-27; consult `models` for the configured catalog. An alternate credential file can be set with `KAGGLE_LLM_ENV_FILE`. The caller's `.env` and ambient `MODEL_PROXY_*` values are intentionally not loaded, preventing stale environment values shadowing refreshed credentials. Custom files require `MODEL_PROXY_URL` and `MODEL_PROXY_API_KEY`; `LLM_DEFAULT`, `LLMS_AVAILABLE`, and `MODEL_PROXY_EXPIRY_TIME` are recommended.

Input JSONL:

```json
{"id":"a","prompt":"Extract the count: seven apples."}
{"id":"b","prompt":"Extract the count: twelve pears."}
```

```bash
kaggle-llm batch input.jsonl --schema schema.json --max-tokens 256 > results.jsonl
```

Output lines contain `{ "line": 1, "id": "a", "ok": true, "result": {...} }` or `{ "line": 2, "id": "b", "ok": false, "error": "..." }`. Processing is sequential and flushes after each row. Blank lines are skipped; `line` is the original physical line number. Errors do not replay successful rows. CLI exits 0 on success, 1 on errors, 2 on argument usage errors, and 130 on interruption.

Envelopes preserve upstream usage/cost fields without estimating absent values. Keep result files private when handling private data. No prompt/result files are created automatically. Captured bootstrap output and raw upstream error bodies are not relayed because they may contain credentials or prompt content.

## Scope and sources

This is a synchronous client for Kaggle-authorized local model access, not a hosted endpoint or unrestricted access to the full catalog. It does not upload, publish, or create tasks. Remote task execution would be a separate workflow with notebook latency and persisted task data.

Implementation checked against:

- [Official CLI docs](https://github.com/Kaggle/kaggle-cli/blob/main/docs/benchmarks.md): local tokens, model restrictions, expiry, and inference quota.
- [Official ModelProxy source](https://github.com/Kaggle/kaggle-benchmarks/blob/main/src/kaggle_benchmarks/kaggle/model_proxy.py): base URL plus `/openapi`, bearer credentials, compatible client.
- [Official proxy adapter](https://github.com/Kaggle/kaggle-benchmarks/blob/main/src/kaggle_benchmarks/actors/proxy_openai.py): call shape and model-dependent parameters.

The upstream skill's interactive benchmark-authoring workflow is not imported; this is a separately scoped inference client requested by the user.
