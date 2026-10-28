# FRONTDESK_LOGIN_FIX — TEST REPORT

All commands below were actually executed in the sandbox workspace against the repo (base `5aeeb18` + delivered POS fix, with this package's changes applied). Nothing was deployed to production. Dependency line used to provision the sandbox: `pip install -q "Django==5.2.18" "djangorestframework==3.18.3" djangorestframework-simplejwt django-cors-headers drf-spectacular python-decouple python-dotenv dj-database-url Pillow qrcode reportlab openpyxl requests django-storages boto3 whitenoise`.

## Executed runs (exact results)

| # | Command (cwd `backend/` unless noted) | Result |
|---|---|---|
| 1 | `python manage.py test tests.test_frontdesk_login_e2e -v 1` | **35 tests, OK** |
| 2 | `python manage.py test tests.test_auth tests.test_receptionist_desktop_sign_in tests.test_receptionist_desktop_ip tests.test_front_desk_desktop_ip tests.test_attendance_workstation_policy tests.test_attendance_code_persistence tests.test_attendance_off_desk_management tests.test_error_contract tests.test_capabilities tests.test_department_roles tests.test_frontdesk_login_e2e tests.test_payment_config_visibility -v 1` | **309 tests, OK (2 skipped)** |
| 3 | `python manage.py test -v 1` (full backend suite) | **1855 tests: 26 failures + 4 errors + 17 skipped — all pre-existing, zero new** (baseline before this change: same 26F+4E+17S over 1820 tests; the +35 are this package's new tests) |
| 4 | `python manage.py test tests.test_attendance_code_and_visibility` **with the changes**, then again after `git stash` (**without them**) | **25 failures + 1 error in BOTH runs** — identical, i.e. pre-existing and unrelated (that file's fixture enrolls staff with a stale free-text department label `"Operations"`, which the fail-closed department-context check correctly rejects; see "Pre-existing failures" below) |
| 5 | `node --test tests-js/test-frontdesk-login-ui.js` (cwd `frontend/`) | **6 tests pass** |
| 6 | `node --test tests-js/` (cwd `frontend/`, full JS suite) | **638 tests: 635 pass, 3 fail — the same 3 pre-existing failures as before this change** |
| 7 | Inline-script syntax check of `login.html` + `dashboard/settings.html` (node `new Function` on every `<script>` block) | all parse OK |

## Required 17-scenario matrix — where each is covered

| Scenario | Automated evidence |
|---|---|
| 1 Authorized FD employee + correct email/password on registered desktop | `test_authorized_receptionist_password_login_from_the_registered_desk` (route 2), `..._desktop_key_route` (route 1), `..._settings_address_route` (route 3), `..._through_the_production_proxy_chain` (Cloudflare edge-header shape), `..._cdn_range_is_trusted_as_a_proxy` (XFF-walk + `TRUSTED_PROXY_CIDRS` remedy) |
| 2 Wrong password | `test_wrong_password_is_rejected_with_the_credentials_envelope` (401 `UNAUTHORIZED` "Invalid email or password.") |
| 3 Unknown email | `test_unknown_email_is_rejected_identically` (same 401, no account enumeration) |
| 4 Inactive employee | `test_inactive_account_is_rejected_without_revealing_it_exists` |
| 5 Other-department employee denied FD access | `test_cross_department_sign_in_is_refused_for_the_front_desk_context` (403 FORBIDDEN); `test_another_departments_staff_cannot_reach_front_desk_data` (waiter at the desk holds no `booking.read`); positive control `test_a_receptionist_keeps_front_desk_data_access` |
| 6 Unregistered desktop | `test_unregistered_desktop_is_refused_and_names_the_address_to_register` (403 + `errors.client_ip`) |
| 7 Disabled desktop | `test_disabled_front_desk_workstation_stops_admitting_staff` |
| 8 Failed-then-valid login | `test_the_full_desk_sequence_receptionist_admin_receptionist` (steps 1–2) and `test_a_receptionists_failed_login_does_not_block_the_administrator` |
| 9 Receptionist failure ≠ admin blocked | `test_a_receptionists_failed_login_does_not_block_the_administrator` |
| 10 Admin failure ≠ receptionist blocked | `test_an_administrators_failed_login_does_not_block_a_receptionist` |
| 11 Logout → subsequent login | `test_logout_is_idempotent_and_a_fresh_login_works_afterwards`, `test_relogin_after_session_loss_works_from_the_desk` |
| 12 Session expiration recovery | `SessionExpiryRecoveryTests`: refresh at desk (200, new access works), refresh away from desk refused **without consuming the refresh token** (same token still works back at the desk), re-login after logout |
| 13 Actual production API | **Live probes executed this session** (see below); not part of the automated suite by design (no network dependency in tests) |
| 14 Error display accuracy (400/401/403/404/429/500 mapping) | Envelope `code`/`message` asserted for 401 credentials, 403 desk, 403 department, 429 throttle (`RATE_LIMITED`, "Too many…"); 400/404/500 precedence logic unchanged and pinned by pre-existing `tests.test_error_contract` (in run #2) and `frontend/tests-js/test-login-error-states.js` (in run #6) |
| 15 Admin/Manager auth intact | `test_management_signs_in_from_any_device` (ADMIN, MANAGER, GENERAL_MANAGER from any IP), `test_guest_sign_in_is_unaffected_by_the_desk_policy`; plus the 161 pre-existing auth/desktop suites in run #2 |
| 16 Device auth not bypassable via browser storage/forged values | `test_a_forged_desktop_key_from_any_device_is_refused`, `test_a_browser_cannot_nominate_its_own_address_via_edge_headers`, `test_a_browser_cannot_nominate_its_own_address_via_the_forwarding_chain` (server reports the true peer, not the claim); browser-storage edits carry nothing the server trusts (JWT + server-side workstation state only — pinned by pre-existing suites and UI contract test) |
| 17 Attendance/fingerprint workflows intact | `AttendanceWorkflowIntactTests.test_clock_in_issues_a_code_that_signs_the_staff_member_in` (real biometric terminal endpoint → 6-digit code → real login endpoint); plus `tests.test_attendance_workstation_policy`, `tests.test_attendance_code_persistence`, `tests.test_attendance_off_desk_management` in run #2 (all OK) |
| — The fixed dead end (Settings address saved, no workstation) | `test_settings_address_alone_with_no_workstation_is_the_dead_end_now_named` (refusal carries `errors.no_active_front_desk_workstation`; registering the workstation makes the same address work) |
| — New diagnostic | `FrontDeskDoctorCommandTests` (4 tests incl. read-only proof and ambiguity flag) |
| — New Settings payload | `SettingsPayloadTests` (both states) |

## Live production probes (executed 2026-10-28, from this sandbox)

- `GET https://api.jonehotels.com/api/health/live/` → `200 {"success":true,"status":"ok"}`.
- `POST https://api.jonehotels.com/api/auth/login/` (bogus credentials, `Origin: https://jonehotels.com`) → `401 {"success":false,"code":"UNAUTHORIZED","message":"Invalid email or password."}` **with** full CORS headers (allow-origin/credentials, `x-jone-desktop-key`, `x-jone-terminal`, `x-request-id` in allow-headers) — the production API answers the login endpoint correctly for the deployed frontend origin.
- `OPTIONS` preflights: allowed for `jonehotels.com` / `www.jonehotels.com`; `frontdesk.jonehotels.com` does not resolve (NXDOMAIN) — staff use `jonehotels.com/login.html`, which is allowlisted.
- Response fingerprint (`server: cloudflare`, `via: 1.1 varnish`, `x-envoy-upstream-service-time`) shows the chain Cloudflare → varnish → envoy → application host. A supplied `X-Request-ID` was replaced by a server-generated UUID (documented Envoy edge behavior); Northflank documents that its load balancer attaches `X-Forwarded-For` — consistent with the default `TRUSTED_PROXY_CIDRS` (private ranges + Railway CGNAT) resolving the real client address behind that chain.

## Pre-existing failures (present WITHOUT this package — verified by `git stash` rerun)

1. `tests/test_attendance_code_and_visibility.py` — 25 subtest failures + 1 error: the fixture's `enroll()` writes free-text department `"Operations"` to `StaffProfile.department`; the (pre-existing, intentional) fail-closed check `department_context_is_valid` rejects an unrecognized label, so those fixture users hold zero capabilities. The production staff forms only offer valid department codes. This is a stale test fixture, not a login defect; **left untouched** (out of scope — attendance, and "do not modify unrelated functionality").
2. Same 3 JS failures as before this change: deployed-page-count/runtime-config ordering (72-page expectation), "no obsolete production API rewrite" (static hosting config), web-app-manifest scope. Unrelated to login; untouched.
3. `test_deployment_readiness` / finance nightly / email-pipeline errors: environment-dependent (no Redis/Celery in sandbox); pre-existing.

## What could NOT be verified from here (stated honestly)

- **Production database contents**: whether an active FRONT_DESK workstation is registered, what `front_desk_desktop_ip` holds, and which accounts exist/are active. The new `frontdesk_login_doctor` command is the tool to establish this on the server.
- **The configured desktop itself**: no access to the desk browser (localStorage desktop key presence, cached pages, service-worker state).
- **Production staff credentials**: no valid account to complete a real sign-in end-to-end.
- **The real resolved client IP behind the live proxy chain** (Cloudflare → varnish → envoy): inferred safe from documented platform behavior + live headers, but only verifiable on the server — the Settings page's "IP address seen by the server" (or `frontdesk_login_doctor --observed-ip …`) settles it in one step. If that address ever shows as a changing hosting/proxy address, the remedy is configuration only: extend `TRUSTED_PROXY_CIDRS` (e.g. add Cloudflare's published ranges) — pinned as a passing test in this package.
- **No real-browser (Playwright/Selenium) run**: the browser-side behaviour is covered by static contract tests + the unchanged shared binder's own suite, not a live browser session.
- **No deployment was performed** — production remains untouched, per instruction.
