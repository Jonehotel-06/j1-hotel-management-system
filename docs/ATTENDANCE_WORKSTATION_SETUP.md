# Receptionist Desktop & fingerprint workstation setup

Operational reference for the attendance chain repaired on 2026-10-14:
**front desk fingerprint clock-in → server-issued 6-digit code → department
workstation sign-in → clock-out (code cancelled)**.

Everything below is enforced on the server. No step depends on the user-agent,
the screen size, or anything else the browser *asserts*. The one address-based
input — a desk registered by the address it reaches the API from (§2b) — uses
the address the server observed, never one the browser supplied.

---

## 1. What a Receptionist Desktop is

| Concept | Where it lives | Enforced by |
|---|---|---|
| Workstation record | `Workstation` rows (Staff → Workstations, `dashboard/terminals.html`) | admin UI, `terminal.manage` |
| Desk key (secret) | Only the desk browser's `localStorage` (`jone.receptionistDesktopKey`); the server stores a SHA-256 hash | `apps/accounts/desktop_policy.py` |
| Desk key transport | `X-JONE-Desktop-Key` request header | `receptionist_desktop_for_key()` |
| Registered address | `Workstation.ip_address` (+ `desktop_ip_registered_at`), set by an administrator | `receptionist_desktop_for_ip()` |
| Sign-in policy | Non-management roles must sign in — and refresh a session — from a desk identified either way: key **or** registered address | `sign_in_policy_applies()`, `resolve_receptionist_desktop()` |
| Attendance policy | Non-management roles must present the key to clock in/out | `receptionist_desktop_required()` + `_assert_reception_workstation()` |

Management roles (`ADMIN`, `MANAGER`, `GENERAL_MANAGER` — `STAFF_SIGN_IN_EXEMPT_ROLES`)
keep their existing device freedom. That exemption is the same constant for
sign-in and for attendance, so the two policies cannot drift apart. It is also
what makes the supervised fallback usable by a manager who is not sitting at the
reception PC.

## 2. Enrolling a desk

A front desk workstation is enrolled in **either** way — both are verified on the
server, and a desk may have both. What matters is that front desk staff can sign
in from that PC; **only the registered address needs nothing typed at sign-in**.

### 2a. By desktop key (per browser)

1. Sign in as an administrator or manager.
2. **Staff → Workstations** (`dashboard/terminals.html`).
3. Create/verify the workstation: department **Front Desk**, status active.
4. Press **Issue desktop key**. The key is shown **once**; copy it immediately.
   (Server action: `POST /api/admin/users/terminals/<id>/desktop-key/`.)
5. On the front-desk PC, in the **Receptionist Desktop enrolment** panel on the
   same page, paste the key and save. It is stored only in that browser's
   `localStorage` and is never rendered again.
6. Confirm the panel reports the browser as enrolled, then verify at the
   attendance console (`Workforce → Reception Biometric Terminal`): the status
   line must read *"This browser is enrolled as a Receptionist Desktop."*

Rotate = press **Issue desktop key** again and re-paste on the desk PC. The old
key stops working immediately (hash replaced). **Clear key** on the workstation
revokes the desk (sign-in for operational staff then fails until re-enrolled).
A key belongs to one browser: a re-imaged PC, a new staff profile or a different
browser loses it, and it must be pasted again — which is exactly the friction
2b removes.

### 2b. By registered address (the PC itself, nothing typed at sign-in)

1. **From the front desk PC**, sign in as an administrator or manager (management
   roles are exempt from the desk policy, so they can sign in from anywhere).
2. **Staff → Workstations**. The enrolment panel shows *"This browser's address
   as the server sees it: `<address>`"* — the server reports it
   (`GET /api/auth/client-address/`), because the address that matters is the one
   the server observes, which behind the deployment's proxy or the hotel's
   carrier NAT is **not** what the PC says its own address is.
