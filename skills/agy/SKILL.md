---
name: agy
description: >-
  Get programmatic, API-style LLM access for a repeated job whose per-item work needs
  semantic understanding — classifying, extracting, parsing, summarizing, labelling,
  or judging many files, rows, documents, or records, and any LLM call embedded in a
  script, pipeline, or cron job. Reach for this INSTEAD of doing the semantic work
  turn-by-turn inside the coding agent: it runs on a separate, largely unused
  high-quota account, while Claude Code and Codex quota is reserved for coding.
  Triggers include looping over many inputs that each need a language judgment,
  writing a script that has to call an LLM, one-shot headless prompts, structured
  JSON output against a schema, unattended or batch runs, and any request to "use
  Gemini", "use agy", or "run it headlessly".
---

# agy — programmatic LLM access for batch semantic work

`agy` (`~/.local/bin/agy`) is the **Antigravity CLI** — not the Antigravity
desktop app. It is installed and logged in.

## When to reach for it

The signal is **a repeated job that needs language understanding per item**, with
no human in the loop: N documents to extract fields from, N rows to classify, N
files to summarize, a script that needs one LLM call per record.

Treat it as an **API you can shell out to**, and prefer it over doing the same
work yourself in-session:

- **Quota.** This account has high quota and is not used for anything else.
  Claude Code and Codex quota is spent on coding agents and may be tight — do not
  burn it on bulk semantic labor a cheaper endpoint can do.
- **Determinism.** A scripted loop with a fixed prompt and a JSON schema is
  reproducible and re-runnable; the same work done conversationally is not.
- **Scale.** Per-item calls stay flat in cost as N grows, instead of dragging
  every item through one long context.

So when the task is "go through all of these and decide something about each
one," write the loop and call `agy` from it, rather than reading them all in and
answering item by item.

Not for this: interactive back-and-forth, work needing repo context or tool use,
or anything where you'd otherwise make one or two calls total.

## Invocation

```bash
agy -p "<prompt>" --model gemini-3.7-flash-high
```

Never leave `--model` unset; the built-in default is not this one.

- Reasoning effort is **baked into the model id** (`-high`), so `--effort` is
  redundant and should be omitted.
- Pick the **highest version number**, not the "Pro" tier. `gemini-3.1-pro-high`
  is an older version and zhenlan judges it worse than 3.7 Flash — Pro is not an
  upgrade path.
- Run `agy models` when a newer Gemini may have shipped and move to it. As of
  2026-08-30 the newest was `gemini-3.7-flash-high`.

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
- `--print-timeout` defaults to 5m; raise it for long jobs.

## Other flags worth knowing

`--add-dir` (add a workspace dir), `--dangerously-skip-permissions` (unattended
runs), `--output-format stream-json` (incremental), `-c` / `--conversation <id>`
(resume), `--sandbox`, `agy mcp` / `agy plugin` (manage servers and plugins).
