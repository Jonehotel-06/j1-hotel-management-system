#!/usr/bin/env bash
# Apply the Accounting agent repairs to a checkout of the hotel system.
#
#   cd /path/to/j1-hotel-management-system
#   bash ai_updates/accounting_agent/apply.sh            # copy files + back up what it replaced
#   cd backend && python manage.py migrate finance       # the one new migration
#
# The script only copies the files listed in MANIFEST.txt, preserving each
# repository-relative path. Every file it overwrites is first copied to
# ai_updates/accounting_agent/.rollback/ so rollback.sh can put the tree back
# exactly as it was.
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$AGENT_DIR/../.." && pwd)"
BACKUP_DIR="$AGENT_DIR/.rollback"

if [ ! -f "$REPO_ROOT/backend/manage.py" ]; then
  echo "error: $REPO_ROOT does not look like the hotel system (no backend/manage.py)" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
applied=0
while IFS= read -r rel; do
  [ -z "$rel" ] && continue
  case "$rel" in \#*) continue ;; esac
  # MANIFEST lines may be "M path" / "A path"; take the path.
  rel="${rel#M }"; rel="${rel#A }"
  src="$AGENT_DIR/$rel"
  dst="$REPO_ROOT/$rel"
  if [ ! -f "$src" ]; then
    echo "missing in agent folder: $rel" >&2
    exit 1
  fi
  if [ -f "$dst" ]; then
    mkdir -p "$BACKUP_DIR/$(dirname "$rel")"
    cp -p "$dst" "$BACKUP_DIR/$rel"
  else
    # New file: remember that it did not exist so rollback removes it.
    mkdir -p "$BACKUP_DIR/$(dirname "$rel")"
    : > "$BACKUP_DIR/$rel.absent"
  fi
  mkdir -p "$(dirname "$dst")"
  cp "$src" "$dst"
  applied=$((applied + 1))
done < "$AGENT_DIR/MANIFEST.txt"

echo "Applied $applied file(s) to $REPO_ROOT"
echo
echo "Next steps:"
echo "  1. cd $REPO_ROOT/backend && python manage.py migrate finance"
echo "     (additive only: one new table pair, one nullable FK, one unique"
echo "      constraint, one choice-list change — no column is dropped or renamed)"
echo "  2. run the tests named in REPORT.md section 6"
echo "  3. to undo everything: bash $AGENT_DIR/rollback.sh"
