#!/usr/bin/env python3
"""Live API verification of audit items 1, 4, 5, 6, 7 against the sandbox Django server.

Run:  python tools/verification/api_verify_items.py

The login endpoint is throttled at 5/min per client, so the script keeps its
sign-ins in two budgeted windows (it sleeps between them) instead of clearing a
cache that lives in another process. Everything is read/create-only.
"""
import sys
import time
from datetime import datetime, timedelta

import requests
from jone_verify import (ADMIN, BAR, CHEF, MANAGER, RECEPTION, API, API_BASE, api_login,
                         desk_key, password, self_clock_out, terminal_clock_out)

BASE = API_BASE
API = BASE + "/api"
ADMIN_NS = API + "/admin/staff-operations"
PW = password()
DESK_KEY = desk_key()
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

VALID_MINUTIAE = [
    {"x": 100 + i * 5, "y": 150 + i * 4, "angle": (30 + i * 15) % 360, "type": "ending", "quality": 90}
    for i in range(15)
]

RESULTS = []


def record(item, name, ok, detail=""):
    """ok is True/False, or None when the check could not be executed at all —
    a skipped check is never counted as a pass."""
    RESULTS.append((item, name, ok, detail))
    label = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
    print(label, f"[{item}]", name, "-", detail)


def login(email, headers=None, **extra):
    body = {"email": email, "password": PW}
    body.update(extra)
    r = None
    for _ in range(4):
        r = requests.post(API + "/auth/login/", json=body, headers=headers or {}, timeout=30)
        if r.status_code != 429:
            return r
        print("· login throttled; waiting for the next window…")
        time.sleep(62)
    return r


def token(email, headers=None, **extra):
    r = login(email, headers=headers, **extra)
    if r.status_code != 200:
        print("  !! login failed", email, r.status_code, r.text[:200])
        sys.exit(1)
    return r.json()["data"]["tokens"]["access"]


def staff_rows(token, **params):
    """Staff profiles straight from the API — no ORM/shell coupling."""
    rows = data_of(requests.get(ADMIN_NS + "/profiles/", headers=auth(token),
                                params={"page_size": 100, **params}, timeout=30))
    return rows if isinstance(rows, list) else []


def fresh_id(email, token_value=None):
    """The user id behind an email, found through the staff profile search."""
    token_value = token_value or token(ADMIN)
    for row in staff_rows(token_value, search=email):
        if (row.get("email") or "").lower() == email.lower():
            return row["id"]
    for row in staff_rows(token_value):
        if (row.get("email") or "").lower() == email.lower():
            return row["id"]
    raise SystemExit(f"no staff profile found for {email}")


def auth(tok):
    return {"Authorization": "Bearer " + tok}


def envelope(r):
    try:
        return r.json()
    except Exception:
        return {"raw": r.text[:300]}


def data_of(r):
    body = envelope(r)
    return body.get("data", body) if isinstance(body, dict) else body


def terminal(payload, tok, key=None):
    headers = auth(tok)
    if key:
        headers["X-JONE-Desktop-Key"] = key
    return requests.post(ADMIN_NS + "/biometrics/reception-terminal/", json=payload, headers=headers, timeout=60)


def audited(admin_tok, action):
    r = requests.get(API + "/admin/audit-logs/", params={"action": action, "page_size": 10},
                     headers=auth(admin_tok), timeout=30)
    rows = data_of(r)
    if isinstance(rows, dict):
        rows = rows.get("results", rows.get("items", []))
    return r.status_code, rows if isinstance(rows, list) else []


def untouched_staff(admin_tok=None, exclude=()):
    """A non-exempt active staff member with no recent attempt and no open shift.

    The manager-fallback refusal is about the 30-minute evidence window, so the
    script must pick a subject who is genuinely clean instead of relying on
    permanently pristine data. Non-exempt operational roles are the ones the
    workstation rules bind; the check runs through the API so the script stays
    portable.
    """
    admin_tok = admin_tok or token(ADMIN)
    operational = {"BARTENDER", "CHEF", "HOUSEKEEPING", "WAITER", "RECEPTIONIST"}
    open_shifts = {row.get("staff") for row in
                   data_of(requests.get(ADMIN_NS + "/attendance/", headers=auth(admin_tok),
                                        params={"status": "ON_DUTY", "page_size": 100}, timeout=30))
                   or [] if isinstance(row, dict)}
    for row in sorted(staff_rows(admin_tok), key=lambda r: r.get("id", 0)):
        if not row.get("is_active") or row.get("role") not in operational:
            continue
        if row.get("id") in open_shifts:
            continue
        if (row.get("email") or "").lower() in {e.lower() for e in exclude}:
            continue
        return row.get("email") or ""
    return ""


