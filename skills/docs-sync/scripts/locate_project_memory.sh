#!/usr/bin/env bash
# Locates the CURRENT project's Claude Code memory directory and lists its
# files. The path is DERIVED from the repo root at runtime, never hardcoded:
# Claude Code stores per-project memory at
#   ~/.claude/projects/<encoded-project-root>/memory/
# where <encoded-project-root> is the absolute project path with every '/'
# replaced by '-' (so /home/u/proj -> -home-u-proj).
#
# Usage: bash locate_project_memory.sh [repo_path]
# Output:
#   MEMORY_DIR=<abs path>            (or MEMORY_DIR_NOT_FOUND)
#   MEMORY_INDEX=<abs path to MEMORY.md>   (or MEMORY_INDEX_ABSENT)
#   ---MEMORY_FILES--- followed by one line per *.md memory file (excluding
#   MEMORY.md), with age in days so the caller can prioritize stale notes.

set -euo pipefail

REPO="${1:-.}"
cd "$REPO"

ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
CLAUDE_PROJECTS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects"

encode() { printf '%s' "$1" | sed 's#/#-#g'; }

MEMORY_DIR=""
# Primary: direct encoding of the git root.
cand="$CLAUDE_PROJECTS/$(encode "$ROOT")/memory"
if [ -d "$cand" ]; then
  MEMORY_DIR="$cand"
else
  # Fallback: cwd may differ from git root; try encoding of pwd.
  cand="$CLAUDE_PROJECTS/$(encode "$(pwd)")/memory"
  [ -d "$cand" ] && MEMORY_DIR="$cand"
fi

if [ -z "$MEMORY_DIR" ]; then
  echo "MEMORY_DIR_NOT_FOUND"
  echo "---"
  echo "No per-project memory dir under $CLAUDE_PROJECTS for repo root: $ROOT"
  echo "(encoded as $(encode "$ROOT")). Nothing to clean up."
  exit 0
fi

echo "MEMORY_DIR=$MEMORY_DIR"

if [ -f "$MEMORY_DIR/MEMORY.md" ]; then
  echo "MEMORY_INDEX=$MEMORY_DIR/MEMORY.md"
else
  echo "MEMORY_INDEX_ABSENT"
fi

echo "---MEMORY_FILES---"
now=$(date +%s)
found=0
for f in "$MEMORY_DIR"/*.md; do
  [ -e "$f" ] || continue
  base=$(basename "$f")
  [ "$base" = "MEMORY.md" ] && continue
  found=1
  mtime=$(date -r "$f" +%s 2>/dev/null || echo "$now")
  age_days=$(( (now - mtime) / 86400 ))
  echo "${age_days}d	$f"
done
if [ "$found" = 0 ]; then
  echo "(no memory files besides MEMORY.md)"
fi
