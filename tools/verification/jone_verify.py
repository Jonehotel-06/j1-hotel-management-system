"""Shared helpers for the end-to-end verification scripts.

These scripts drive the REAL application — a running Django API and a running
static frontend — the way an operator would, so they are the load-bearing
evidence for `PRODUCTION_AUDIT_REPAIR_REPORT.md`. They therefore must never
carry credentials in the repository.

Configuration comes from the environment only:

    JONE_VERIFY_PASSWORD     (required)  password of the verification accounts
    JONE_VERIFY_DESK_KEY     (required by the attendance-terminal scripts)
                             the Receptionist Desktop key to present
    JONE_VERIFY_WEB          default http://127.0.0.1:5500   static frontend
    JONE_VERIFY_API          default http://127.0.0.1:8000   Django API
    JONE_VERIFY_PWA          default http://127.0.0.1:8080   PWA test server
    JONE_VERIFY_ARTIFACTS    default ./artifacts next to these scripts

    JONE_VERIFY_EMAIL_ADMIN / _MANAGER / _GENERAL_MANAGER / _HR / _RECEPTION /
    _CHEF / _BAR  override the seeded role accounts below when your environment
    uses different names.

Nothing here writes to the database directly: every state change goes through the
application's own API, and the scripts clean up after themselves where they can
(drafts are cancelled, never deleted).
"""
from __future__ import annotations

import os
import pathlib
import sys
import time

import requests

WEB_BASE = os.environ.get("JONE_VERIFY_WEB", "http://127.0.0.1:5500").rstrip("/")
API_BASE = os.environ.get("JONE_VERIFY_API", "http://127.0.0.1:8000").rstrip("/")
PWA_BASE = os.environ.get("JONE_VERIFY_PWA", "http://127.0.0.1:8080").rstrip("/")
API = API_BASE + "/api"

# Role accounts of the demo/seed data. These are NAMES, not secrets — override
# them with the JONE_VERIFY_EMAIL_* variables to point at another environment.
ADMIN = os.environ.get("JONE_VERIFY_EMAIL_ADMIN", "admin@jonehotel.local")
MANAGER = os.environ.get("JONE_VERIFY_EMAIL_MANAGER", "manager@jonehotel.local")
GENERAL_MANAGER = os.environ.get("JONE_VERIFY_EMAIL_GENERAL_MANAGER", "gm@jonehotel.local")
HR = os.environ.get("JONE_VERIFY_EMAIL_HR", "hr@jonehotel.local")
RECEPTION = os.environ.get("JONE_VERIFY_EMAIL_RECEPTION", "reception@jonehotel.local")
CHEF = os.environ.get("JONE_VERIFY_EMAIL_CHEF", "chef@jonehotel.local")
BAR = os.environ.get("JONE_VERIFY_EMAIL_BAR", "bar@jonehotel.local")

# The workstation the attendance scripts clock in and out at (seeded demo value).
WORKSTATION = os.environ.get("JONE_VERIFY_WORKSTATION", "WS-RECEPTION-MAIN")

DEFAULT_ARTIFACTS = pathlib.Path(__file__).resolve().parent / "artifacts"


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"{name} is required. Export it before running this script; "
                 "see tools/verification/README.md. Refusing to guess a credential.")
    return value


def password() -> str:
    return required("JONE_VERIFY_PASSWORD")


def desk_key() -> str:
    return required("JONE_VERIFY_DESK_KEY")


def artifacts_dir() -> pathlib.Path:
    path = pathlib.Path(os.environ.get("JONE_VERIFY_ARTIFACTS", DEFAULT_ARTIFACTS))
    path.mkdir(parents=True, exist_ok=True)
    return path


def artifact(name: str) -> str:
    return str(artifacts_dir() / name)


