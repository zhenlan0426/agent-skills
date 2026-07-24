#!/usr/bin/env bash
# Mechanical audit of the agent-facing doc routing surface.
# Flags problems that break task -> doc -> code routing for a coding agent:
#
#   ORPHAN_DOC        active doc not referenced from the doc index
#   DEAD_LINK         doc index links to a file that does not exist
#   UNINDEXED_SCRIPT  script entry point with no row in the scripts index
#   NO_INDEX          doc index missing entirely
#
# Usage: bash doc_surface_audit.sh [repo_path]
# Conventions (override via env):
#   DOCS_DIR=docs  INDEX=$DOCS_DIR/README.md  SCRIPTS_DIR=scripts
#   SCRIPTS_INDEX=$DOCS_DIR/scripts.md (falls back to any-doc mention if absent)
#
# Output is one finding per line, prefixed with the tag above; prints OK
# per check when clean. Exit code is always 0 — findings are for the
# skill to triage, not a hard failure.

set -euo pipefail

REPO="${1:-.}"
cd "$REPO"
DOCS_DIR="${DOCS_DIR:-docs}"
INDEX="${INDEX:-$DOCS_DIR/README.md}"
SCRIPTS_DIR="${SCRIPTS_DIR:-scripts}"
SCRIPTS_INDEX="${SCRIPTS_INDEX:-$DOCS_DIR/scripts.md}"

# --- Check 1: every active doc has an index row -------------------------
if [ -f "$INDEX" ]; then
  orphans=0
  for f in "$DOCS_DIR"/*.md; do
    [ -e "$f" ] || continue
    [ "$f" = "$INDEX" ] && continue
    b=$(basename "$f")
    if ! grep -qF "$b" "$INDEX"; then
      echo "ORPHAN_DOC $f"
      orphans=1
    fi
  done
  [ "$orphans" -eq 0 ] && echo "OK no orphan docs"

  # --- Check 2: index links resolve -------------------------------------
  dead=0
  while read -r target; do
    case "$target" in
      http*|mailto:*) continue ;;
    esac
    if [ ! -e "$DOCS_DIR/$target" ] && [ ! -e "$target" ]; then
      echo "DEAD_LINK $INDEX -> $target"
      dead=1
    fi
  done < <(grep -oE '\]\([^)#]+' "$INDEX" | sed 's/^](//' | sort -u)
  [ "$dead" -eq 0 ] && echo "OK no dead index links"
else
  echo "NO_INDEX $INDEX (doc index missing — create one)"
fi

# --- Check 3: every script is findable from the scripts index ----------
if [ -d "$SCRIPTS_DIR" ]; then
  unindexed=0
  for s in "$SCRIPTS_DIR"/*.py; do
    [ -e "$s" ] || continue
    b=$(basename "$s")
    if [ -f "$SCRIPTS_INDEX" ]; then
      grep -qF "$b" "$SCRIPTS_INDEX" || { echo "UNINDEXED_SCRIPT $s"; unindexed=1; }
    else
      grep -rqF --include='*.md' "$b" "$DOCS_DIR" || { echo "UNINDEXED_SCRIPT $s"; unindexed=1; }
    fi
  done
  [ "$unindexed" -eq 0 ] && echo "OK all scripts indexed"
fi
