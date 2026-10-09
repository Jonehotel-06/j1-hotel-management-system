# Implementation report: sign-in restriction, table layout, facial attendance, portal invitations, QR repair

Status legend: **Done** = implemented and tested in this session. **Partial** = backend done, UI or
verification outstanding (see Limitations). Nothing here claims production deployment.

## 1. Summary by feature

| # | Feature | Status | Evidence |
|---|---------|--------|----------|
| 1 | Staff sign-in restricted to Receptionist Desktop | Partial (backend done; terminals panel not browser-checked; clear-on-move model test not added) | `tests.test_receptionist_desktop_sign_in` 30 pass; `tests-js/test-desk-key.js` 3 pass |
| 2 | Table wrapping and layout | Done for the shared CSS/JS and fixture; not re-checked on live dashboard pages | `tests-pwa/test_table_layout.py` OK at 1280 and 390 px; `tests-js/test-table-fit.js` 7 pass |
| 3 | Facial verification for attendance | Partial (backend only: enrollment, gate, outcomes, retention) | `tests.test_staff_face_verification` 24 pass |
| 4 | Guest-portal invitation after check-in | Partial (backend only; no staff or portal UI) | `tests.test_portal_invitations` 15 pass |
| 5 | QR code repair | Partial (backend and admin page done; browser check not run; TTL awaiting confirmation) | `test_service_qr_lifecycle`, `test_service_qr_api`, `test_service_request_pos_handoff`, `test_guest_services` in the full run |

## 2. Root causes

- **Sign-in:** no desktop gate existed. `/api/auth/login/` accepted any active account regardless of role or workstation.
- **Table layout:** a global wrap-anywhere rule on `.table td/th` let auto-layout columns collapse, so amounts, references and dates wrapped. The fixture test first failed on a measurement error (a line-count method that split one line into two because of sub-pixel rounding). The measurement was corrected to cluster line fragments with a font-size tolerance. No CSS change was needed for the fixture to pass.
- **Portal invitation email kind:** `EmailLog.Kind` had no `PORTAL_INVITATION` choice, so `send_email_safe` silently recorded `GENERIC`. This broke idempotency and cooldown counts. Fixed by adding the choice (migration `notifications.0014`, choices only, no schema change) and correcting test fixtures to the real field names (`to_email`, `body`).
- **QR:** no expiry, no HTTPS guard on the public URL, no print or regenerate, a Railway fallback URL in `config.js`, and a test environment that broke the QR guard. The HTTPS guard is now controlled by `SERVICE_QR_REQUIRE_HTTPS`, not `DEBUG`.
- **Facial attendance:** no biometric path existed anywhere in the codebase. Clock-in had no face check.

## 3. Files changed

Repository: `j1-hotel-management-system` (63 modified files, plus the new files below).

**Backend, new:** `apps/accounts/desktop_policy.py`, `apps/accounts/admin_site.py`, `apps/accounts/migrations/0018_workstation_receptionist_desktop_key.py`, `apps/portal/invitations.py`, `apps/portal/views_admin.py`, `apps/portal/migrations/0002_guest_portal_invitation.py`, `apps/guest_services/migrations/0004_service_qr_link_expiry.py`, `apps/notifications/migrations/0014_email_kind_portal_invitation.py`, `apps/staff_operations/face_models.py`, `apps/staff_operations/services/face_service.py`, `apps/staff_operations/views_face.py`, `apps/staff_operations/migrations/0008_face_verification.py`, `apps/staff_operations/management/commands/purge_face_data.py`.

**Backend, modified:** `apps/accounts/{apps,models,serializers,urls_admin,views,views_admin}.py`, `apps/core/exceptions.py`, `apps/bookings/urls_admin.py`, `apps/guest_services/{models,qr_serializers,urls,views}.py`, `apps/guest_services/services/qr_service.py`, `apps/notifications/email_models.py`, `apps/portal/models.py`, `apps/staff_operations/{models,urls,views}.py`, `apps/stays/services.py`, `config/settings/{base,production}.py`.

