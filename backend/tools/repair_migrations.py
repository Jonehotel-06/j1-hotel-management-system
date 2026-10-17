#!/usr/bin/env python
"""Make a copied checkout able to migrate again: drop stale migration files.

WHY THIS EXISTS
---------------
An older copy of this project (or a copy of its ``backend`` folder that was
merged on top of a newer one) can leave EXTRA migration files in
``apps/*/migrations``. Django then sees two chains in the same app and refuses
to run anything:

    CommandError: Conflicting migrations detected; multiple leaf nodes in the
    migration graph: (0001_initial, 0007_classify_existing_stock_items in
    inventory; 0002_cancellation_request_workflow, 0002_initial in enquiries; …)

Every app in the report has a FOUNDATION file that was renamed during
development (``0001_initial`` → ``0001_<thing>_foundation``) and/or a later
renumbering (``0006_laundry_walk_in_customers`` → ``0006_backfill_menuitem_sku_barcodes``).
The repository itself is correct and linear; the extra files are the leftovers.

WHAT IT DOES
------------
1. Reads ``tools/migration_manifest.txt`` — the canonical list of this
   release's migration files.
2. Anything else found in an ``apps/*/migrations`` folder is a leftover. It is
   MOVED (never deleted) into
   ``backend/.migration_backup/<UTC timestamp>/<original relative path>``,
   together with its compiled ``__pycache__`` entry, so it can be inspected or
   restored.
3. Reports any manifest entry that is missing from disk (that means the copy is
   older than the manifest — restore it from the repository instead).

It never touches the database, ``apps/*/models.py`` or any migration that
belongs to the current release.

USAGE
-----
    python tools/repair_migrations.py             # show what would move, then move it
    python tools/repair_migrations.py --dry-run   # show only
    python tools/repair_migrations.py --restore   # put the last backup back
    python tools/repair_migrations.py --write-manifest   # regenerate the manifest

Run it from the ``backend`` folder (``python manage.py migrate`` must work there),
then simply:

    python manage.py migrate
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
MANIFEST_PATH = BACKEND_DIR / "tools" / "migration_manifest.txt"
BACKUP_ROOT = BACKEND_DIR / ".migration_backup"


def _manifest_entries() -> list[str]:
    entries: list[str] = []
    for line in MANIFEST_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        entries.append(line)
    return entries


def _actual_entries() -> list[Path]:
    files: list[Path] = []
    for app_dir in sorted(BACKEND_DIR.glob("apps/*/migrations")):
        for path in sorted(app_dir.glob("*.py")):
            if path.name == "__init__.py":
                continue
            files.append(path)
    return files


def _relative(path: Path) -> str:
    return path.relative_to(BACKEND_DIR).as_posix()


def _latest_backup() -> Path | None:
    if not BACKUP_ROOT.is_dir():
        return None
    backups = sorted((p for p in BACKUP_ROOT.iterdir() if p.is_dir()), reverse=True)
    return backups[0] if backups else None


def show(dry_run: bool, restore: bool) -> int:
    if restore:
        backup = _latest_backup()
        if backup is None:
            print("No backup found; nothing to restore.")
            return 1
        restored = 0
        for path in sorted(backup.rglob("*.py")):
            target = BACKEND_DIR / path.relative_to(backup)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                print(f"  skip (already present): {_relative(target)}")
                continue
            shutil.copy2(path, target)
            restored += 1
            print(f"  restored {_relative(target)}")
        print(f"\nRestored {restored} file(s) from {backup}.")
        return 0

    try:
        manifest = set(_manifest_entries())
    except FileNotFoundError:
        print(f"Manifest not found at {MANIFEST_PATH}. Regenerate it with --write-manifest.")
        return 2

    actual = _actual_entries()
    current = {_relative(path) for path in actual}
    stale = sorted(current - manifest)
    missing = sorted(manifest - current)

    print(f"Canonical migrations in this release : {len(manifest)}")
    print(f"Migration files found on disk        : {len(current)}")

    if missing:
        print("\nMISSING from disk (this copy is OLDER than the release manifest):")
        for entry in missing:
            print(f"  {entry}")
        print("\nRestore these from the repository (git checkout -- backend/apps) before migrating.")

    if not stale:
        print("\nNo leftover migration files: the migration graph is already clean.")
        return 1 if missing else 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = BACKUP_ROOT / stamp
    print(f"\nLEFT-OVER migration files ({len(stale)}) — they create a second chain per app:")
    for entry in stale:
        print(f"  {entry}")
    if dry_run:
        print(f"\nDRY RUN: {len(stale)} file(s) would be moved to {destination}. Nothing changed.")
        return 1 if missing else 0

    for entry in stale:
        source = BACKEND_DIR / entry
        target = destination / entry
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        # Drop the compiled copy too, or Python would keep importing it.
        cached = list((source.parent / "__pycache__").glob(f"{source.stem}.*.pyc"))
        for pyc in cached:
            pyc.unlink(missing_ok=True)
        if not any((source.parent / "__pycache__").glob("*.pyc")):
            shutil.rmtree(source.parent / "__pycache__", ignore_errors=True)

    print(f"\nMoved {len(stale)} file(s) to {destination}")
    print("Nothing was deleted. To undo:  python tools/repair_migrations.py --restore")
    print("\nNow run:  python manage.py migrate")
    return 1 if missing else 0


def write_manifest() -> None:
    entries = [_relative(path) for path in _actual_entries()]
    existing = MANIFEST_PATH.read_text(encoding="utf-8").splitlines() if MANIFEST_PATH.exists() else []
    header = "\n".join(line for line in existing if line.startswith("#"))
    MANIFEST_PATH.write_text((header + "\n" if header else "") + "\n".join(entries) + "\n", encoding="utf-8")
    print(f"Wrote {len(entries)} migration file(s) to {MANIFEST_PATH}.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="show the leftovers without moving anything")
    parser.add_argument("--restore", action="store_true", help="restore the most recent backup")
    parser.add_argument("--write-manifest", action="store_true", help="regenerate tools/migration_manifest.txt")
    args = parser.parse_args()
    if args.write_manifest:
        write_manifest()
        return 0
    return show(dry_run=args.dry_run, restore=args.restore)


if __name__ == "__main__":
    sys.exit(main())