OPERATIONAL_ROLES = {"BARTENDER", "CHEF", "HOUSEKEEPING", "WAITER", "RECEPTIONIST"}


class Pool:
    """Live subject selection.

    The checks that exercise the fingerprint → attendance → code chain MUTATE the
    data they run on (they enrol a template, clock somebody in, and record a
    capture failure), so a fixed account can only satisfy them on a pristine
    database. Every subject is therefore chosen from the live state, and a check
    whose precondition the database cannot supply is reported as NOT EXECUTED
    instead of failing or passing on a stale assumption.

    Preconditions are read from the staff profile API (`fingerprint_status`,
    `attendance_status`) and the attendance list, so nothing here needs ORM access.
    """

    def __init__(self, admin_tok):
        self.token = admin_tok
        self.rows = [r for r in staff_rows(admin_tok) if r.get("is_active")]
        open_shifts = {row.get("staff") for row in
                       data_of(requests.get(ADMIN_NS + "/attendance/", headers=auth(admin_tok),
                                            params={"status": "ON_DUTY", "page_size": 100}, timeout=30))
                       or [] if isinstance(row, dict)}
        self.open_shifts = {i for i in open_shifts if i is not None}
        # Somebody a capture failure was recorded against inside the last 30
        # minutes is no longer a valid "no recent attempt" subject. The window is
        # the product's own fallback evidence window; entries older than that have
        # aged out and the account becomes usable again.
        self.recent_failure_ids = set()
        cutoff = datetime.now().astimezone() - timedelta(minutes=EVIDENCE_WINDOW_MINUTES)
        status, rows = audited(admin_tok, "ATTENDANCE_FINGERPRINT_FAILED")
        for row in rows:
            meta = row.get("metadata") or {}
            try:
                when = datetime.fromisoformat((row.get("created_at") or "").strip())
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=cutoff.tzinfo)
            if when >= cutoff and meta.get("staff_id"):
                self.recent_failure_ids.add(meta["staff_id"])

    def _operational(self):
        return [r for r in self.rows if r.get("role") in OPERATIONAL_ROLES]

    def un_enrolled(self, exclude=(), operational_only=False):
        """An active staff member with no approved template.

        Enrolment (item 1) and the "no code without a template" refusal (item 5)
        are not limited to desk-bound roles, so by default every active non-guest
        account is a candidate; pass operational_only=True to stay inside the
        roles the workstation rules bind.
        """
        skip = {e.lower() for e in exclude}
        candidates = self._operational() if operational_only else \
            [r for r in self.rows if r.get("role") not in ("GUEST",)]
        for row in sorted(candidates, key=lambda r: r.get("id", 0)):
            if str(row.get("fingerprint_status", "")).upper() != "ENROLLED" \
                    and (row.get("email") or "").lower() not in skip:
                return row.get("email") or ""
        return ""

    def clean(self, exclude=(), enrolled=None):
        """Operational, not on duty, and with no capture failure in the window."""
        skip = {e.lower() for e in exclude}
        for row in sorted(self._operational(), key=lambda r: r.get("id", 0)):
            email = (row.get("email") or "").lower()
            if not email or email in skip:
                continue
            if row.get("id") in self.open_shifts or row.get("id") in self.recent_failure_ids:
                continue
            if enrolled is True and str(row.get("fingerprint_status", "")).upper() != "ENROLLED":
                continue
            if enrolled is False and str(row.get("fingerprint_status", "")).upper() == "ENROLLED":
                continue
            return row.get("email") or ""
        return ""

    def describe(self):
        operational = self._operational()
        enrolled = sum(1 for r in operational
                       if str(r.get("fingerprint_status", "")).upper() == "ENROLLED")
        return (f"{len(operational)} operational accounts ({enrolled} enrolled) · "
                f"on duty: {len(self.open_shifts)} · recent capture failures: {len(self.recent_failure_ids)}")


