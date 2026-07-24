#!/usr/bin/env bash
# One-time bootstrap, and the way to re-attach the repo on a new machine.
# Safe to re-run: every step is a no-op once already done.
set -euo pipefail

REPO="${AGENT_SKILLS_REPO:-$HOME/agent-skills}"
SKILLS="$REPO/skills"
CLAUDE_DIR="$HOME/.claude/skills"
CODEX_DIR="$HOME/.codex/skills"
HOOK="$REPO/bin/sync.sh"

say() { printf '==> %s\n' "$*"; }

mkdir -p "$SKILLS" "$REPO/bin"
chmod +x "$REPO/bin/sync.sh" "$REPO/bin/skill" 2>/dev/null || true

# put `skill` on PATH (~/.local/bin is already there on this machine)
if [ -d "$HOME/.local/bin" ]; then
  ln -sfn "$REPO/bin/skill" "$HOME/.local/bin/skill"
  say "linked 'skill' into ~/.local/bin"
fi

# 1. git
if ! git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  say "git init $REPO"
  git -C "$REPO" init -q -b main
fi

# 2. move any real skill dirs out of Claude's dir, then mount the repo there
if [ -d "$CLAUDE_DIR" ] && [ ! -L "$CLAUDE_DIR" ]; then
  for path in "$CLAUDE_DIR"/*/; do
    [ -e "$path" ] || continue
    path="${path%/}"; name="${path##*/}"
    [ -f "$path/SKILL.md" ] || { say "skipping non-skill dir $name"; continue; }
    if [ -e "$SKILLS/$name" ]; then
      say "SKIP '$name' -- already in repo; resolve by hand"
      continue
    fi
    say "moving $name -> $SKILLS/"
    mv "$path" "$SKILLS/$name"
  done
  if [ -z "$(ls -A "$CLAUDE_DIR" 2>/dev/null)" ]; then
    rmdir "$CLAUDE_DIR"
    ln -s "$SKILLS" "$CLAUDE_DIR"
    say "mounted $CLAUDE_DIR -> $SKILLS"
  else
    say "WARN $CLAUDE_DIR still has entries; left as a real dir (sync will project into it)"
  fi
fi

# 3. Codex: drop stale links, adopt real dirs, re-link. sync.sh does all of it.
if [ -d "$CODEX_DIR" ]; then
  find "$CODEX_DIR" -maxdepth 1 -type l -exec rm -f {} + 2>/dev/null || true
fi
"$HOOK"

# 4. hooks -- SessionStart is the one that matters: both tools enumerate skills
#    at startup, so converging there is what makes the other tool's new skills
#    visible. The end-of-session hook just makes it immediate.
if command -v jq >/dev/null 2>&1; then
  cs="$HOME/.claude/settings.json"
  [ -f "$cs" ] || echo '{}' > "$cs"
  cp "$cs" "$cs.bak.$(date +%s)"
  jq --arg cmd "$HOOK" '
    .hooks //= {} |
    .hooks.SessionStart = ((.hooks.SessionStart // []) | map(select(
       (.hooks // []) | any(.command == $cmd) | not))
       + [{hooks:[{type:"command", command:$cmd}]}]) |
    .hooks.Stop = ((.hooks.Stop // []) | map(select(
       (.hooks // []) | any(.command == $cmd) | not))
       + [{hooks:[{type:"command", command:$cmd}]}])
  ' "$cs" > "$cs.tmp" && mv "$cs.tmp" "$cs"
  say "installed Claude Code SessionStart + Stop hooks"

  xh="$HOME/.codex/hooks.json"
  [ -f "$xh" ] || echo '{}' > "$xh"
  [ -f "$xh" ] && cp "$xh" "$xh.bak.$(date +%s)"
  # PostToolUse matters here: Codex authors skills as real dirs, and a session
  # killed before SessionEnd would leave one unadopted and invisible to Claude.
  # Adopting after each tool call shrinks that window to seconds. Cheap: the
  # script is flock-guarded and exits immediately when nothing has changed.
  jq --arg cmd "$HOOK" '
    .hooks //= {} |
    .hooks.SessionStart = [{hooks:[{type:"command", command:$cmd}]}] |
    .hooks.SessionEnd   = [{hooks:[{type:"command", command:$cmd}]}] |
    .hooks.PostToolUse  = [{hooks:[{type:"command",
                                    command:("SKILL_SYNC_QUIET=1 " + $cmd)}]}]
  ' "$xh" > "$xh.tmp" && mv "$xh.tmp" "$xh"
  say "installed Codex SessionStart + SessionEnd + PostToolUse hooks (Codex asks once to trust them)"
else
  say "WARN jq not found -- add the hooks by hand, see README.md"
fi

say "done. 'skill ls' to see the result, 'skill doctor' to verify."
