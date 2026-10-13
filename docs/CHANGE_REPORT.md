# J-ONE HOTEL & LODGE — Comprehensive Change Report (Priorities 1–6)

## 1. Executive Summary
This change report documents the architectural extensions, bug fixes, security hardening, and UI/UX upgrades completed across the J-ONE HOTEL & LODGE management system (`j1-hotel-management-system`) in strict compliance with production database and settings protection rules.

---

## 2. Priority-by-Priority Implementation & Root-Cause Analysis

### Priority 1: Staff Management, Profiles, Departments & Dashboards (Parts A, B, C)
- **Unified Employee Data Model (Zero Duplication)**:
  - Extended `apps.accounts.models.User` (`middle_name`, `must_change_password`, `password_changed_at`) and `apps.staff_operations.models.StaffProfile` (personal details, emergency contact, probation tracking, reporting supervisor, assigned workstation, default shift template, credential delivery status, separation metadata) via additive migrations `accounts/0020` and `staff_operations/0011`.
  - Created `Department` registry model (`apps.staff_operations.models.Department`) seeded idempotently with all 9 operational departments (`MANAGEMENT`, `ACCOUNTS_FINANCE`, `ADMIN_PROCUREMENT`, `FRONT_DESK`, `KITCHEN`, `RESTAURANT_BAR`, `HOUSEKEEPING`, `PORTERING`, `STORES_INVENTORY`) and mapped to all 10 hotel job positions (General Manager, Accountant, Admin Manager / Procurement, Chef, Kitchen Assistant, Waiters/Waitresses, Porters, Receptionist, Housekeepers, Storekeeper).
  - Created `StaffDisciplinaryRecord` (`apps.staff_operations.models.StaffDisciplinaryRecord`) with full investigation workflow (`OPEN` → `UNDER_REVIEW` → `EMPLOYEE_RESPONDED` → `RESOLVED` / `DISMISSED` / `APPEALED`) and maker-checker payroll deduction posting (`post_disciplinary_deduction_to_payroll`), ensuring allegations never automatically deduct salary without authorized resolution.
  - Created `StaffDocument` (`apps.staff_operations.models.StaffDocument`) for employment letters, contracts, IDs, certifications, reviews, assets, and exit clearances with file size/extension validation and soft-archiving (`is_active=False`).
  - Added `UserCapabilityOverride` (`apps.accounts.models.UserCapabilityOverride`) for administrator-managed per-employee `GRANT` / `REVOKE` capability overrides, integrated into `capability_codes_for_user()`.
- **Dedicated 10-Tab Staff Profile & Admin Consoles**:
  - Built `frontend/dashboard/staff-profile.html` featuring a comprehensive summary header, lifecycle controls (Activate, Suspend, Mark On Leave, Reactivate, Resend Credentials, Require Password Change), and 10 permission-gated tabs:
    1. Overview
    2. Personal Information
    3. Employment & Department
    4. Fingerprint & Attendance
    5. Salary & Payments
    6. Offenses & Disciplinary
    7. Leave & Shifts
    8. Documents & Records
    9. Account, Permissions & Access
    10. Activity & Audit History
  - Upgraded `frontend/dashboard/staff.html` with 4 tabs (Staff Directory with multi-criteria backend filters, Department Registry, Offenses & Disciplinary Register, and Administrator Permission Matrix) and a 4-tab Employee Registration Modal.

### Priority 2: Fingerprint Attendance & Numeric Workstation Access (Parts D, E)
- **Cryptographically Random 6-Digit Numeric Workstation Codes**:
  - Extended `TemporaryWorkstationCredential` (`apps.staff_operations.biometric_models`) with `access_code_hash` (SHA-256 with secret salt), `access_code_last4` (masked suffix), `department`, `attendance_record`, `revoked_at`, and `revoked_reason`.
  - Implemented `generate_unique_numeric_workstation_code()`, `issue_temporary_workstation_credential()`, `authenticate_numeric_workstation_code()`, and `reception_attendance_workstation_action()` in `apps/staff_operations/services/fingerprint_service.py`.
  - Raw fingerprint images are explicitly rejected by `validate_no_raw_fingerprint_payload()`; only ISO/IEC 19794-2 minutiae or standardized numeric feature vectors are stored.
- **Immediate Transactional Expiration on Clock-Out**:
  - Hooked `revoke_workstation_credentials_on_clock_out(staff=staff, reason="CLOCKED_OUT")` directly inside `clock_attendance()` (`apps/staff_operations/services/staff_operations_service.py`) when `action == AttendanceEvent.Type.CLOCK_OUT`.
  - Embedded `workstation_credential_id` and `department` claims into JWT access and refresh tokens issued via numeric access codes (`apps/accounts/views.py`), and enforced real-time active credential + active shift verification on every API request in `WorkstationJWTAuthentication` (`apps/accounts/authentication.py`) and token refresh (`JOneTokenRefreshView`).
