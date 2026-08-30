#!/usr/bin/env bash
# Commit and push skill changes automatically.
#
# Deliberately NOT part of sync.sh: that script also runs from .bashrc on every
# interactive shell and from Codex's PostToolUse hook, so folding git writes into
# it would commit on every terminal you open and push on every tool call. This
# runs only at end-of-turn / end-of-session, where a batch of edits is complete.
#
# Never blocks and never fails a session: every path exits 0. A failed push is
# not data loss -- the commit is already local, and the next run retries.
#
#   autosave.sh              commit + push if anything changed
#   autosave.sh --no-push    commit only (also: SKILL_AUTOSAVE_NO_PUSH=1)

set -uo pipefail

REPO="${AGENT_SKILLS_REPO:-$HOME/agent-skills}"
NO_PUSH="${SKILL_AUTOSAVE_NO_PUSH:-}"
[ "${1:-}" = "--no-push" ] && NO_PUSH=1

info() { [ -n "${SKILL_SYNC_QUIET:-}" ] || printf 'skill-autosave: %s\n' "$*"; }
warn() { printf 'skill-autosave: %s\n' "$*" >&2; }

git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || exit 0

# Single-flight: two sessions ending at once must not race on index.lock.
exec 9>"$REPO/.autosave.lock" || exit 0
flock -n 9 || exit 0

# Nothing staged, nothing tracked-dirty, nothing untracked -> done.
[ -n "$(git -C "$REPO" status --porcelain 2>/dev/null)" ] || exit 0

# Name the skills that actually moved, so history stays readable.
names=$(git -C "$REPO" status --porcelain 2>/dev/null \
        | awk '{print $NF}' \
        | sed -n -e 's|^skills/\([^/]*\)/.*|\1|p' -e 's|^commands/\(.*\)|command \1|p' \
        | sort -u | paste -sd', ' -)
[ -n "$names" ] || names="repo"
msg="skills: autosave $names"
[ "${#msg}" -gt 72 ] && msg="skills: autosave ($(git -C "$REPO" status --porcelain | grep -c .) changes)"

git -C "$REPO" add -A || { warn "git add failed"; exit 0; }
git -C "$REPO" diff --cached --quiet && exit 0
if ! git -C "$REPO" commit -q -m "$msg"; then
  warn "commit failed -- leaving changes staged"
  exit 0
fi
info "committed: $msg"

[ -n "$NO_PUSH" ] && exit 0
git -C "$REPO" remote get-url origin >/dev/null 2>&1 || exit 0

branch=$(git -C "$REPO" branch --show-current 2>/dev/null)
[ -n "$branch" ] || { warn "detached HEAD -- committed but not pushed"; exit 0; }

push() { timeout 30 git -C "$REPO" push -q origin "$branch" 2>/dev/null; }

if push; then
  info "pushed to origin/$branch"
else
  # Most likely the remote moved (edited from another machine). Rebase once.
  if timeout 30 git -C "$REPO" pull --rebase --autostash -q origin "$branch" 2>/dev/null && push; then
    info "rebased on origin/$branch and pushed"
  else
    warn "push failed -- commit is safe locally, will retry next session"
  fi
fi
exit 0
