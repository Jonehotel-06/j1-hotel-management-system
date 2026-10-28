# FRONTDESK_LOGIN_FIX — CHANGELOG

**Task:** Front Desk staff sign-in with registered email + password from the configured Front Desk desktop.
**Diff baseline:** commit `2131cb0` (= `5aeeb18` "cashier to restaurants" + the separately delivered `JONE_POS_ROOMSERVICE_STOCK_FIX` working-tree changes). All line numbers below were generated from the actual files in this package (`grep -n` after implementation).
**Result:** no change to who can sign in or from where — device authorization is untouched and still enforced. The fix closes a configuration dead end that made a *correctly configured* desktop refuse every password sign-in with advice that could not help, and adds the diagnostics + regression matrix that prove the flow end to end.

---

## Why (root cause, short form)

Every Receptionist-Desktop enrolment route — the browser-installed desktop key, the address registered on a workstation, and the address saved in Settings — requires an **active FRONT_DESK workstation record** to exist first (`Workstation` row, `department=FRONT_DESK`, `is_active=True`). An administrator who follows the Settings page and saves the desk address in `HotelSettings.front_desk_desktop_ip`, but never registered a Front Desk workstation on the Terminals page, has a configuration that *looks* complete while **every** password sign-in at that desk is still refused. The refusal then said only *"register that address … in Settings → Front Desk desktop, or on the workstation itself"* — sending the administrator in a circle, because the address alone can never be enough. The email/password authentication itself (credentials, hashing, envelopes, sessions, throttles, CORS/CSP) was verified correct and is not modified.

---

## Changed files

### 1. `backend/apps/accounts/desktop_policy.py`
- **What:** new helper `front_desk_workstation_missing()`.
- **Where (new lines 389–404):** after `active_front_desk_workstation()` (lines 374–388, unchanged).
- **Why:** one authoritative predicate for "no active FRONT_DESK workstation record exists", reused by the sign-in refusal, the refresh refusal, and the diagnostic command.

### 2. `backend/apps/core/exceptions.py`
- **What:** `StaffSignInRestrictedError` accepts and exposes the new `front_desk_workstation_missing` flag; `errors_payload` emits `errors.no_active_front_desk_workstation: true` when set.
- **Where:** `__init__` signature line 187–188 (new kwarg), line 195 (attribute), `errors_payload` lines 212–218 (new branch). The existing payload keys (`client_ip`, `client_ip_is_infrastructure`, `reason: front_desk_ip_mismatch`, `configured_address_unusable`, `configured_front_desk_address`) and the default message are unchanged.
- **Note:** this file also carries the POS-fix exception classes delivered separately; the copy in this package is the complete current file.

### 3. `backend/apps/accounts/serializers.py`
- **What:** `JOneTokenObtainPairSerializer.validate` passes the new flag when raising the desktop refusal.
- **Where:** import line 21 (`front_desk_workstation_missing` added to the `desktop_policy` import block, lines 19–25); the `raise StaffSignInRestrictedError(...)` in the `desktop is None` branch now includes line 131 `front_desk_workstation_missing=front_desk_workstation_missing(),` (block lines 121–133).
- **Why:** the refusal is raised only after the password has been validated, so the extra structured hint never leaks anything to an unauthenticated caller and does not change the HTTP status (still 403 `STAFF_SIGN_IN_RESTRICTED`) or message text.

### 4. `backend/apps/accounts/views.py`
- **What:** the staff-session refresh refusal (`_refuse_staff_refresh_outside_desk`) includes the same flag.
- **Where:** import line 35 (`front_desk_workstation_missing` in the `desktop_policy` import block, lines 32–37); the `raise` at lines 259–262 (block context lines 244–262).
- **Why:** a staff member whose refresh is refused outside the desk gets the same accurate "which step is missing" information; the refusal still happens before SimpleJWT rotates/blacklists the token, so a refused refresh keeps the session intact (covered by test).

### 5. `backend/apps/hotel/views_admin.py`
- **What:** the admin Settings payload now reports `front_desk_workstation_registered` (bool).
- **Where:** lines 67–76 of `HotelSettingsAdminView._settings_payload` (new block after `front_desk_desktop_ip_unusable`, line 76 is the new payload key).
- **Why:** lets the Settings page warn that the address field alone cannot authorize anything yet, instead of letting the configuration look complete.

### 6. `frontend/dashboard/settings.html`
- **What:** "Front Desk desktop & attendance security" section shows a warning when no active Front Desk workstation is registered.
- **Where:** in `fill()`/notices logic — lines 319 (`var workstationMissing = current.front_desk_workstation_registered === false;`) and 321–328 (new `if(workstationMissing){ ... }` notice block, inserted before the existing `ipIsInfrastructure` notice at line 329+). Existing warnings (infrastructure request IP, unusable saved value) unchanged.
- **Why:** closes the dead end at the place the administrator configures it.