- **Reception Attendance Workstation Console**:
  - Added the Reception Attendance & Numeric Access Code Console to both `frontend/dashboard/workforce.html` (accessible to Receptionists, Managers, HR, and Admins) and `frontend/dashboard/terminals.html`.

### Priority 3: Inventory Correctness (Part J)
- **Root Causes Identified & Fixed**:
  1. **Direct POS Delivery Bypass**: POS orders transitioning directly from `SUBMITTED` or `PREPARING` to `DELIVERED` previously bypassed `READY`, skipping `StockConsumptionRequest` creation. Fixed in `apps/pos/services/order_service.py` so any transition to `READY` or `DELIVERED` triggers `_create_inventory_consumption_requests()` idempotently, and `record_pos_payment()` ensures settled orders have processed stock consumption.
  2. **1:1 Direct Stock Item Fallback**: Menu items without an explicit multi-ingredient `StockRecipe` did not deplete matching `StockItem` rows. Added automatic 1:1 `StockRecipe` provisioning in `_create_inventory_consumption_requests()` when a menu item matches an active `StockItem` by SKU or exact name.
  3. **Unit Conversion Support**: Added `convert_stock_quantity()` in `apps/inventory/services/inventory_service.py` supporting `G`↔`KG`, `ML`↔`L`, `CL`↔`L`/`ML`, and `DOZEN`/`CRATE`/`CASE`/`PACK`↔`PCS`/`BOTTLE`/`CAN`/`UNIT`.
  4. **Void/Cancellation Reversals & Ledger Completeness**: Implemented `reverse_pos_stock_consumption()`, `record_opening_stock()`, `record_stock_return()`, and `WRITE_OFF` wastage reasons, and exposed live reconciliation summary metrics (`opening_stock_qty`, `receipts_qty`, `returns_in_qty`, `sales_consumption_qty`, `issues_qty`, `wastage_qty`, `returns_out_qty`, `adjustments_qty`, `expected_gross_sales_value`, `potential_gross_margin_value`) on `StockItemSerializer` and `frontend/dashboard/inventory.html`.

### Priority 4: Payment & User-Action Reliability (Parts G, H, I)
- **Paystack First-Attempt Verification Fix**:
  - *Root Causes*: (a) `frontend/js/api.js` stripped trailing slashes from URLs while Django's `APPEND_SLASH=True` cannot redirect `POST` requests without losing the body; (b) `frontend/js/config.js` referenced `/api/payments/verify/` while `apps/payments/urls.py` only registered `/api/payments/paystack/verify/`; (c) Paystack's transaction status API can briefly report `ongoing`/`pending` immediately after checkout redirect.
  - *Fixes*: Preserved trailing slashes in `frontend/js/api.js`; registered `/api/payments/verify/` (`GET` and `POST`) in `apps/payments/urls.py`; updated `VerifyPaymentView` to accept `reference`, `trxref`, `payment_reference`, `transaction_id`, or `booking_reference` and preserve guest `booking_access_token`; added bounded retry for transient Paystack `ongoing`/`pending` states in `PaymentService.process_verification()`; persisted guest access token across `sessionStorage`, `localStorage`, and `.jonehotel.com` cookie in `frontend/booking-confirmation.html` and `frontend/payment-verify.html`.
- **Duplicate "Send Mail to Guest" Prevention**:
  - *Root Causes*: Rapid double-clicks or network retries could dispatch two concurrent POST requests before the first completed.
  - *Fixes*: Added `sendingInFlight` lock and deterministic `Idempotency-Key` in `frontend/js/receipt-email-modal.js`; added `transaction.atomic()` row lock (`select_for_update`), `Idempotency-Key` check, and 30-second duplicate `SENT` window guard in `AdminBookingSendReceiptView` (`apps/bookings/views_admin.py`).
- **First-Click Operational Button Reliability**:
  - *Root Causes*: Concurrent 401 token refreshes raced against each other in `frontend/js/auth.js`; search input debounce timers overwrote URL-prefilled booking selections on `check-in.html` and `check-out.html`; error paths in several dashboard pages exited before `JONE.releaseGuard(btn)`.
  - *Fixes*: Implemented single-flight `refreshPromise` mutex in `frontend/js/auth.js`; cleared pending debounce timers on booking selection in `check-in.html` and `check-out.html`; wrapped action handlers in `try ... finally { JONE.releaseGuard(btn); }`.

