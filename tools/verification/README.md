# Verification drivers (`tools/verification/`)

End-to-end drivers for the eleven items in
[`PRODUCTION_AUDIT_REPAIR_REPORT.md`](../../PRODUCTION_AUDIT_REPAIR_REPORT.md).

They are here, in the repository, for one reason: every "verified" claim in that
report should be reproducible by anyone who checks out this commit — against a
running Django API and the static frontend, driving the **real application**
through its own HTTP API and a real headless browser. They are *not* part of the
test suites (`backend/tests/`, `frontend/tests-js/`, `frontend/tests-pwa/`) and
are never run by `run.sh test` or by CI.

## Running them

Start the two servers first (they are ordinary dev servers):

```bash
# terminal 1 — API
cd backend && python manage.py migrate && python manage.py seed_demo && python manage.py runserver 8000
# terminal 2 — static frontend, proxying /api to the API
cd frontend && python dev_server.py 5500 http://127.0.0.1:8000
```

Then export the credentials as environment variables. **Nothing is hardcoded, and
no script will guess:** a missing value is a loud failure, not a default.

```bash
export JONE_VERIFY_PASSWORD='…'        # required — the verification accounts' password
export JONE_VERIFY_DESK_KEY='…'        # required by the attendance-terminal scripts

export JONE_VERIFY_WEB=http://127.0.0.1:5500      # optional (default)
export JONE_VERIFY_API=http://127.0.0.1:8000      # optional (default)
export JONE_VERIFY_PWA=http://127.0.0.1:8080      # optional — only the secret-scan control
export JONE_VERIFY_ARTIFACTS=…                    # optional — screenshots/logs (gitignored)

# seeded role accounts, if your environment names them differently:
export JONE_VERIFY_EMAIL_ADMIN=admin@jonehotel.local      # …_MANAGER, _HR, _RECEPTION, _CHEF, _BAR, …
export JONE_VERIFY_WORKSTATION=WS-RECEPTION-MAIN
```

Use the seeded role accounts of your environment. In this checkout's demo data the
seeded roles share one password while the `demo.*` accounts use another — the
scripts detect that situation rather than assuming it (see *Statefulness* below).

## What each script covers

| Script | Report items | What it does |
|---|---|---|
| `api_verify_items.py` | 1, 4, 5, 6, 7 | The attendance and fingerprint chain end-to-end over HTTP: enrolment permissions, the workstation gate, verify → attendance → numeric code, the fallback authority rules, code lifecycle and clock-out invalidation. 26 checks. |
| `verify_ui_items.py` | 1, 2, 3, 5, 6, 9, 10, 11 | The same flows in headless Chromium against the running frontend: the sidebar identity link, the cleaned-up sign-in page, the terminal UI, the update prompt and its service-worker refresh, the audit-log modal, plus the browser-side capture contract (no fabricating minutiae). 26 checks, screenshots in `artifacts/`. |
| `verify_pos_modes.py` | 8 | `Create draft` in takeaway, bar and restaurant (against a real table session): exactly one order from a double click, no kitchen ticket, cart preserved, refusal when the cart is empty. |
| `verify_room_service_live.py` | 8 | Room service against a real in-house stay (created through the staff booking API), then cleaned up: the draft is cancelled and the booking checked out with the balance preserved. |
| `repro_pos.py` | 8 | The original defect reproduction: an invalid submit must show a visible error, a valid one must return 201 and keep the cart until the server confirms. |
| `negative_control_secret_scan.py` | — | Proves the tightened secret scan in `tests-pwa/test_regression.py` still fails on planted credentials (and stops flagging the legitimate `/api/auth/password/change/` path). |
| `close_open_shifts.py` | — | Maintenance: clocks out attendance windows left open by a verification run, at the desk. Dry run by default; `--apply` to act. |

Everything goes through the application's own API — no ORM writes, no direct SQL,
no script edits or deletes a record. Where a script creates data it is **cancelled
or closed, never deleted**, because attendance and audit history must survive.

### A note on the probe minutiae

`api_verify_items.py` posts a deterministic set of minutiae points to the terminal
endpoint to exercise the server-side verification path. That is an API-level test
harness, not product behaviour: **the product never fabricates biometric data.**
In a browser the capture must come from a real reader bridge
(`window.JoneFingerprintReader.capture()`), or from the explicitly configured
`BIOMETRIC_READER_MODE="simulated"` switch used for demos — see
`docs/ATTENDANCE_WORKSTATION_SETUP.md`. `verify_ui_items.py` asserts that a
missing reader surfaces as a visible error instead of a silent fake success.

## Statefulness: read this before judging a run

The attendance checks mutate what they measure — they enrol a template, clock
somebody in, record a capture failure. Consequences:

* **Subjects are chosen from live state.** `api_verify_items.py` prints a
  `· subject pool:` line and picks a subject that actually satisfies each check's
  precondition (un-enrolled for an enrolment check, not-on-duty for a code to be
  issued, no capture failure inside the 30-minute evidence window for the
  fallback refusal). Requests for a check whose precondition is unmet are
  **deliberately not sent**: issuing a code "just to see" would supersede the code
  being tested.
* **A skipped check is reported as `SKIP` / *not executed*, never as a pass.**
  `14 passed · 0 failed · 11 not executed` is an honest result; the summary line
  and the exit code distinguish the three states.
* **Runs are repeatable within their window.** Each run closes the attendance
  windows it opened, so a second run starts from the same state. The one thing
  that cannot be undone is the 30-minute evidence window: repeat the run sooner
  than that and the fallback checks report *not executed* for the accounts already
  used, because the product is doing exactly what it should — refusing an override
  while a recent attempt is on record.
* If a pool is exhausted, `close_open_shifts.py` shows what is left open and
  closes it (at the desk, as the subject, or with a manager's terminal authority
  for accounts whose password this environment does not use).

## Deleting this directory

Nothing else in the repository imports these files. If you would rather keep the
drivers outside the repository, delete `tools/verification/` — the report's §D
keeps the results, and the test suites in `backend/tests/` and `frontend/` remain
the durable regression coverage.
