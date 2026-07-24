---
name: tui-tmux-harness
description: >
  Use this skill whenever you build, modify, or debug a Terminal UI (TUI) in
  this project — e.g. the GT2 reviewer, any curses/textual/rich-based app, or
  any full-screen terminal program. The agent cannot see the TUI it creates, so
  verification must happen by running it inside a detached tmux session and
  using capture-pane + send-keys to observe and drive it. Trigger on: "review
  the TUI", "test the TUI", "the rendering looks off", "check that pressing X
  does Y", or any change to TUI layout/keybindings/rendering logic.
---

# TUI verification via detached tmux

## Why this exists

Claude Code cannot see or interact with a TUI it launches directly — the terminal is attached to the user, and tool results return nothing useful. Running the TUI inside a **detached tmux session** makes the UI state inspectable (via `capture-pane`) and drivable (via `send-keys`), so you can verify rendering and interaction end-to-end after every change.

Use this harness instead of claiming "the TUI should work" without verification.

## Minimal workflow

```bash
SESSION=mika-tui

# 1. Start the TUI in a detached, size-pinned session
tmux kill-session -t "$SESSION" 2>/dev/null
tmux new-session -d -s "$SESSION" -x 200 -y 50 "python path/to/tui_app.py"

# 2. Let it paint, then capture
sleep 0.4
tmux capture-pane -t "$SESSION" -p

# 3. Drive it
tmux send-keys -t "$SESSION" Down
sleep 0.2
tmux capture-pane -t "$SESSION" -p

# 4. Send a literal string vs a named key
tmux send-keys -t "$SESSION" -l "hello"      # literal text
tmux send-keys -t "$SESSION" Enter           # named key

# 5. Clean up when done
tmux kill-session -t "$SESSION"
```

## Rules that prevent flaky verification

1. **Pin terminal size** with `-x`/`-y` on `new-session`. Without this, capture output depends on the host terminal and layout assertions break across environments. Default to `200x50` unless the app needs different dimensions.
2. **Wait for repaint** after every `send-keys` before capturing. A `sleep 0.2`–`0.4` is usually enough; for slower apps, poll `capture-pane` until the output changes rather than guessing.
3. **Know when to use `-l`**. Literal strings that happen to collide with key names (`space`, `enter`) need `-l`. Named keys (`Enter`, `Down`, `Up`, `Left`, `Right`, `Tab`, `BSpace`, `C-c`, `C-a`, `M-x`, `Space`) go **without** `-l`.
4. **Strip styling by default**. `capture-pane -p` already omits ANSI color — good for stable diffs. Only add `-e` when verifying styling itself.
5. **Kill the session between runs.** Stale state (leftover modal, cursor position, partial input) is the #1 source of confusing captures. Always `tmux kill-session -t "$SESSION" 2>/dev/null` before `new-session`.
6. **Scrollback**: `capture-pane` shows only the visible pane. For history, use `-S -100 -E -`.
7. **Verify after every rendering-logic change**, not just at the end. Layout regressions are cheap to catch one change at a time and expensive to bisect later.

## Common pitfalls

- TUI exits immediately on launch → the session disappears. Detect with `tmux has-session -t "$SESSION"`; if missing, re-run the command in the foreground to see the traceback.
- `send-keys` appears to do nothing → you likely sent a literal name instead of a key (e.g. `"Enter"` as text). Drop the quotes for named keys, or use `-l` only for literal text.
- Capture shows stale content → no `sleep` after `send-keys`, or the app debounces input. Increase the wait or poll until the frame changes.
- Different results locally vs in the agent → terminal size drift. Always pin `-x`/`-y`.

## What to report back

When verifying a TUI change, include in your response:
- The exact `capture-pane` snapshot(s) showing the relevant region before/after the interaction.
- The `send-keys` sequence you used.
- A one-line conclusion: what you verified works, and what you could not verify (and why).

Never report a TUI task complete without at least one `capture-pane` confirming the expected state.
