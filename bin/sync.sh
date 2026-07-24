#!/usr/bin/env bash
# Converge every registered tool skill dir onto the repo.
#
# Model: the repo is authoritative for which skills EXIST; each tool dir is a
# projection of it. The one exception is a real (non-symlink) directory in a
# tool dir containing a SKILL.md -- that is read as "a skill was just authored
# here", and it gets adopted into the repo.
#
# Because every projection is a symlink, there is exactly one copy of a skill on
# disk. Edits therefore need no syncing at all; only create/delete/rename do.
#
# Idempotent and order-independent: running it any number of times, from either
# tool, converges to the same state. Always exits 0 so it can never block a
# session start (use --strict to surface conflicts as a nonzero exit).

set -uo pipefail

REPO="${AGENT_SKILLS_REPO:-$HOME/agent-skills}"
SKILLS="$REPO/skills"
STRICT=0
[ "${1:-}" = "--strict" ] && STRICT=1

# Tool skill dirs as "path:comma,separated,exclusions".
# Exclusions exist so we never shadow a skill the tool ships itself --
# Codex ships its own skill-creator under .system/.
TOOLS=(
  "$HOME/.claude/skills:"
  "$HOME/.codex/skills:skill-creator"
)

warn() { printf 'skill-sync: %s\n' "$*" >&2; }
info() { [ -n "${SKILL_SYNC_QUIET:-}" ] || printf 'skill-sync: %s\n' "$*"; }

[ -d "$SKILLS" ] || { warn "no repo at $SKILLS -- nothing to do"; exit 0; }

# Single-flight: two sessions starting at once must not race on mv/ln.
exec 9>"$REPO/.sync.lock" || exit 0
flock -n 9 || exit 0

is_skill()  { [ -f "$1/SKILL.md" ]; }
# A tool dir that IS the repo skills dir (Claude, via whole-dir symlink) needs
# no projection -- skills created there are already in the repo.
is_mounted() { [ "$(readlink -f "$1")" = "$(readlink -f "$SKILLS")" ]; }

conflicts=0

# --- pass 1: adopt newly authored skills from every projected tool dir -------
# Runs across ALL tools before any linking, so a skill authored in Codex is in
# the repo before we try to project the repo back out.
for entry in "${TOOLS[@]}"; do
  dir="${entry%%:*}"
  [ -d "$dir" ] || continue
  is_mounted "$dir" && continue
  for path in "$dir"/*/; do
    [ -e "$path" ] || continue          # empty glob
    path="${path%/}"; name="${path##*/}"
    [ -L "$path" ] && continue          # already a projection
    is_skill "$path" || continue        # not a skill dir -- leave it alone
    if [ -e "$SKILLS/$name" ]; then
      warn "conflict: '$name' exists both in the repo and as a real dir in $dir"
      warn "          neither was modified -- merge by hand, then re-run"
      conflicts=1
      continue
    fi
    if mv "$path" "$SKILLS/$name"; then
      info "adopted '$name' from $dir"
    else
      warn "failed to adopt '$name' from $dir"
      conflicts=1
    fi
  done
done

# --- pass 2: project the repo into every tool dir, prune dead links ----------
for entry in "${TOOLS[@]}"; do
  dir="${entry%%:*}"; excl=",${entry#*:},"
  is_mounted "$dir" && continue
  mkdir -p "$dir" || continue

  # Broken links = skills deleted or renamed in the repo.
  while IFS= read -r dead; do
    rm -f "$dead" && info "pruned stale link $(basename "$dead") from $dir"
  done < <(find "$dir" -maxdepth 1 -xtype l 2>/dev/null)

  for path in "$SKILLS"/*/; do
    [ -e "$path" ] || continue
    path="${path%/}"; name="${path##*/}"
    is_skill "$path" || continue                    # skips bin/, docs, junk
    case "$name" in .*) continue ;; esac
    case "$excl" in *",$name,"*) continue ;; esac
    # Never write a link INTO an existing real directory of the same name.
    [ -d "$dir/$name" ] && [ ! -L "$dir/$name" ] && continue
    [ "$(readlink "$dir/$name" 2>/dev/null)" = "$path" ] && continue
    ln -sfn "$path" "$dir/$name" && info "linked '$name' into $dir"
  done
done

# --- report uncommitted work (never auto-commits) ---------------------------
if git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  n=$(git -C "$REPO" status --porcelain -- skills 2>/dev/null | grep -c .)
  [ "${n:-0}" -gt 0 ] && info "$n uncommitted skill change(s) -- 'skill save' to commit"
fi

[ "$STRICT" = 1 ] && exit "$conflicts"
exit 0
