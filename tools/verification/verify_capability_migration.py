#!/usr/bin/env python3
"""Prove the item-1 fix, and its rollback, at the database level.

`accounts/0021_staff_records_capabilities.py` is the fix for the fingerprint
enrolment failure: `staff.view` / `staff.manage` had never been defined, so no
Capability row existed and no account — not even an administrator — could hold
those codes. This script drives the migration forwards and backwards against the
LOCAL database and probes the API at each step:

    reverse  → the original symptom comes back (403 on the biometric status read)
    re-apply → the fix is restored, with the same grants as before

That makes the root cause in the report falsifiable rather than asserted, and it
exercises the rollback step in §F of the report before anyone needs it in an
incident.

    JONE_VERIFY_ALLOW_MIGRATION=1 JONE_VERIFY_PASSWORD=… \\
        python tools/verification/verify_capability_migration.py

Safety: it REFUSES to run without JONE_VERIFY_ALLOW_MIGRATION=1, because it
migrates a database. Point it only at a development database — the attendance and
audit history you keep does not belong to this check.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import requests

from jone_verify import ADMIN, API_BASE, api_login, password

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
MANAGE = BACKEND / "manage.py"
CODES = ("staff.view", "staff.manage")

SHELL_PROBE = (
    "from apps.accounts.models import Capability, RoleCapability;"
    "codes=%r;"
    "print('CAPS', sorted((c.code, c.is_active) for c in Capability.objects.filter(code__in=codes)));"
    "print('GRANTS', sorted(RoleCapability.objects.filter(capability__code__in=codes)"
    ".values_list('role','capability__code')))"
) % (CODES,)

ROWS = []


def record(name, ok, detail=""):
    ROWS.append((name, ok, detail))
    print(("PASS" if ok else "FAIL"), name, "-", detail)


def manage(*args, python: str = sys.executable) -> subprocess.CompletedProcess:
    return subprocess.run([python, str(MANAGE), *args], cwd=BACKEND,
                          capture_output=True, text=True, timeout=300)


def db_state() -> tuple[list, list]:
    out = manage("shell", "-c", SHELL_PROBE).stdout.strip().splitlines()
    caps = [line for line in out if line.startswith("CAPS")]
    grants = [line for line in out if line.startswith("GRANTS")]
    return caps, grants


def migration_head() -> str:
    out = manage("showmigrations", "accounts").stdout
    applied = [line for line in out.splitlines() if line.strip().startswith("[X]")]
    return applied[-1].strip() if applied else "(none)"


def api_can_read_biometric_status() -> int:
    """The exact call item 1 reported as failing for every role."""
    code, body = api_login(ADMIN)
    if code != 200:
        return code
    token = body["data"]["tokens"]["access"]
    response = requests.get(API_BASE + "/api/admin/staff-operations/fingerprint/staff/7/",
                            headers={"Authorization": "Bearer " + token}, timeout=30)
    return response.status_code


def main() -> int:
    if os.environ.get("JONE_VERIFY_ALLOW_MIGRATION") != "1":
        sys.exit("refusing to migrate: set JONE_VERIFY_ALLOW_MIGRATION=1 (development database only). "
                 "See tools/verification/README.md.")
    password()

    head_before = migration_head()
    caps_before, grants_before = db_state()
    status_before = api_can_read_biometric_status()
    print("· before:", head_before, caps_before, f"{len(grants_before)} grant line(s)", f"API HTTP {status_before}")

    # ---------------------------------------------------------------- reverse
    reversed_ok = manage("migrate", "accounts", "0020").returncode == 0
    caps_off, grants_off = db_state()
    status_off = api_can_read_biometric_status()
    inactive = all("False" in line for line in caps_off) and bool(caps_off)
    record("rollback runs cleanly and deactivates the capability rows", reversed_ok and inactive,
           f"{caps_off} · migrate rc ok={reversed_ok}")
    record("rolled back database reproduces the ORIGINAL symptom (403 for an administrator)",
           status_off == 403, f"API HTTP {status_off}")
    record("rollback drops the role grants", grants_off == ["GRANTS []"], f"{grants_off}")

    # ---------------------------------------------------------------- re-apply
    reapplied_ok = manage("migrate", "accounts", "0021").returncode == 0
    caps_after, grants_after = db_state()
    status_after = api_can_read_biometric_status()
    record("re-applying restores the grants exactly", reapplied_ok and grants_after == grants_before,
           f"{grants_after} · migrate rc ok={reapplied_ok}")
    record("re-applied database serves the read again (200 for an administrator)",
           status_after == 200, f"API HTTP {status_after}")
    record("migration head is 0021 afterwards", "0021" in migration_head(), migration_head())

    failed = [row for row in ROWS if not row[1]]
    print(f"\n{len(ROWS) - len(failed)}/{len(ROWS)} migration checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