3. On the front desk workstation's row press **Register this address** and
   confirm. (Server action: `PATCH /api/admin/users/terminals/<id>/` with
   `{"ip_address": "<address>"}`; the address is stored canonically, and a
   network — `192.168.1.0/24` — may be registered instead of one address when the
   desk's address is handed out by DHCP.)
4. The row now reads *"Receptionist Desktop: … · sign-in from `<address>`"* and
   the *Not enrolled* flag disappears. Front desk staff sign in on that PC with
   **their email and password only** — nothing else.

The row action then offers **Change registered address** and **Clear address**.
Clearing takes the desk back to key-only, so clear it only when the key is (or
will be) installed.

Register the **front desk PC**, never a phone or laptop: every device arriving
from a registered address is treated as that desk. Two consequences worth
stating plainly, because both have bitten real deployments:

* **Shared addresses count as the desk.** If the whole hotel leaves through one
  public address (typical on a single router, and on some mobile carriers), every
  device behind it is inside the desk's address — so read the warning in the
  confirm dialog before registering a wide range (or a `/0`-sized "address").
  Register the exact address, or the LAN range the desk sits in, rather than the
  hotel's whole internet-facing range.
* **A moved desk stops matching**, by design. The refusal names the address the
  request came from (`errors.client_ip`) and the sign-in page shows it, so an
  administrator re-registers the new address in one step. The audit entry
  (`STAFF_SIGN_IN` → `desktop_method: registered_ip`) records which route
  admitted each sign-in, so "it worked yesterday" is answerable.

A phone on mobile data, a guest laptop, or any browser off that address is still
refused, exactly as before.

### 2c. Client address (deployment setting — read this before deploying)

Which address the server believes decides whether a registered desk can sign in,
so this matters on any host with a load balancer in front of Django:

| Setting | Meaning | Default |
|---|---|---|
| `TRUSTED_PROXY_CIDRS` | Addresses whose *connection* may speak for a client | loopback + private ranges |
| `TRUSTED_CLIENT_IP_HEADERS` | Single-value headers that host's edge writes with the client it saw | `true-client-ip,cf-connecting-ip` |

A header is read **only** when the connection itself came from a trusted proxy,
which is what stops a guest from nominating the desk's address. Leave both unset
unless your host needs something different — an unset *or blank* value keeps the
defaults (blank never means "trust nothing"), and `none` turns a rule off
deliberately.

Why not just read `X-Forwarded-For`? Because a chain cannot be trusted from the
left, and on Render (Cloudflare in front) it arrives as

```
X-Forwarded-For: <whatever the client sent>, <real client>, <Cloudflare edge>, <Render 10.x>
```

so the leftmost entry is client-controlled and the entries behind the real
client are the provider's own edge — a **different one per request**, which no
registered desk address could ever match. Render's edge writes `True-Client-IP`
with the address it actually saw and overrides anything the client sent, so that
value wins when the connection came from a trusted proxy (Render connects from
its private `10.x` network, covered by the default). If `True-Client-IP` is ever
absent, the chain is walked from the right as before and the reported source
changes, which the Workstations panel and `errors.client_ip` show honestly.

Verify on the live host before registering a desk:

```bash
# Expect the address of the machine you ran this from, plus which rule named it.
curl -s https://<host>/api/auth/client-address/ -H "Authorization: Bearer <admin-token>"
# {"success":true,"data":{"client_ip":"81.107.0.110","source":"true-client-ip","trusted_proxy_hops":true}}
```

If `source` is `peer` on a host that has a proxy, the proxy's address is not in
`TRUSTED_PROXY_CIDRS` — list it there, or every client looks like the proxy and
no desk address can match.

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

### When no reader is attached at all

A browser with no reader bridge refuses to fabricate a capture: pressing
**Clock In & Issue 6-Digit Code** shows *"No fingerprint reader is available on
this workstation…"* and **no request is sent** — the failure happens on the
client, before the API. Signing in as an administrator does not change that:
the admin privileges are *server-side* authority, they cannot supply the
biometric itself. What the admin gets instead is the fallback above, which needs
no capture.

Note the consequence on such a station: **Clock Out (Expire Code) is also a
capture action**, so it cannot be used either. End a shift from the employee's
own desk sign-in instead — `Workforce → Attendance → Clock out`, which is a
desk-authenticated self-clock and needs no probe; that cancels the access code
and preserves the attendance history. Re-enrol the reader as soon as possible;
until then every clock-in goes through the supervised fallback, which is what it
exists for. (On the fallback card itself the button is *Issue fallback code* and
the record appears immediately as `ON_DUTY`; the code is shown with its TTL.)

One more consequence, verified against the running system: the desk terminal can
only clock somebody **out** whose finger it can verify, so a staff member who has
**no template at all** cannot be clocked out by the terminal (it answers 403).
The fallback will still clock them *in* and issue a code — the intended order is
to enrol staff first and keep the fallback for reader outages — so if a
non-enrolled person is ever clocked in this way, close the window from their own
desk sign-in (`Workforce → Attendance → Clock out`) or enrol them and close it
from the terminal.

There is deliberately **no button on the console** for `RECORD_FAILURE` today —
it is an API action — so a **manager** cannot record device evidence from a
reader-less station and therefore cannot self-serve a fallback there either.
Administrators can, via the exception above. Wiring a *Record capture failure*
control into the console (and a supervised clock-out that does not require a
probe) is the small follow-up this document is flagging, not something the
repair work pretended to fix.

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
      `staff.view` / `staff.manage` capability rows the fingerprint screens need —
      and `0023_workstation_registered_desktop_ip`, the two columns that let a
      desk be identified by address).
- [ ] Every front desk PC signed in at least once from **the desk itself**, its
      address registered (§2b) — *or* an issued desktop key installed in that
      browser (§2a). A desk with neither cannot sign anyone in.
- [ ] `GET /api/auth/client-address/` on the deployed host reports the real
      client address and the rule that named it, not the proxy's (§2c).
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
# Which address does the server see THIS browser from, and by which rule? (admin/mgr)
curl -s https://<host>/api/auth/client-address/ -H "Authorization: Bearer <admin-token>"

# A registered desk signs in with credentials alone (expect 200; no key header)
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<host>/api/auth/login/ \
     -H 'Content-Type: application/json' \
     -d '{"email":"frontdesk@example.com","password":"<password>"}'

# The same request from another address is refused, and says from where (expect
# 403 + errors.client_ip = the address to register for that desk)
curl -s -X POST https://<host>/api/auth/login/ -H 'Content-Type: application/json' \
     -d '{"email":"frontdesk@example.com","password":"<password>"}'

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
`tools/verification/api_verify_items.py`. The desk-by-address route — including
the deployed host's forwarding shape, a forged edge header, refresh, attendance
and the administrator's register/clear actions — is covered by
`backend/tests/test_receptionist_desktop_ip.py`; the browser side (row state,
the register/clear flows, and the refusal shown on the sign-in page) by
`frontend/tests-js/test-desktop-address-ui.js` and
`frontend/tests-js/test-desk-key.js`.
