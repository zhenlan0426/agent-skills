---
name: codex-delegate
description: >-
  Drive a coding subtask through Codex and review its work: Claude plans and
  writes the brief, Codex implements with full access, Claude reviews the diff
  and owns the commit. Use whenever the user asks to use Codex ("use codex",
  "have codex build this", "delegate to codex", a gpt-* Codex model name), or
  when a plan they gave you assigns work to Codex. Use this instead of
  codex:rescue, the codex:codex-rescue agent and raw `codex exec`: the plugin
  forces a sandbox that cannot start on this machine.
---

# Delegating to Codex

Claude drives, Codex implements, Claude reviews. Hand Codex substantial work with
a well-defined scope. A task that is quicker to do than to brief, do yourself.

All commands below use `~/.claude/skills/codex-delegate/scripts/cdx`.

## Why full access

Codex's bubblewrap sandbox cannot start here: AppArmor restricts unprivileged
user namespaces, so every shell call fails in every mode, read-only included
(`bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`). The plugin
hardcodes the sandbox, so its rescue path never works. `cdx` runs Codex without a
sandbox, which the user has approved as the default for this workflow, so do not
ask again. Do not launch full-access Codex any other way.

## 1. Brief

Write the brief to a file in the scratchpad. Codex starts with no context, so say:

- The goal, and why it matters, in a few lines.
- Files and docs to read first, and the interfaces to keep.
- What is out of scope, and anything not to touch: running jobs, a GPU in use,
  files another agent is editing.
- How to verify: the exact test or command to run, and what passing looks like.
- What to report: what changed, what was run, what could not be verified.

Nothing sandboxes Codex, and it holds the user's credentials. So every brief ends
with these limits, lifted only for what the task needs: do not commit or push, do
not submit to Kaggle or spend any other quota, do not delete anything the task
did not create, stay inside the repo.

## 2. Launch

Run it with the Bash tool's `run_in_background: true`. You are notified when it
exits, so do not poll. Keep working on anything independent.

```bash
~/.claude/skills/codex-delegate/scripts/cdx run --name attn-pool "$SCRATCHPAD/attn-pool.md"
```

`cdx` defaults to `gpt-6.1-sol` at `medium` effort, on both `run` and `resume`,
whatever `~/.codex/config.toml` says. Pass `--model` and `--effort` only when the
user names them: "6.1 terra high" is `--model gpt-6.1-terra --effort high`.

Two jobs at once must not share a working tree. Give each its own `git worktree`
and pass `--cd`.

## 3. Review

On exit `cdx` prints the changed files and Codex's final message. That message is
a claim, not evidence.

1. Read the whole diff: `cdx diff last` (add `--stat` or a path to narrow it).
   It covers everything since launch, including commits and new files, and leaves
   out edits that were already in the tree. Anything else that wrote to the tree
   meanwhile shows up too.
2. Check it against the brief: scope, edge cases, and files it should not have touched.
3. Run the verification yourself. Do not rely on Codex's report of it.

## 4. Fix, then commit

Send feedback to the same Codex thread, which keeps its context, and run this in
the background too. `cdx diff` stays cumulative across rounds.

```bash
~/.claude/skills/codex-delegate/scripts/cdx resume last "$SCRATCHPAD/attn-pool-fix.md"
```

Make small fixes yourself. Commit only after review passes, following the user's
usual commit rules.

If a job fails, read the log path `cdx` printed before retrying. `cdx list` shows
recent jobs.
