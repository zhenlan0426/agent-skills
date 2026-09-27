---
name: agy
description: >-
  Fallback for programmatic, API-style LLM access through the Antigravity CLI (Gemini)
  on a separate high-quota account. Use when the user asks for Gemini or agy ("use
  Gemini", "use agy", "run it headlessly"), when a kaggle-llm batch stops on quota
  (HTTP 403 or 429), or for a very large repeated LLM job where Kaggle Model Proxy
  spend matters. Otherwise the kaggle-llm skill is the default for scripted,
  batch, or schema-constrained LLM calls.
---

# agy — fallback LLM access for batch semantic work

`agy` (`~/.local/bin/agy`) is the **Antigravity CLI** — not the Antigravity
desktop app. It is installed and logged in.

## When to reach for it

`kaggle-llm` is the default backend for scripted LLM work: it scored higher and ran
faster on the eval set (`../kaggle-llm/eval/`), and its routing rules are in its
SKILL.md. Use agy instead when:

- **The user asks for Gemini or agy**, or an existing script already uses it.
- **Kaggle quota runs out.** A kaggle-llm batch stops on 403 or 429; send the
  unfinished rows here rather than retrying Kaggle.
- **The job is very large.** Kaggle spend comes from a proxy quota that cannot be
  queried (about $0.01 per item on the eval); this account has high quota and is
  not used for anything else.

The same principle holds for either backend: when the task is "go through all of
these and decide something about each one," write the loop and call the LLM from
it, rather than reading every item into the coding agent's context.

Not for this: interactive back-and-forth, work needing repo context or tool use,
or anything where you'd otherwise make one or two calls total.

## Invocation

```bash
agy -p "<prompt>" --model gemini-3.7-flash-high
```

Never leave `--model` unset; the built-in default is not this one.

- Reasoning effort is **baked into the model id** (`-high`), so `--effort` is
  redundant and should be omitted.
- Use the **newest Flash version that passes the eval set** (18/18, see
  `../kaggle-llm/eval/README.md`), not the "Pro" tier. `gemini-3.1-pro-high` is
  an older version and zhenlan judges it worse than 3.7 Flash — Pro is not an
  upgrade path.
- When `agy models` lists a newer Gemini, run the eval on it before switching.
  As of 2026-09-27, `gemini-3.8-flash-high` is newer but failed (empty output on
  a code-writing task, 3 of 3 times) and was about 2x slower, so the default
  stays `gemini-3.7-flash-high`.

## Structured output — the usual shape for batch work

```bash
agy -p "<prompt with the source text inlined>" \
    --model gemini-3.7-flash-high \
    --output-format json \
    --json-schema <schema-file-or-string>
```

The answer is in the JSON envelope's `structured_output` field. Use a schema for
anything a script will consume — it turns the call into a typed function.

## Gotchas

- **Run from an empty cwd.** Otherwise agy pulls the working directory into its
  workspace and the agent starts exploring it.
- **Inline the source text in the prompt; do not pipe it via stdin.** Piped
  input makes the agent reach for tools instead of just answering.
- **Headless mode auto-denies tool use.** That is a feature: it doubles as
  prompt-injection protection when the input text is untrusted.
- **Check `structured_output`, not `status`.** When the model tries a tool
  (e.g. running its own code to test it), headless mode denies it and the call
  can end with `status: SUCCESS`, exit 0, and `structured_output` null; the
  reason goes to stderr. Treat a null `structured_output` as a failed row and
  retry. Seen reproducibly on `gemini-3.8-flash-high` for a code-writing prompt
  (2026-09-27).
- `--print-timeout` defaults to 0 (no limit); set one (e.g. `15m`) so a
  runaway agent loop cannot hang a batch.

## Other flags worth knowing

`--add-dir` (add a workspace dir), `--dangerously-skip-permissions` (unattended
runs), `--output-format stream-json` (incremental), `-c` / `--conversation <id>`
(resume), `--sandbox`, `agy mcp` / `agy plugin` (manage servers and plugins).
