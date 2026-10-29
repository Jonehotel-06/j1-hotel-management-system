#!/usr/bin/env bash
# Undo everything apply.sh did, and drop the additive database objects.
#
#   cd /path/to/j1-hotel-management-system
#   bash ai_updates/accounting_agent/rollback.sh
#
# Files are restored byte-for-byte from .rollback/; files that did not exist
# before are removed. The migration is then reversed with
# `python manage.py migrate finance <previous migration>`, which drops the two
# new tables, the nullable purchase-order column and its constraint. No
# pre-existing financial row is read, changed or deleted by the reverse
# migration: the columns it removes were added by this change.
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$AGENT_DIR/../.." && pwd)"
BACKUP_DIR="$AGENT_DIR/.rollback"

if [ ! -d "$BACKUP_DIR" ]; then
  echo "nothing to roll back: $BACKUP_DIR does not exist" >&2
  exit 1
fi

restored=0
while IFS= read -r rel; do
  [ -z "$rel" ] && continue
  case "$rel" in \#*) continue ;; esac
  # MANIFEST lines may be "M path" / "A path"; take the path.
  rel="${rel#M }"; rel="${rel#A }"
  dst="$REPO_ROOT/$rel"
  if [ -f "$BACKUP_DIR/$rel" ]; then
    cp -p "$BACKUP_DIR/$rel" "$dst"
    restored=$((restored + 1))
  elif [ -f "$BACKUP_DIR/$rel.absent" ]; then
    rm -f "$dst"
    restored=$((restored + 1))
  fi
done < "$AGENT_DIR/MANIFEST.txt"

echo "Restored $restored file(s) in $REPO_ROOT"
echo
echo "Now reverse the database change (run it from backend/):"
echo "  python manage.py migrate finance 0016_ledger_reporting_indexes"
echo
echo "WARNING: reversing the migration drops ReconciliationCheckReview rows and"
echo "SupplierPaymentRequest rows. Export them first if a review or a payment"
echo "request recorded under this change must be kept:"
echo "  python manage.py dumpdata finance.ReconciliationCheckReview finance.SupplierPaymentRequest --indent 2 > accounting_agent_reviews.json"
