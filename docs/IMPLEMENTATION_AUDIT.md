# Implementation audit — verified completion pass (2026-10-09)

## Scope and method

This pass continued in the authoritative `Jonehotel-06/j1-hotel-management-system`
checkout (`main`, baseline `f598091`); it did not create a second project, database,
or frontend architecture. Django/DRF and the configured central database remain
the source of truth. The public booking site, multi-room booking, Paystack flow,
verified-email guest portal, PWA, staff console, and vanilla HTML/CSS/JS contract
were retained. Existing models and ledgers were extended rather than duplicated.

The repository was inspected across its Django apps, models, services, permission
classes, routes, migrations, frontend API helpers/pages, deployment settings, and
docs. Claims below distinguish automated tests from design intent and production
validation. A passing suite does not mean an external MySQL/provider deployment
was exercised.

## Verified validation

- `backend/manage.py check`: **clean**, no issues.
- `backend/manage.py makemigrations --check --dry-run`: **no changes detected**.
- Full Django suite (after payroll Tax ID/BIK/YTD additions): **749 tests run; 748 passed, 1 skipped; OK**.
- Full Node frontend suite: **89 tests; 89 passed**.
- `frontend/validate.py`: **65 HTML files and 25 standalone JavaScript files**
  checked; all OK. Inline JavaScript in the HTML files was separately checked
  with `node --check`; all passed.
- `frontend/build.py --bump patch`: **67 pages released at version 1.1.11**; version marker and service-worker cache namespace are aligned.
- Python compilation checks and `git diff --check`: passed.
- The Django test run emits a local-environment warning that
  `backend/staticfiles/` does not exist. It does not fail the test run; this
  checkout did not run `collectstatic` as a deployment step.

## Implementation matrix

| Area | Current status | Evidence in this pass | Remaining boundary |
|---|---|---|---|
| Public site, booking, multi-room, payment, guest portal, PWA | **Preserved and regression-tested** | Existing Django quote/availability/payment flows and static frontend remain the base. Full backend suite and frontend tests pass; portal/auth/cache tests remain included. | Payment providers, mail, and production database still require deployment credentials and live smoke tests. |
| Shared identity and authorization | **Implemented, with legacy surfaces** | Individual accounts, persisted role-capability grants, capability endpoint, operational role UI, capability-gated navigation, and API-side permission checks. Cashier, housekeeping, and maintenance no longer inherit booking reads from being staff. `payment.read` provides payment/refund and a separate staff-receipt projection; sending receipts and recording room payments use distinct capability gates. Receipt/payment screens await capability resolution before loading rows/actions. | Some historical booking/admin routes intentionally remain on their existing role allowlists; the capability matrix is additive, not a rewrite of every legacy endpoint. |
| Workstations / attribution | **Implemented as optional metadata** | Registered terminal list/create/update, active/department/search filters, browser selection/clear, request correlation, audit fields and tests. The request's authenticated staff identity remains authoritative. | A terminal reference can be spoofed and is not verified proof of physical presence or a user/session lock. Browser storage is only a convenience for shared-desk attribution. |
| Reception / folios / finance | **Implemented on existing records** | Existing stay/folio/ledger and cashier contracts retained; financial reversals and corrections remain explicit rather than silent edits. Payment/refund reads use `payment.read`; finance can use the staff receipt projection without booking-detail access. Receipt sending (`payment.receipt.send`) and staff room-payment initialization/recording (`booking.manage` + `payment.capture`) are separate, server-enforced actions. Ledger reporting remains separate from legacy projections. | No complete external bank payout/reconciliation integration or universal exported reporting suite. Provider operations and production financial reconciliation still require live validation. |
| Restaurant / bar / kitchen | **Department workflows implemented on shared POS** | Server-priced catalog, service-area controls, kitchen tickets, specialist permissions, manager table register/edit/status, idempotent sessions, multi-check association, audited draft cancellation, and close guards. Eligible anonymous table QR F&B requests can be staff-linked one-to-one to a POS draft; restaurant links require the exact registered table code and matching open session, while bar links preserve free-text location. The public QR form still creates only a request; the staff handoff does not submit or charge. Table code/name/layout freezes after session history; activation remains manageable. No parallel POS ledger. | No table merge/split, bar-specific table registry, or guest self-ordering/QR payment flow. |
| Housekeeping / maintenance / guest services | **Operational flows retained and improved** | Existing assignment/status/event workflow and specialist queues reused. Guest/QR intake routes into existing request records with role and visibility controls. | Room readiness remains based on the existing room/housekeeping projection; this pass does not add minibar/amenity inventory or lost-and-found workflow. |
| Inventory / procurement | **Implemented on shared stock ledger** | Existing supplier, purchase/receipt/count, approval, recipe/consumption and immutable movement APIs/screens remain authoritative and regression-tested. | Supplier invoice settlement and some operational reports remain limited. MySQL concurrency/load behavior was not production-tested. |
| Workforce / payroll | **2026 Nigeria statutory calculations implemented with legal-review gates** | Effective-dated compensation, annual progressive PAYE bands, employee/employer pension, confidential effective-dated Tax ID records with 30-day change-report tracking, benefit-in-kind valuations and statutory exemptions, evidence-backed NHF/NHIS/mortgage/life-insurance claims, rent relief prorated across calendar years and capped, approved in-system or verified opening YTD, maker/checker rule and payroll review, immutable snapshots, ledger accruals and private payslips. Pre-2026 manual payroll semantics remain unchanged. | Legal/compliance reviewers must confirm statutory source applicability, effective dates, employee coverage and treatment before production approval; calculations are configurable, not legal advice. Bank payout/reconciliation/provider integration remains deferred; a manual settlement reference is not proof that a bank transfer occurred. Use replacement/correction runs rather than editing finalized snapshots. |
| Guest/service QR | **Implemented with scope and cache controls** | Random bearer links for room/table use, rotation/revocation, room occupancy derivation, table-to-F&B-only request routing, no-store responses, fragment-only guest bearer handling, admin UI and tests. `/qr-service.html` navigation is network-only in the service worker. Table QR intake remains request-only; eligible requests can be staff-linked to a server-priced POS draft without automatic submission or charge. | QR intake requires connectivity. Guests still cannot select menu lines or place/pay for orders directly. Operators must replace rotated/revoked printed codes. |
| Audit / security / private data | **Hardened, not a substitute for operations controls** | Server-enforced capabilities, request-ID and workstation context, additive audit fields, bounded inputs/lists, no-store responses for sensitive/private APIs, and append-only financial evidence. | Generic audit logging remains fail-soft by design; a durable external security-monitoring/SIEM pipeline and production incident review are outside this pass. |
| Real-time updates | **Visibility-aware refresh, not push** | Existing operational lists use bounded, single-flight, visibility-aware refresh helpers where configured. | No WebSocket/SSE/Channels event transport; refresh latency depends on page/poll interval. |
| Performance / concurrency | **Code-level safeguards; production validation pending** | Pagination, query bounds, selected indexes, `select_related`/`prefetch_related`, idempotency and transactional service paths remain covered by the test suite. | No representative MySQL execution-plan, load, multi-worker race, or failover test was possible in this workspace. Local SQLite tests are not proof of MySQL production behavior. |
| Deployment and docs | **Updated, provider-neutral guidance** | Setup/API/usage docs describe shared API/database configuration, QR origin/cache rules, workforce and terminal attribution. Frontend configuration remains same-origin/provider-neutral. | Operators still need production environment values, TLS/CORS/origin setup, database backups, static collection, mail/provider setup, migration rollout and live smoke tests. |