**Frontend, modified:** `js/auth.js`, `js/config.js`, `css/dashboard.css`, `login.html`, all 38 `dashboard/*.html` pages (table-fit script tag and feature UI on terminals, service-QR, workforce).

**Frontend, new:** `js/table-fit.js`, `tests-js/test-table-fit.js`, `tests-js/test-desk-key.js`, `tests-pwa/fixtures/table-layout.html`, `tests-pwa/test_table_layout.py`.

**Tests, new:** `backend/tests/test_receptionist_desktop_sign_in.py`, `test_portal_invitations.py`, `test_service_qr_lifecycle.py`, `test_staff_face_verification.py`.

## 4. Implementation details

### 4.1 Staff sign-in restriction
- Receptionist Desktop = an active `Workstation` in department FRONT_DESK with an issued key.
- Key is sent in `X-JONE-Desktop-Key`, stored only as a SHA-256 digest (indexed), shown once at issue. Rotation revokes the old key.
- Exempt roles: ADMIN, MANAGER, GENERAL_MANAGER. All other staff must present a valid key at login.
- Refresh is enforced for policy-bound staff (403 `STAFF_SIGN_IN_RESTRICTED` without a valid key). Access tokens last 30 minutes, so a revoked key takes effect within one access-token lifetime.
- Django admin: superusers and active staff not bound by the policy only. Operational roles are refused at the admin login form.
- Rejected attempts are logged with reason and identifiers only (no password, key or token).
- Guest accounts, logout and password reset are unaffected.

### 4.2 Table layout
- `.table td/th` now uses `overflow-wrap: break-word` with `white-space: normal`, not wrap-anywhere.
- `table-fit.js` classifies headings (amount, date, reference, status and similar) and marks their cells `cell-fit` (no wrap). Prose columns keep wrapping. A MutationObserver handles rows added after load.
- `.table-wrap` is keyboard-focusable and scrolls wide tables. Sort, filter, pagination and sticky headers were not changed.

### 4.3 Facial verification (descriptor matching, no liveness)
- **Model:** `FaceTemplate` (status PENDING, ACTIVE, REJECTED, REVOKED, EXPIRED; JSON descriptor of 128 numbers). A DB constraint allows at most one ACTIVE template per staff member. `FaceVerificationAttempt` stores outcome, distance and a scoped source key, never the probe.
- **Enrollment:** self-service request only (`attendance.clock`). Cross-account enrolment is refused and audited (`FACE_ENROLLMENT_DENIED`). A newer request supersedes a pending one. Approval needs `attendance.manage` and a different user (self-approval refused). Approval replaces the previous ACTIVE template. Reject and revoke clear the descriptor immediately.
- **Retention:** approved templates expire after `FACE_TEMPLATE_RETENTION_DAYS` (default 365). Verification also checks expiry. The `purge_face_data` command expires due templates and deletes attempt rows older than `FACE_ATTEMPT_RETENTION_DAYS` (default 365). Run it daily.
- **Clock gate:** `POST /api/admin/staff-operations/attendance/clock/` accepts an optional `face_probe`. When `STAFF_FACE_VERIFICATION_REQUIRED` is true, or a probe or manual reason is sent, verification runs before `clock_attendance`. Outcome codes: `VERIFIED`, `NO_MATCH`, `NOT_ENROLLED`, `TEMPLATE_EXPIRED`, `PROBE_INVALID`, `FACE_REQUIRED`, `MANUAL_OVERRIDE`. Any refusal returns 422 `FACE_VERIFICATION_FAILED` and writes no attendance. Refusals are audited (`ATTENDANCE_FACE_REFUSED`) without biometric values.
- **Idempotency:** the verification source key is derived from staff, action and client idempotency key. A repeat returns the recorded outcome without re-matching.
- **Manual fallback:** only a supervisor (`attendance.manage`) clocking their own shift, with a reason of at least 10 characters. Audited as `ATTENDANCE_MANUAL_OVERRIDE`. Other staff fail closed when verification is required.
- **Matching:** Euclidean distance against the ACTIVE descriptor; passes when distance ≤ `FACE_MATCH_MAX_DISTANCE` (default 0.5).
- **API responses:** enrollment and review responses never include descriptors or probes (asserted in tests).