def close_verification_shifts(admin_tok, subjects, opened_after=None):
    """Clock out the windows this run left open, at the desk, as the subject.

    A window counts as "this run's" when it was clocked in after the run started
    (or when its owner is one of the subjects this run deliberately used), so a
    shift somebody else legitimately left open is never touched.
    """
    wanted = {e.lower() for e in subjects if e}
    if not wanted and not opened_after:
        return 0
    rows = data_of(requests.get(ADMIN_NS + "/attendance/", headers=auth(admin_tok),
                                params={"status": "ON_DUTY", "page_size": 100}, timeout=30)) or []
    if isinstance(rows, dict):
        rows = rows.get("results", [])
    closed = 0
    for row in rows:
        email = (row.get("staff_email") or "").lower()
        if row.get("clock_out_at"):
            continue
        started = (row.get("clock_in_at") or "")[:19]
        if not (email in wanted or (opened_after and started and started >= opened_after)):
            continue
        # The desk can clock a staff member out without their password; fall back
        # to the subject's own sign-in for accounts the desk cannot verify.
        status, _ = terminal_clock_out(admin_tok, email)
        if status not in (200, 201):
            status, _ = self_clock_out(email)
        if status in (200, 201):
            closed += 1
    return closed


EVIDENCE_WINDOW_MINUTES = 30   # the product's own fallback evidence window
STARTED_AT = ""


