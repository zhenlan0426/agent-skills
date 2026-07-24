#!/usr/bin/env bash
# Finds the last commit marked as a docs-sync checkpoint (trailer
# 'Docs-Sync-Checkpoint: true' in the commit message), then shows all
# commits and changed files since that commit.
#
# The marker is written by the docs-sync skill itself in Phase 6, so the
# baseline advances only when a real docs-sync runs — not on every ad-hoc
# commit that happens to mention "doc".
#
# Usage: bash git_changes_since_last_doc.sh [repo_path]
# Output: commit log + file diff summary since the last docs-sync checkpoint

set -euo pipefail

REPO="${1:-.}"
cd "$REPO"

CHECKPOINT_MARKER='Docs-Sync-Checkpoint: true'

# Find the most recent commit whose message contains the checkpoint trailer.
# --grep matches against the full commit message (subject + body), and -F
# treats the pattern as a fixed string so the colon/spaces aren't regex.
LAST_DOC_COMMIT=$(git log --all -F --grep="$CHECKPOINT_MARKER" --format='%H' -n 1 2>/dev/null || true)

if [ -z "$LAST_DOC_COMMIT" ]; then
  echo "NO_DOCS_SYNC_CHECKPOINT_FOUND"
  echo "---"
  echo "No prior docs-sync checkpoint found (looking for trailer: $CHECKPOINT_MARKER)."
  echo "This appears to be the first docs-sync run, or earlier syncs predate the marker."
  echo "Falling back to showing the last 20 commits."
  git log --oneline -20
  echo "---FILES_CHANGED---"
  git diff --stat HEAD~20 HEAD 2>/dev/null || git log --oneline --stat -20
  exit 0
fi

echo "LAST_DOCS_SYNC_COMMIT=$LAST_DOC_COMMIT"
echo "LAST_DOCS_SYNC_MSG=$(git log --format='%s' -n 1 "$LAST_DOC_COMMIT")"
echo "LAST_DOCS_SYNC_DATE=$(git log --format='%ci' -n 1 "$LAST_DOC_COMMIT")"
echo ""

# Show commits since the last docs-sync checkpoint (excluding it)
echo "---COMMITS_SINCE---"
git log --oneline "$LAST_DOC_COMMIT"..HEAD
echo ""

# Show files changed since the last docs-sync checkpoint
echo "---FILES_CHANGED---"
git diff --stat "$LAST_DOC_COMMIT"..HEAD
echo ""

# Show detailed diff of non-doc files (code changes) — summary only
echo "---CODE_CHANGES_SUMMARY---"
git diff --name-status "$LAST_DOC_COMMIT"..HEAD | grep -v '\.md$' || echo "(no non-doc file changes)"
echo ""

# Show current state of doc files changed
echo "---DOC_FILES_CHANGED---"
git diff --name-status "$LAST_DOC_COMMIT"..HEAD | grep '\.md$' || echo "(no doc files changed since last docs-sync checkpoint)"