### 4.4 Guest-portal invitation
- Scheduled from `ensure_stay_for_check_in` via `transaction.on_commit`. Check-in is not rolled back if the invitation or email fails (tested with a provider failure).
- One `GuestPortalInvitation` per stay (unique OneToOne), so repeated check-in does not re-send.
- Delivery status is read from the linked `EmailLog` (SENT, FAILED or NO_EMAIL), not assumed.
- Email contains only the portal login URL, never a token. Recipient is masked in staff responses (`ng*******@example.com` form).
- Staff can view status, resend (cooldown `PORTAL_INVITATION_RESEND_COOLDOWN_MINUTES`, default 5) and copy invitation text via `GET/POST .../bookings/<lookup>/portal-invitation/` (capability `stay.check_in`).
- Portal service catalog: `GET portal/requests/catalog/` lists the enabled categories (`PORTAL_ENABLED_SERVICE_CATEGORIES`).

### 4.5 QR codes
- `ServiceQRLink.expires_at` (nullable, indexed). Expiry uses `SERVICE_QR_LINK_TTL_HOURS` (default 720, clamped to 1 to 8760). Same expiry and revocation rules as portal links.
- Public states: ACTIVE, EXPIRED (410), REVOKED, INVALID. The link is returned only when ACTIVE.
- Public URL is built from `SERVICE_QR_FRONTEND_URL`. HTTPS is required when `SERVICE_QR_REQUIRE_HTTPS` is true. Production sets it true and fails to load on an `http://` URL.
- Admin page (`dashboard/service-qr.html`): download SVG, copy link, print sheet, Print QR, Regenerate and Issue fresh. Staff list, rotate and revoke endpoints exist.

## 5. Migrations

All migrations are additive and reversible. None drop or alter existing columns or data.

| App | Migration | Change | Effect on existing data |
|-----|-----------|--------|-------------------------|
| accounts | 0018 | `Workstation.sign_in_key_hash` (CharField default '', indexed), `sign_in_key_issued_at` (nullable) | Existing workstations have no key until one is issued |
| portal | 0002 | new table `GuestPortalInvitation` | None |
| guest_services | 0004 | `ServiceQRLink.expires_at` (nullable, indexed, callable default) | **Behaviour change:** see Deployment |
| notifications | 0014 | `EmailLog.kind` choices gain `PORTAL_INVITATION` | Choices only; no database change |
| staff_operations | 0008 | new tables `FaceTemplate` (with partial unique constraint) and `FaceVerificationAttempt` | None |

`manage.py makemigrations --check` reports no changes. Production database was not touched and no destructive migration was run.

## 6. Security

- Sign-in: server-side enforcement on login and refresh. Keys stored as digests only and shown once.
- Rejected sign-in and facial attempts are audited without secrets, descriptors or probes.
- Facial data: descriptors are never returned by any API, never logged, cleared on reject/revoke/expiry, and purged by retention. No image is stored. No third-party service receives face data.
- Portal invitation email contains no bearer token.
- QR tokens: stored hashed, high-entropy, expiring and revocable. Public endpoints are throttled (context 60/min, submit 8/min).
- Service-worker and `/api/` caching are unchanged. Private data is not added to any public cache.
- Logs: no passwords, PINs, tokens, keys, descriptors or raw guest personal data in the new code paths.

## 7. Measured performance

**Not measured in this session.** No latency, query-count or N+1 benchmark was run. Full Django suite wall time was 282.7 s for 835 tests on the sandbox. That is a test-suite timing, not an application benchmark. The new list endpoints (face enrollment queue, invitation status, QR list) use pagination. The per-staff face queries are indexed (`staff, status`; unique source key). Verify with production-like data before claiming any figure.

## 8. Test results (actual runs)

