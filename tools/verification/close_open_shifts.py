#!/usr/bin/env python3
"""Close attendance windows left open by the verification scripts.

Running `api_verify_items.py` / `verify_ui_items.py` clocks real people in (items
4, 5, 6, 7 exercise CLOCK_IN and the terminal flows) and some of those windows are
deliberately left open — an open shift is evidence, and the scripts never delete
attendance history. The side effect is that the demo staff end up permanently
"ON_DUTY", which then makes the item-6 checks unrunnable, because they need a
subject who is *not* already on duty.

This helper turns those windows into normal COMPLETED records through the
application's own clock endpoint (each subject clocks out at the Receptionist
Desk with the workstation key, exactly as a person would). Nothing is deleted and
no timestamp is forged — the clock-out time is whatever the server stamps now.

    Dry run (default):  JONE_VERIFY_PASSWORD=… JONE_VERIFY_DESK_KEY=… \\
                        python close_open_shifts.py
    Apply:              … python close_open_shifts.py --apply

    --subjects a@x,b@y   restrict to specific emails (default: the seeded role
                         accounts this checkout's scripts sign in as)
"""
from __future__ import annotations

import argparse
import sys
import time

import requests

from jone_verify import (ADMIN, API, BAR, CHEF, GENERAL_MANAGER, HR, MANAGER, RECEPTION, WORKSTATION,
                         api_login, desk_key, password, terminal_clock_out)

# Non-exempt operational roles: the accounts the workstation rules bind and the
# verification scripts clock in. Discovered live, so a rename in the seed data
# cannot leave an account stranded on duty.
OPERATIONAL_ROLES = {"BARTENDER", "CHEF", "HOUSEKEEPING", "WAITER", "RECEPTIONIST"}
SEEDED_SUBJECTS = [ADMIN, MANAGER, GENERAL_MANAGER, HR, RECEPTION, CHEF, BAR]


def operational_emails(token: str) -> set[str]:
    response = requests.get(API + "/admin/staff-operations/profiles/",
                            headers={"Authorization": "Bearer " + token},
                            params={"page_size": 100}, timeout=30)
    rows = response.json().get("data") or []
    if isinstance(rows, dict):
        rows = rows.get("results", [])
    return {(row.get("email") or "").lower()
            for row in rows
            if row.get("role") in OPERATIONAL_ROLES and row.get("is_active")}


def admin_token() -> str:
    code, body = api_login(ADMIN)
    if code != 200:
        sys.exit(f"cannot sign in as {ADMIN}: HTTP {code}")
    return body["data"]["tokens"]["access"]


def open_windows(token: str) -> list[dict]:
    response = requests.get(API + "/admin/staff-operations/attendance/",
                            headers={"Authorization": "Bearer " + token},
                            params={"status": "ON_DUTY", "page_size": 100}, timeout=30)
    if response.status_code != 200:
        sys.exit(f"attendance list failed: HTTP {response.status_code} {response.text[:200]}")
    rows = response.json().get("data") or []
    if isinstance(rows, dict):
        rows = rows.get("results", [])
    return [row for row in rows if not row.get("clock_out_at")]


def close_window(email: str) -> tuple[int, str]:
    """The subject clocks out at the desk, as themselves."""
    code, body = api_login(email, headers={"X-JONE-Desktop-Key": desk_key()})
    if code != 200:
        return code, f"sign-in failed: {body.get('message', body)[:80]}"
    token = body["data"]["tokens"]["access"]
    response = requests.post(
        API + "/admin/staff-operations/attendance/clock/",
        headers={"Authorization": "Bearer " + token, "X-JONE-Desktop-Key": desk_key()},
        json={"action": "CLOCK_OUT",
              "idempotency_key": f"verify-cleanup-{email.split('@')[0]}-{int(time.time())}"},
        timeout=60,
    )
    return response.status_code, response.text[:120]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="actually clock out (default: dry run)")
    parser.add_argument("--subjects", default="", help="comma-separated emails to restrict to")
    args = parser.parse_args()

    password()          # fail loudly if the credential is not configured
    desk_key()
    token = admin_token()
    manager_token = token
    if args.subjects.strip():
        subjects = {e.strip().lower() for e in args.subjects.split(",") if e.strip()}
    else:
        subjects = operational_emails(token) | {e.lower() for e in SEEDED_SUBJECTS}
    windows = [row for row in open_windows(token)
               if (row.get("staff_email") or "").lower() in subjects]
    if not windows:
        print("no open attendance windows for the configured subjects — nothing to do")
        return 0

    print(f"{'email':34} {'reference':26} status    clocked in")
    for row in windows:
        print(f"{(row.get('staff_email') or ''):34} {(row.get('reference') or ''):26} "
              f"{row.get('status', ''):9} {(row.get('clock_in_at') or '')[:19]}")

    if not args.apply:
        print(f"\ndry run — nothing changed. Re-run with --apply to clock these out "
              f"at {WORKSTATION}.")
        return 0

    failures = 0
    for row in windows:
        email = (row.get("staff_email") or "")
    status, detail = terminal_clock_out(manager_token, email)
    if status not in (200, 201):
        status, detail = close_window(email)      # subject's own sign-in
        ok = status in (200, 201)
        failures += 0 if ok else 1
        print(("closed  " if ok else "FAILED  "), row.get("staff_email"), f"HTTP {status}", "" if ok else detail)
    remaining = [r for r in open_windows(token) if (r.get("staff_email") or "").lower() in subjects]
    print(f"\n{len(windows) - failures}/{len(windows)} windows closed · "
          f"{len(remaining)} still open for these subjects")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
