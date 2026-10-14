# Receptionist Desktop & fingerprint workstation setup

Operational reference for the attendance chain repaired on 2026-10-14:
**front desk fingerprint clock-in → server-issued 6-digit code → department
workstation sign-in → clock-out (code cancelled)**.

Everything below is enforced on the server. No step depends on user-agent, IP
address, screen size or any other client claim.

---

## 1. What a Receptionist Desktop is

| Concept | Where it lives | Enforced by |
|---|---|---|
| Workstation record | `Workstation` rows (Staff → Workstations, `dashboard/terminals.html`) | admin UI, `terminal.manage` |
| Desk key (secret) | Only the desk browser's `localStorage` (`jone.receptionistDesktopKey`); the server stores a SHA-256 hash | `apps/accounts/desktop_policy.py` |
| Desk key transport | `X-JONE-Desktop-Key` request header | `receptionist_desktop_for_key()` |
| Sign-in policy | Non-management roles must present the key to sign in and to refresh a session | `sign_in_policy_applies()` |
| Attendance policy | Non-management roles must present the key to clock in/out | `receptionist_desktop_required()` + `_assert_reception_workstation()` |

Management roles (`ADMIN`, `MANAGER`, `GENERAL_MANAGER` — `STAFF_SIGN_IN_EXEMPT_ROLES`)
keep their existing device freedom. That exemption is the same constant for
sign-in and for attendance, so the two policies cannot drift apart. It is also
what makes the supervised fallback usable by a manager who is not sitting at the
reception PC.

## 2. Enrolling a desk (one-off, per front-desk browser)

1. Sign in as an administrator or manager.
2. **Staff → Workstations** (`dashboard/terminals.html`).
3. Create/verify the workstation: department **Front Desk**, status active.
4. Press **Issue desktop key**. The key is shown **once**; copy it immediately.
   (Server action: `POST /api/admin/staff-accounts/workstations/<id>/desktop-key/`.)
5. On the front-desk PC, in the **Receptionist Desktop enrolment** panel on the
   same page, paste the key and save. It is stored only in that browser's
   `localStorage` and is never rendered again.
6. Confirm the panel reports the browser as enrolled, then verify at the
   attendance console (`Workforce → Reception Biometric Terminal`): the status
   line must read *"This browser is enrolled as a Receptionist Desktop."*

Rotate = press **Issue desktop key** again and re-paste on the desk PC. The old
key stops working immediately (hash replaced). **Clear key** on the workstation
revokes the desk (sign-in for operational staff then fails until re-enrolled).

## 3. Fingerprint hardware

The browser never invents a fingerprint. `js/biometrics.js` obtains a template
from exactly one of:

1. **Reader bridge (production)** — the workstation exposes

   ```js
   window.JoneFingerprintReader = {
     // Resolve with { minutiae: [{x, y, angle, type, quality}, ...] } (ISO/IEC 19794-2),
     // or { cancelled: true }, or reject with an Error carrying a reason.
     capture: async ({ fingerPosition, timeout, purpose }) => ({ minutiae: [...] })
   };
   ```

   Install it from the reader vendor's SDK (USB/HID/WebUSB/WebHID) through a
   small local script tag in the desk pages, or a browser extension. At least
   10 minutiae points are required; the client validates the shape before
   sending, and the server re-validates everything (`validate_minutiae_data`).
   If the reader cannot capture, the operator gets an explicit, actionable
   message — never a fabricated template.

2. **Lab/demo simulation (never in production)** —
   `window.__APP_CONFIG__.BIOMETRIC_READER_MODE = "simulated"` (deployment-owned
   `js/runtime-config.js`). It derives a reproducible template from a seed and
   logs a warning that it is simulating. Because a simulated template is
   indistinguishable from a captured one to the API, enabling this where real
   attendance is recorded would remove the biometric factor. Default is
   `"hardware"`; leave it alone.

Without a reader the biometric pages refuse with *"No fingerprint reader is
available on this workstation…"* and point the operator at the supervised
fallback (§4). The **manager fallback** is the supported answer to a broken or
absent reader, not a simulated fingerprint.