### 7. `frontend/login.html`
- **What:** `desktopRefusalNotice` renders the server's `no_active_front_desk_workstation` flag as a precise instruction.
- **Where:** lines 216–224 (new standalone `if (meta.no_active_front_desk_workstation) { ... }` branch, placed after the opening "reached the server from" line and *before* the address-quality chain `if (meta.client_ip_is_infrastructure) … else if …`, so both kinds of guidance can appear together when both apply).
- **Why:** the refusal now says the workstation record is the missing step and names the exact administrator action (Dashboard → Terminals), instead of repeating "register this address" — which cannot help while no workstation exists. The 6-digit-code fallback line and all other variants are unchanged.

## New files

### 8. `backend/apps/accounts/management/__init__.py` (+ 9. `.../commands/__init__.py`)
Empty package markers so `apps.accounts` can host the management command (the app had no `management/` package before).

### 10. `backend/apps/accounts/management/commands/frontdesk_login_doctor.py`
Read-only `python manage.py frontdesk_login_doctor [--observed-ip <address>]`:
- lists every FRONT_DESK workstation with its state (active/inactive, desktop key issued or not, registered address) and which enrolment routes are ready;
- reports the Settings binding, flags it **INEFFECTIVE** while no active workstation exists, flags **UNUSABLE** hosting/private entries, and checks an observed address against it;
- flags **AMBIGUOUS** duplicate address registrations (the same address on two active front-desk workstations authorizes nobody);
- prints the exact next administrator step and a final `Diagnosis complete. Nothing was changed.`
- never prints or stores a key, digest, token or password; performs no writes.

### 11. `backend/tests/test_frontdesk_login_e2e.py`
35-test end-to-end regression matrix over the real `/api/auth/login/`, refresh, logout, profile, settings and biometric-terminal endpoints. Classes: `HappyPathSignInTests` (all three enrolment routes + production proxy-chain shapes), `CredentialFailureTests` (wrong password / unknown email / inactive — non-enumerating 401), `DesktopEnforcementTests` (unregistered, disabled, the fixed dead end, forged key, forged edge headers, forged XFF, 429 throttle envelope), `DepartmentAuthorizationTests` (cross-department refusal; front-desk data access for receptionist only), `IndependentSessionTests` (the full 6-step shared-desk sequence, independence both ways, idempotent logout, refused refresh ≠ logout), `SessionExpiryRecoveryTests` (refresh at desk, refused away without token loss, re-login), `AttendanceWorkflowIntactTests` (clock-in → 6-digit code → sign-in still works), `FrontDeskDoctorCommandTests` (command output, ambiguity flag, read-only proof), `SettingsPayloadTests` (the new payload key, both states).

### 12. `frontend/tests-js/test-frontdesk-login-ui.js`
6 static source-contract tests: the new refusal branch exists and is additive; the address-quality branches and 6-digit fallback remain; the Settings warning is driven by the new payload flag; the password form still submits only email+password with the shared binder; no fingerprint/attendance code/clock-in is required in the password form (the code tab stays separate).

---

## What was deliberately NOT changed

- No change to `LoginView`, `JOneTokenObtainPairSerializer` credential handling, password verification, hashing, session/JWT issuance, throttling (login stays 5/min per client address), CORS, CSRF, CSP, `runtime-config.js`/`api.js`/`auth.js`, or the trusted-proxy resolution logic — all were verified correct by code inspection, 161 pre-existing auth/desktop tests, and live probes of `https://api.jonehotels.com` (healthy, correct 401 envelope, correct CORS for `jonehotels.com`/`www`).
- No weakening of device authorization: the new flag is *informational*; it changes no authorization decision. Forged-key/forged-header/forged-XFF bypasses remain refused and are now pinned by tests.
- No fingerprint, attendance code, or clock-in requirement added to password authentication; the separate post-auth attendance workflows are untouched and covered by the existing suites.

## Deployment summary

- **Backend:** the 6 backend files above; **no migrations, no new dependencies, no env-var changes required**. (Deploy = copy files & restart the API service.)
- **Frontend:** `frontend/login.html` + `frontend/dashboard/settings.html` only (no JS asset changed, so no `version.json` bump is required; dashboard pages are served `no-store` and Cloudflare Pages revalidates HTML).
- **Owner setup on the server (one-time, ~2 minutes):** run `python manage.py frontdesk_login_doctor`. It names the exact missing step — register an active **Front Desk workstation** in Dashboard → Terminals, then enrol the desk (install its desktop key in the desk browser, or register the desk's address on the workstation / in Settings). Read the desk's real address from the Settings page **opened on the desk** ("IP address seen by the server").
- **No production change or deployment has been made from here.**