def api_login(email: str, password_value: str | None = None, headers: dict | None = None,
              attempts: int = 4, **extra):
    """Sign in, waiting out the 5/min login throttle instead of failing."""
    body = {"email": email, "password": password_value or password()}
    body.update(extra)
    response = None
    for _ in range(attempts):
        response = requests.post(API + "/auth/login/", json=body, headers=headers or {}, timeout=30)
        if response.status_code != 429:
            return response.status_code, response.json()
        print("· login throttled; waiting for the next window…")
        time.sleep(62)
    return response.status_code, response.json()


class Session:
    """An API session whose identity can be injected into a headless browser."""

    def __init__(self, email: str, password_value: str | None = None, desk_key_value: str | None = None):
        import json
        headers = {"X-JONE-Desktop-Key": desk_key_value} if desk_key_value else None
        code, body = api_login(email, password_value, headers=headers)
        assert code == 200, f"login failed {code}: {body}"
        self.user = body["data"]["user"]
        self.tokens = body["data"]["tokens"]
        self._json = json

    def init_script(self) -> str:
        return """
        (function(){
          localStorage.setItem('jone.auth', %s);
          sessionStorage.setItem('jone.session', %s);
        })();
        """ % (self._json.dumps(self._json.dumps(self.user)),
               self._json.dumps(self._json.dumps(self.tokens)))


class Results:
    """Collects PASS/FAIL lines and prints the same format the report quotes."""

    def __init__(self):
        self.rows: list[tuple] = []

    def record(self, item, name, ok, detail=""):
        label = f"[{item}] " if item else ""
        self.rows.append((item, name, ok, detail))
        print(("PASS" if ok else "FAIL"), label + name, "-", detail)

    def summary(self) -> int:
        failed = [r for r in self.rows if not r[2]]
        print(f"\n{len(self.rows) - len(failed)}/{len(self.rows)} checks passed")
        return 1 if failed else 0

def terminal_clock_out(manager_token: str, email: str, workstation: str = WORKSTATION,
                       minutiae: list | None = None) -> tuple[int, str]:
    """Clock a subject out at the Receptionist Desk as a manager/administrator.

    Needs no password for the subject — the operator authenticates and the desk
    verifies the staff member — which is also the only way to close a window for
    an account whose password this environment does not use.
    """
    response = requests.post(
        API + "/admin/staff-operations/biometrics/reception-terminal/",
        headers={"Authorization": "Bearer " + manager_token, "X-JONE-Desktop-Key": desk_key()},
        json={"action": "CLOCK_OUT", "email": email, "minutiae": minutiae or PROBE_MINUTIAE,
              "workstation_reference": workstation},
        timeout=60,
    )
    return response.status_code, response.text[:140]


# Deterministic, structural probe points used ONLY by these API-level scripts to
# exercise the server-side verification path. The product never fabricates
# biometric data: in a browser the capture must come from a real reader (or the
# explicit BIOMETRIC_READER_MODE="simulated" switch used for demos).
PROBE_MINUTIAE = [
    {"x": 100 + i * 5, "y": 150 + i * 4, "angle": (30 + i * 15) % 360, "type": "ending", "quality": 90}
    for i in range(15)
]


def self_clock_out(email: str, password_value: str | None = None) -> tuple[int, str]:
    """The subject clocks themselves out at the desk (needs their password)."""
    code, body = api_login(email, password_value, headers={"X-JONE-Desktop-Key": desk_key()})
    if code != 200:
        return code, f"sign-in failed: {str(body.get('message', body))[:80]}"
    token_value = body["data"]["tokens"]["access"]
    response = requests.post(
        API + "/admin/staff-operations/attendance/clock/",
        headers={"Authorization": "Bearer " + token_value, "X-JONE-Desktop-Key": desk_key()},
        json={"action": "CLOCK_OUT",
              "idempotency_key": f"verify-cleanup-{email.split('@')[0]}-{int(time.time())}"},
        timeout=60,
    )
    return response.status_code, response.text[:140]