Reader-side notes: keep the sensor clean and the PC awake; the terminal reads
`READER_UNAVAILABLE`-class problems as a **device** failure, which is a
qualifying cause for a manager fallback.

## 4. Supervised fallback (manager / administrator)

`Workforce → Reception Biometric Terminal → Supervised fallback`.

| Actor | Requirement | Audit |
|---|---|---|
| Ordinary staff | Always refused (no fallback authority) | — |
| Manager (`attendance.manage`, exempt role) | A **recorded** capture/reader failure for that staff member within the last 30 minutes (`MANUAL_ATTENDANCE_FALLBACK_WINDOW_MINUTES`), plus a reason of ≥10 characters | `ATTENDANCE_MANUAL_OVERRIDE` (+ `ATTENDANCE_MANUAL_FALLBACK_REFUSED` on refusal) |
| Administrator | Reason of ≥10 characters; no prior attempt needed (privileged exception) | `ATTENDANCE_MANUAL_OVERRIDE` with `evidence: ADMINISTRATOR_EXCEPTION` |

Record the failure first — from the terminal, with the classification the
console supports: `DEVICE_UNAVAILABLE`, `DEVICE_FAULT`, `CAPTURE_TIMEOUT`,
`UNSUPPORTED_DEVICE`, `INTEGRATION_ERROR` (endpoint action `RECORD_FAILURE`, which
writes an `ATTENDANCE_FINGERPRINT_FAILED` audit entry). A plain `NO_MATCH` — a
wrong finger — never qualifies; that is what re-enrolment and supervision are
for. A refused fallback writes **nothing**: no attendance record, no code.

## 5. Attendance codes

* Issued by the server (`mint_temporary_workstation_credential`), bound to one
  staff member **and** their open attendance record: 6 digits, TTL, single use.
* Stored only as SHA-256 (token and code); the raw values are never logged.
* A new issue supersedes the previous active code (`SUPERSEDED`).
* Clock-out cancels the credential (session included) — the code can no longer
  be replayed and a session minted from it stops working; attendance history is
  preserved (`WORKSTATION_CREDENTIALS_REVOKED_ON_CLOCK_OUT`).
* Issuing requires an open attendance record: a code can never exist without the
  clock-in that justifies it.

## 6. Deployment configuration checklist

- [ ] `python manage.py migrate` (adds `0021_staff_records_capabilities` — the
      `staff.view` / `staff.manage` capability rows the fingerprint screens need).
- [ ] At least one active FRONT_DESK `Workstation` with an issued desktop key,
      and every front-desk browser enrolled with that key.
- [ ] A fingerprint reader bridge on each reception PC, or a documented decision
      to use the supervised fallback for reader outages.
- [ ] `js/runtime-config.js` left at the default (`BIOMETRIC_READER_MODE`
      absent/`hardware`).
- [ ] Frontend release bumped so clients pick the new JS
      (`python build.py --bump` updates `version.json`, `sw.js` `CACHE_VERSION`
      and the `?v=` stamps together).
- [ ] Smoke test from a phone on mobile data: the attendance endpoints must
      answer **403** for operational staff (see §7).

## 7. Verification commands

```bash
# Desk is enrolled and the key is right (expect 200 + a code for an enrolled, clocked-in employee)
curl -s -X POST https://<host>/api/admin/staff-operations/biometrics/reception-terminal/ \
     -H "Authorization: Bearer <staff-ops-token>" -H "X-JONE-Desktop-Key: <key>" \
     -H 'Content-Type: application/json' \
     -d '{"action":"ISSUE_CODE","email":"worker@example.com","minutiae":[...]}'

# Personal device must be refused (expect 403, no attendance row)
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<host>/api/admin/staff-operations/attendance/clock/ \
     -H "Authorization: Bearer <operational-staff-token>" -H 'Content-Type: application/json' \
     -d '{"action":"CLOCK_IN","idempotency_key":"phone-test-1"}'
```

The same checks plus the code lifecycle (issue → sign in → single-use replay
refused → clock-out → code dead) are automated in
`backend/tests/test_attendance_workstation_policy.py` and
`tools/verification/api_verify_items.py`.