## Key implementation files

- `backend/apps/accounts/capabilities.py`, `authentication.py`, `views_admin.py` —
  capability/RBAC definitions, authenticated API policy, directory and workstation
  administration. `accounts/migrations/0016_payment_read_and_booking_scope.py`
  seeds separate ledger/receipt-send access and narrows broad booking-read grants.
- `backend/apps/bookings/access.py`, `bookings/views_admin.py`,
  `payments/views.py`, `payments/views_admin.py` — object-scoped booking access,
  the staff receipt projection, and payment/refund/recording capability gates.
  `backend/tests/test_payment_rbac.py` and `test_guest_access.py` exercise the
  authorization boundaries.
- `backend/apps/audit/` and `backend/apps/core/request_context.py` — append-only
  audit/request context and correlation metadata.
- `backend/apps/pos/` — department-aware service areas and kitchen queue, retaining
  shared price/charge/tender rules; `services/table_service.py` owns transactional
  restaurant-table sessions and close guards; `services/order_service.py` supports
  the staff-reviewed, server-priced QR-request-to-draft handoff. Migrations
  `0003_restauranttablesessionevent_restauranttable_and_more.py` and
  `0004_posorder_service_request.py` add the table/session ledger events and the
  one-to-one order association. `backend/tests/test_pos_restaurant_tables.py` and
  `test_service_request_pos_handoff.py` cover lifecycle and handoff controls.
- `backend/apps/guest_services/qr_serializers.py`, `services/qr_service.py`,
  `views.py` — QR bearer validation and request routing; `0003_alter_servicerequestevent_type.py`
  records the new immutable POS-link event. `backend/tests/test_service_qr_api.py`
  verifies public QR intake still rejects direct POS/menu/price fields.
- `backend/apps/staff_operations/payroll_models.py`, `payroll_serializers.py`,
  `payroll_views.py`, `services/payroll_service.py` — confidential TIN records,
  reviewed Nigeria rulesets, YTD statutory calculations, immutable snapshots,
  transitions and payslips.
- `backend/apps/accounts/migrations/0017_payroll_statutory_rule_capabilities.py`
  and `backend/apps/staff_operations/migrations/0004_*` through `0006_*` — rule
  proposal/review grants and payroll statutory/TIN/benefit schema.
- Migrations under `backend/apps/accounts/migrations/`, `audit/migrations/`,
  `guest_services/migrations/`, `pos/migrations/`, `staff_operations/migrations/`,
  and `finance/migrations/` — schema and seeded capability changes.
- `frontend/dashboard/{terminals,payroll,service-qr}.html`, `frontend/qr-service.html`,
  `frontend/js/{api,config,dashboard}.js`, and `frontend/sw.js` — static staff/guest
  surfaces, shared transport, and QR network-only navigation.
- `backend/docs/API.md`, `docs/SETUP.md`, `docs/USAGE.md` — API contract, deployment
  configuration, and operational instructions.

## Not run / release gate

The Playwright-driven browser/PWA suite was not run because Playwright is not
installed in this workspace. The real service-worker/offline/browser interaction
must be exercised in the release environment. The Django runner applied account
migrations through `0017` and staff-operations migrations through `0006` on its
temporary test database; they have not been applied to a persistent workspace or
deployment database. Schedule and review those migrations against the target
MySQL database before release. No live Paystack, email, production MySQL,
external webhook, or real staff-device smoke test was performed. Run those checks
before production rollout. The one skipped Django module is
`backend/tests/test_site_content.py`; it guards policy/image fields removed from
`HotelSettings` and is skipped while those fields are absent. Keep that skip
visible in CI and revisit the tests if the feature is restored.