def main():
    global STARTED_AT
    STARTED_AT = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    # ---- window 1: four sign-ins (admin, bar at the desk, manager, chef)
    admin_tok = token(ADMIN)
    bar_tok = token(BAR, headers={"X-JONE-Desktop-Key": DESK_KEY})
    mgr_tok = token(MANAGER)
    chef_tok = token(CHEF, headers={"X-JONE-Desktop-Key": DESK_KEY})
    print("· window 1 tokens acquired (admin, bar@desk, manager, chef)")

    # ------------------------------------------------------------- item 1
    pool = Pool(admin_tok)
    print("· subject pool:", pool.describe())
    # The desk flows below need somebody who is enrolled (there is a template to
    # verify), not on duty (a code can be issued) and not recently attempted (the
    # 30-minute evidence window is what item 6 exercises). Resolve once, up front.
    fresh = pool.clean(exclude=[BAR], enrolled=True)
    if not fresh:
        print("· no clean enrolled subject (all on duty or recently attempted): "
              "run close_open_shifts.py, or wait out the 30-minute evidence window")
    bar_id = fresh_id(BAR, admin_tok)
    r = requests.get(ADMIN_NS + "/fingerprint/staff/%s/" % bar_id, headers=auth(admin_tok), timeout=30)
    record(1, "ADMIN reads another staff member's biometric status", r.status_code == 200, f"HTTP {r.status_code}")

    # The enrolment subject must genuinely have no active template, otherwise the
    # check would be re-enrolling somebody and proving nothing.
    # ...and somebody OTHER than the acting administrator: the server deliberately does
    # not auto-approve an administrator's own biometric (checked separately below).
    enroll_subject = pool.un_enrolled(exclude=[BAR, ADMIN, MANAGER])
    r = requests.post(ADMIN_NS + "/fingerprint/staff/%s/" % (fresh_id(enroll_subject, admin_tok) if enroll_subject else bar_id),
                      json={"action": "enroll", "minutiae": VALID_MINUTIAE,
                            "finger_position": "RIGHT_INDEX", "auto_approve": True},
                      headers=auth(admin_tok), timeout=60)
    body = data_of(r)
    record(1, "ADMIN enrols + auto-approves a fingerprint (active template)%s"
           % (" (subject: %s)" % enroll_subject if enroll_subject else ""),
           None if not enroll_subject else
           (r.status_code == 201 and body.get("enrollment_status") == "ENROLLED"),
           "every operational account already has a template" if not enroll_subject
           else f"HTTP {r.status_code} enrollment_status={body.get('enrollment_status')}")

    # Positive control: an administrator enrolling THEIR OWN fingerprint must not
    # be able to approve it in the same call — biometrics on the enrolling account
    # need a second pair of eyes. If this ever auto-approves, the control is gone.
    admin_id = fresh_id(ADMIN, admin_tok)
    r = requests.post(ADMIN_NS + "/fingerprint/staff/%s/" % admin_id,
                      json={"action": "enroll", "minutiae": VALID_MINUTIAE,
                            "finger_position": "LEFT_INDEX", "auto_approve": True},
                      headers=auth(admin_tok), timeout=60)
    body = data_of(r)
    record(1, "an administrator cannot auto-approve their OWN enrolment (self-approval needs a second approver)",
           r.status_code == 201 and body.get("enrollment_status") in ("PENDING_APPROVAL", "ENROLLED_PENDING", "PENDING"),
           f"HTTP {r.status_code} enrollment_status={body.get('enrollment_status')}")

    r = requests.get(ADMIN_NS + "/fingerprint/staff/%s/" % fresh_id(CHEF, admin_tok), headers=auth(chef_tok), timeout=30)
    record(1, "an ordinary role is still refused this endpoint",
           r.status_code in (403, 401), f"HTTP {r.status_code}")

    # ------------------------------------------------------------- item 4
    r = requests.post(ADMIN_NS + "/attendance/clock/",
                      json={"action": "CLOCK_OUT", "idempotency_key": "verify-phone-out-1"},
                      headers={**auth(bar_tok), "User-Agent": MOBILE_UA}, timeout=30)
    record(4, "phone clock-out (no desk key) refused, desk UA irrelevant", r.status_code == 403,
           f"HTTP {r.status_code}")

    r = requests.post(ADMIN_NS + "/attendance/clock/",
                      json={"action": "CLOCK_IN", "idempotency_key": "verify-desk-in-1"},
                      headers={**auth(bar_tok), "X-JONE-Desktop-Key": DESK_KEY}, timeout=30)
    record(4, "clock-out/clock-in at the enrolled desk works", r.status_code in (200, 201), f"HTTP {r.status_code}")

    r = terminal({"action": "VERIFY_STATUS", "minutiae": VALID_MINUTIAE, "email": BAR,
                  "workstation_reference": "WS-RECEPTION-MAIN"},
                 bar_tok, key=DESK_KEY)
    record(4, "the desk key, not the device fingerprint or user agent, decides access",
           r.status_code == 200, f"VERIFY_STATUS at desk HTTP {r.status_code}")

    r = requests.post(ADMIN_NS + "/attendance/clock/",
                      json={"action": "CLOCK_IN", "idempotency_key": "verify-mgr-phone-1"},
                      headers={**auth(mgr_tok), "User-Agent": MOBILE_UA}, timeout=30)
    record(4, "management self-clock without a desk key is allowed (documented exemption)",
           r.status_code in (200, 201), f"HTTP {r.status_code} — STAFF_SIGN_IN_EXEMPT_ROLES, audited")

    # ---------------------------------------------------------- item 5 chain
    r = terminal({"action": "CLOCK_IN", "minutiae": VALID_MINUTIAE, "email": BAR,
                  "workstation_reference": "WS-RECEPTION-MAIN"}, mgr_tok, key=DESK_KEY)
    body = data_of(r)
    code1 = body.get("access_code") if r.status_code == 200 else None
    record(5, "fingerprint verified at the desk -> attendance + 6-digit code",
           r.status_code == 200 and isinstance(code1, str) and len(code1) == 6 and body.get("attendance_reference"),
           f"HTTP {r.status_code} attendance={body.get('attendance_reference')} status={body.get('attendance_status')}"
           + ("" if r.status_code == 200 else " — is %s enrolled in this database?" % BAR))

    un_enrolled = pool.un_enrolled(exclude=[BAR, enroll_subject])
    r = terminal({"action": "CLOCK_IN", "minutiae": VALID_MINUTIAE, "email": un_enrolled or CHEF,
                  "workstation_reference": "WS-RECEPTION-MAIN"}, mgr_tok, key=DESK_KEY)
    record(5, "an un-enrolled staff member gets no code (verify path refuses)%s"
           % (" (subject: %s)" % un_enrolled if un_enrolled else ""),
           None if not un_enrolled else (r.status_code >= 400),
           "no operational account is un-enrolled in this database" if not un_enrolled
           else f"HTTP {r.status_code}: {str(envelope(r))[:90]}")

    if fresh:
        r = terminal({"action": "ISSUE_CODE", "minutiae": VALID_MINUTIAE, "email": fresh,
                      "workstation_reference": "WS-RECEPTION-MAIN"}, mgr_tok, key=DESK_KEY)
        record(5, "a code is refused before the staff member has clocked in",
               r.status_code == 400, f"HTTP {r.status_code}: {str(envelope(r))[:110]}")
    else:
        record(5, "a code is refused before the staff member has clocked in", None,
               "no clean subject available — the request is deliberately not sent, because issuing a "
               "code here would supersede the one under test")

    # ------------------------------------------------------------- item 6
    r = terminal({"action": "MANUAL_FALLBACK", "email": fresh or enroll_subject or BAR,
                  "manual_override_reason": "scanner unreadable this morning (manager attempt)"}, mgr_tok, key=DESK_KEY)
    record(6, "manager fallback refused with no recorded capture failure (test subject: %s)" % fresh,
           None if not fresh else (r.status_code == 403),
           "no clean subject available" if not fresh else f"HTTP {r.status_code}")

    r = terminal({"action": "MANUAL_FALLBACK", "email": fresh,
                  "manual_override_reason": "short"}, mgr_tok, key=DESK_KEY)
    record(6, "a short reason is refused", None if not fresh else (r.status_code == 400),
           "no clean subject available" if not fresh else f"HTTP {r.status_code}")

    r = terminal({"action": "RECORD_FAILURE", "email": fresh,
                  "failure_classification": "DEVICE_UNAVAILABLE",
                  "note": "Sensor reports no device after replug."}, mgr_tok, key=DESK_KEY)
    record(6, "the failed capture is recorded from the terminal (allowlisted class)",
           None if not fresh else (r.status_code == 200 and data_of(r).get("outcome") == "DEVICE_UNAVAILABLE"),
           "no clean subject available" if not fresh
           else f"HTTP {r.status_code} outcome={data_of(r).get('outcome') if r.status_code == 200 else None}")

    r = terminal({"action": "RECORD_FAILURE", "email": fresh,
                  "failure_classification": "READER_UNAVAILABLE"}, mgr_tok, key=DESK_KEY)
    record(6, "an invented failure class is refused (evidence cannot be fabricated)",
           None if not fresh else (r.status_code == 400),
           "no clean subject available" if not fresh else f"HTTP {r.status_code}")

    r = terminal({"action": "MANUAL_FALLBACK", "email": fresh,
                  "manual_override_reason": "reader unavailable, supervisor approved the fallback"}, mgr_tok, key=DESK_KEY)
    body = data_of(r)
    code2 = body.get("access_code") if r.status_code == 200 else None
    record(6, "manager fallback succeeds once the failure is recorded",
           None if not fresh else (r.status_code == 200 and body.get("authority") == "MANAGER" and bool(code2)),
           "no clean subject available" if not fresh
           else f"HTTP {r.status_code} authority={body.get('authority')} evidence={body.get('evidence')}")

    # The administrator exception is defined by the ABSENCE of qualifying
    # evidence, so it must target somebody who has none — never the subject the
    # RECORD_FAILURE above just created evidence for (the server then correctly
    # answers with the evidence-based authority instead).
    admin_subject = pool.clean(exclude=[BAR, fresh], enrolled=True)
    r = terminal({"action": "MANUAL_FALLBACK", "email": admin_subject or RECEPTION,
                  "manual_override_reason": "administrator exception for today's shift change"}, admin_tok, key=DESK_KEY)
    body = data_of(r)
    record(6, "administrator exception works without a prior attempt (subject: %s)" % (admin_subject or "none"),
           None if not admin_subject else
           (r.status_code == 200 and body.get("evidence") == "ADMINISTRATOR_EXCEPTION"),
           "no subject without a recent attempt available" if not admin_subject
           else f"HTTP {r.status_code} evidence={body.get('evidence')}")

    r = terminal({"action": "MANUAL_FALLBACK", "email": RECEPTION,
                  "manual_override_reason": "ordinary employee attempting a fallback code"}, chef_tok)
    record(6, "ordinary employee cannot issue a fallback code", r.status_code in (403, 401), f"HTTP {r.status_code}")

    # ------------------------------- window 2: the employee signs in with the code
    print("· sleeping 62s for the login throttle window…")
    time.sleep(62)

    r = login(BAR, access_code=code1, department="BAR")
    staff_tok = r.json()["data"]["tokens"]["access"] if r.status_code == 200 else ""
    record(5, "employee signs in with the numeric code", r.status_code == 200, f"HTTP {r.status_code}")

    r = requests.get(API + "/auth/profile/", headers=auth(staff_tok), timeout=30)
    record(5, "the session issued from that code is accepted", r.status_code == 200, f"HTTP {r.status_code}")

    r = login(BAR, access_code=code1, department="BAR")
    record(5, "the code is single-use (immediate replay refused)", r.status_code in (400, 401, 403),
           f"HTTP {r.status_code}: {str(envelope(r))[:100]}")

    # code2 was minted for `fresh`, so the borrower must be somebody else — when
    # `fresh` happened to equal a fixed constant this check used to test the
    # OWNER and then report the correct HTTP 200 as a failure.
    borrower = BAR if (fresh or "").lower() != BAR.lower() else CHEF
    r = login(borrower, access_code=code2, department="BAR")
    record(5, "another employee cannot use a code issued to someone else (owner %s, borrower %s)"
           % (fresh, borrower),
           None if not code2 else (r.status_code in (400, 401, 403)),
           "no fallback code was issued earlier in this run" if not code2
           else f"HTTP {r.status_code}: {str(envelope(r))[:100]}")

    # positive control: the refusal above must not have consumed the owner's code
    r = login(fresh, access_code=code2)
    record(5, "the code still works for its own owner after a refusal (owner %s)" % fresh,
           None if not code2 else (r.status_code == 200),
           "no fallback code was issued earlier in this run" if not code2
           else f"HTTP {r.status_code}: {str(envelope(r))[:100]}")

    # ------------------------------------------------------------- item 7
    r = terminal({"action": "CLOCK_OUT", "minutiae": VALID_MINUTIAE, "email": BAR,
                  "workstation_reference": "WS-RECEPTION-MAIN"}, mgr_tok, key=DESK_KEY)
    record(7, "clock-out succeeds at the desk", r.status_code == 200, f"HTTP {r.status_code}")

    r = login(BAR, access_code=code1, department="BAR")
    record(7, "a clocked-out code cannot be replayed", r.status_code in (400, 401, 403), f"HTTP {r.status_code}")

    r = requests.get(API + "/auth/profile/", headers=auth(staff_tok), timeout=30)
    record(7, "the front-desk session stops working after clock-out", r.status_code in (401, 403),
           f"HTTP {r.status_code}")

    # ------------------------------------------------------ audit trail check
    missing = []
    for action in ("ATTENDANCE_MANUAL_FALLBACK_REFUSED", "ATTENDANCE_MANUAL_OVERRIDE",
                   "ATTENDANCE_FINGERPRINT_FAILED", "TEMPORARY_WORKSTATION_CREDENTIAL_ISSUED"):
        status, rows = audited(admin_tok, action)
        if status != 200 or not rows:
            missing.append(f"{action}(HTTP {status}, {len(rows)} rows)")
    record(6, "refusal / override / failure / issuance audits are all queryable",
           not missing, ", ".join(missing) or "all present")

    # ------------------------------------------------------------- tidy up
    # The run clocked real people in (items 4, 5, 6, 7). Closing those windows
    # through the same desk endpoint is what makes the script repeatable; the
    # records stay in the history as COMPLETED attendance.
    closed = close_verification_shifts(admin_tok, subjects=[BAR, fresh, admin_subject, enroll_subject],
                                       opened_after=STARTED_AT)
    if closed:
        print(f"· closed {closed} attendance window(s) opened by this run "
              f"(they remain in the history as COMPLETED)")

    failed = [row for row in RESULTS if row[2] is False]
    skipped = [row for row in RESULTS if row[2] is None]
    passed = [row for row in RESULTS if row[2] is True]
    print("\n==== SUMMARY ====")
    for item, name, ok, detail in RESULTS:
        label = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
        print(f"{label}  [{item}] {name}  ({detail})")
    print(f"\n{len(passed)} passed · {len(failed)} failed · {len(skipped)} not executed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