### Priority 5: Public Website & Department Subdomains (Part F)
- **Public Footer Cleanup**:
  - Removed `<a href="login.html">Staff</a>` from `frontend/components/footer.html` and rebuilt all 70 static HTML pages via `python3 build.py`.
- **11 Department Subdomains & Isolation**:
  - Configured `DEPARTMENT_SUBDOMAINS` in `frontend/js/config.js` and hostname-aware routing in `frontend/js/auth.js`, `frontend/index.html`, `frontend/login.html`, and `frontend/js/dashboard.js` for:
    - `manager.jonehotel.com`
    - `accountant.jonehotel.com`
    - `admin.jonehotel.com`
    - `procurement.jonehotel.com`
    - `frontdesk.jonehotel.com`
    - `kitchen.jonehotel.com`
    - `restaurant.jonehotel.com`
    - `bar.jonehotel.com`
    - `housekeeping.jonehotel.com`
    - `porter.jonehotel.com`
    - `store.jonehotel.com`
  - Enforced cross-department rejection on both backend (`assert_staff_authorized_for_department()` and `LoginView`) and frontend (`Auth.isUserAllowedOnSubdomain()` and `Auth.boot()`).

---

## 3. Modified & Added Files Inventory

### Backend (`/backend`)
- `apps/accounts/models.py` — Added `middle_name`, `must_change_password`, `password_changed_at` on `User`; created `UserCapabilityOverride`.
- `apps/accounts/migrations/0020_user_middle_name_user_must_change_password_and_more.py` — Additive schema migration.
- `apps/accounts/capabilities.py` — Integrated per-user `GRANT`/`REVOKE` overrides into `capability_codes_for_user()`.
- `apps/accounts/authentication.py` — Enforced real-time `TemporaryWorkstationCredential` active status & clock-out check on JWT requests.
- `apps/accounts/serializers.py` — Extended `UserProfileSerializer`, `AdminUserListSerializer`, `AdminUserCreateSerializer`, and `LoginSerializer` (supporting `access_code` / `numeric_code` and `department`).
- `apps/accounts/views.py` — Updated `LoginView` and `JOneTokenRefreshView` for 6-digit numeric workstation codes, department portal enforcement, and clock-out session invalidation.
- `apps/accounts/views_admin.py` & `apps/accounts/urls_admin.py` — Added `select_related("staff_profile")` on user list, `AdminUserAccountActionView`, `AdminPermissionMatrixView`, and `AdminUserPermissionsView`.
- `apps/staff_operations/models.py` — Extended `StaffProfile`; created `Department`, `StaffDisciplinaryRecord`, and `StaffDocument`.
- `apps/staff_operations/biometric_models.py` — Extended `TemporaryWorkstationCredential` with `access_code_hash`, `access_code_last4`, `department`, `attendance_record`, `revoked_at`, and `revoked_reason`.
- `apps/staff_operations/migrations/0011_staffprofile_assigned_workstation_and_more.py` — Additive schema migration.
- `apps/staff_operations/services/fingerprint_service.py` — Implemented 6-digit numeric code generation, biometric status summary, reception workstation attendance actions, and immediate clock-out credential revocation.
- `apps/staff_operations/services/staff_operations_service.py` — Hooked `revoke_workstation_credentials_on_clock_out` into `clock_attendance()` on `CLOCK_OUT`.
- `apps/staff_operations/services/payroll_service.py` — Implemented `post_disciplinary_deduction_to_payroll()`.
- `apps/staff_operations/serializers.py`, `apps/staff_operations/views.py`, `apps/staff_operations/views_biometrics.py`, `apps/staff_operations/urls.py` — Added Department CRUD, Department Dashboard Summary, Comprehensive 10-Tab Staff Profile API, Disciplinary CRUD/Resolve/Post-Deduction APIs, Staff Document APIs, and Reception Attendance Workstation APIs.
- `apps/inventory/services/inventory_service.py`, `apps/inventory/serializers.py`, `apps/inventory/views.py`, `apps/inventory/urls.py` — Added unit conversion, POS void/cancellation stock reversal, opening stock, inward/outward returns, and full stock equation reconciliation fields.
- `apps/pos/services/order_service.py` — Triggered inventory consumption handoff on direct `SUBMITTED`/`PREPARING` → `DELIVERED` transitions, payment settlement, and automatic 1:1 SKU/name recipe fallback; reversed stock on `CANCELLED`/`VOIDED`.
- `apps/payments/urls.py`, `apps/payments/views.py`, `apps/payments/services/payment_service.py` — Added `/api/payments/verify/` (`GET`/`POST`), multi-parameter lookup, guest token preservation, and bounded Paystack `ongoing`/`pending` retry.
- `apps/bookings/views_admin.py` — Added atomic row lock, `Idempotency-Key` check, and 30-second duplicate window to `AdminBookingSendReceiptView`.
- `tests/test_site_content.py` — Replaced module-level `SkipTest` with `@unittest.skipIf` for Python 3.13 `--parallel` compatibility.
- `tests/test_enterprise_upgrade_suite.py` — Added comprehensive end-to-end backend regression tests covering Priorities 1–5.