| Check | Result |
|-------|--------|
| `manage.py check` | No issues |
| `manage.py makemigrations --check --dry-run` | No changes detected |
| Full Django suite (`manage.py test --parallel 1`) | **835 run, OK, 1 skipped** (baseline before changes: 754 run, OK, 1 skipped). The skip predates this work; its reason was not recorded in this session. |
| Sign-in module | 30 pass |
| Portal invitation module | 15 pass (includes resend, catalog, failure isolation, rollback) |
| Staff face verification module | 24 pass |
| QR modules (four) | 26 pass in the earlier run, and included in the full run |
| Node suite (`node --test tests-js/`) | **99 pass, 0 fail** |
| Playwright table layout (`tests-pwa/test_table_layout.py`) | OK at desktop and phone widths |
| `frontend/validate.py` | ALL OK (65 HTML files, 26 standalone JS files) |

Known gaps in test evidence: the terminals panel, `service-qr.html` (print, regenerate, expiry) and the portal invitation staff UI were not browser-tested, though Chromium is installed.

## 9. Deployment steps

1. Back up the production database. Confirm the backup restores.
2. Deploy code. Run `python manage.py migrate` (all migrations are additive).
3. Set environment variables as needed (defaults are safe):
   - `SERVICE_QR_FRONTEND_URL` must be the public **HTTPS** frontend URL (production raises on `http://`).
   - `SERVICE_QR_LINK_TTL_HOURS` (default 720; awaiting confirmation).
   - `PORTAL_INVITATION_RESEND_COOLDOWN_MINUTES` (default 5), `PORTAL_ENABLED_SERVICE_CATEGORIES`.
   - `STAFF_FACE_VERIFICATION_REQUIRED` (default false), `FACE_MATCH_MAX_DISTANCE` (0.5), `FACE_TEMPLATE_RETENTION_DAYS` (365), `FACE_ATTEMPT_RETENTION_DAYS` (365).
4. **Before enabling sign-in enforcement for front-desk staff:** an administrator must issue a Receptionist Desktop key for each front-desk workstation on the Terminals page and give it to the desk. Until then, front-desk staff cannot sign in. Admins and managers are unaffected.
5. **Existing QR links:** migration 0004 gives every existing link one shared `expires_at` (migration time + TTL). Printed codes stop working at that time. Regenerate or reissue QR codes before then.
6. Schedule `python manage.py purge_face_data` daily (cron or scheduler).
7. For the Vercel frontend, supply `API_BASE_URL` at deploy time through `runtime-config.js` or a `vercel.json` rewrite. Not decided yet.
8. Smoke-test: front-desk sign-in with and without a key; check-in and invitation email with a test booking; QR print and expiry on the admin page.

## 10. Limitations and remaining work

- **Liveness detection: not implemented, not claimed.** Verification is descriptor matching only. A still photo or a printed face may match if the descriptor matches.
- **Facial capture UI: not built.** No browser model is vendored (the library is available on npm but was not added). The clock gate and enrollment endpoints are ready, but the Receptionist Desktop cannot yet capture a descriptor. `STAFF_FACE_VERIFICATION_REQUIRED` therefore stays false until the capture UI is delivered.
- **Camera release: not implemented** (no camera UI).
- **Templates are not encrypted at rest.** The `cryptography` package is not in the requirements; adding it is a decision for the team. Access is limited by the DB and by the API design.
- **Manual fallback** is available only to supervisors clocking their own shift. Other staff fail closed when verification is required. A supervisor-authorised override flow for staff would need design.
- **Portal invitation:** staff status, resend and copy are API endpoints only. No staff or portal UI was built for the enabled-service catalog or request tracking. Request routing and duplicate-order prevention beyond the existing scoped idempotency were not extended.
- **Sign-in:** the terminals panel is syntax-checked only. Clear-on-move at model level and the audit-row assertions for key rotation are not yet written.
- **QR:** browser checks of print, regenerate and expiry on `service-qr.html` not run. The 720-hour default is awaiting confirmation.
- **Table layout:** verified on the fixture page only. Live pages were not re-checked for sort, filter, pagination and keyboard access.
- **Performance:** not measured (see section 7).
- **`docs/SETUP.md`:** referenced by a `config.js` comment but does not exist. Not created.