### Frontend (`/frontend`)
- `components/footer.html` & 29 built public HTML pages — Removed public staff login link and fixed footer heading hierarchy.
- `build.py` & `vercel.json` — Normalized script paths for subdirectory pages (`dashboard/`, `portal/`) and restored clean `/api/:path*` proxy rewrite.
- `js/config.js` — Added `DEPARTMENT_SUBDOMAINS` (11 subdomains) and new staff, department, disciplinary, document, biometric, and inventory endpoints.
- `js/api.js` — Preserved trailing slashes on API endpoints and added helper methods for all new endpoints.
- `js/auth.js` — Added single-flight token refresh mutex, subdomain context detection, cross-department guard, and 6-digit numeric access code login.
- `js/dashboard.js` — Added department summary KPI banner (`renderDepartmentSummaryBanner`) and expanded role navigation.
- `js/biometrics.js` — Added Reception Attendance Workstation & numeric access code helpers.
- `js/receipt-email-modal.js` — Added single-flight submission lock and deterministic `Idempotency-Key`.
- `login.html` & `index.html` — Added 3-mode staff authentication (`Email & Password`, `6-Digit Access Code`, `Biometric Scan`) and subdomain-aware routing.
- `booking-confirmation.html` & `payment-verify.html` — Persisted guest booking access token across storage + `.jonehotel.com` cookie and added bounded verification polling.
- `dashboard/staff-profile.html` — New dedicated 10-tab Employee Profile & Administration page.
- `dashboard/staff.html` — Upgraded with Staff Directory filters, Department Registry tab, Offenses & Disciplinary Register tab, and Admin Permission Matrix tab.
- `dashboard/workforce.html` & `dashboard/terminals.html` — Added Reception Attendance Workstation & 6-Digit Numeric Code Issuance console.
- `dashboard/inventory.html`, `dashboard/pos.html`, `dashboard/check-in.html`, `dashboard/check-out.html`, `dashboard/booking-details.html`, `dashboard/enquiries.html`, `dashboard/payments.html`, `dashboard/missed-bookings.html`, `dashboard/service-requests.html`, `dashboard/procurement.html`, `dashboard/accounting.html` — Added inventory opening/return/wastage modals, POS kitchen/bar/waiter role support, first-click reliability fixes, and department summary banners.
- `tests-js/test-enterprise-upgrade-ui.js` & `tests-js/test-expense-details-ui.js` — Added frontend automated regression tests for Priorities 1–5.

---

## 4. External Dependencies & Infrastructure Configuration List
No new third-party Python or Node packages are required. To enable full production functionality across all subdomains and hardware devices, ensure the following infrastructure items are configured:
1. **DNS & SSL Wildcard / Subdomain Records**:
   - Configure DNS `A` / `CNAME` records and TLS certificates for `manager.jonehotel.com`, `accountant.jonehotel.com`, `admin.jonehotel.com`, `procurement.jonehotel.com`, `frontdesk.jonehotel.com`, `kitchen.jonehotel.com`, `restaurant.jonehotel.com`, `bar.jonehotel.com`, `housekeeping.jonehotel.com`, `porter.jonehotel.com`, and `store.jonehotel.com` pointing to the frontend deployment.
2. **Database Migrations**:
   - Run `python manage.py migrate --noinput` on deployment to apply `accounts.0020_user_middle_name_user_must_change_password_and_more` and `staff_operations.0011_staffprofile_assigned_workstation_and_more`.
3. **Receptionist Desktop & Biometric Hardware**:
   - Register the Front Desk workstation in `dashboard/terminals.html` (`department=FRONT_DESK`) and save the one-time Receptionist Desktop key (`X-Receptionist-Desktop-Key`) in the Reception browser's Desk Setup panel.
   - Connect the USB/network fingerprint scanner bridge emitting ISO/IEC 19794-2 minutiae templates to the local WebUSB/localhost helper or configure `FINGERPRINT_DEVICE` webhook credentials.
