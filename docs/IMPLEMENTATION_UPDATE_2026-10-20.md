# J-ONE update audit — 2026-10-20

## Scope and source

Audited and modified the checked-out GitHub repository at `main` (baseline `126c55c`). The implementation remains Django/DRF plus static HTML/CSS/vanilla JS. No production account, live payroll, live inventory, payment gateway, or physical device was used.

This is a **partial, safety-scoped update**, not a claim that every requirement in the supplied master prompt is complete. Where existing records are intentionally immutable or where stock/refund/fulfillment policy is not represented by the current model, this update did not invent a destructive correction path.

## Dependency map reviewed

- Payroll: `staff_operations/payroll_models.py`, serializers, payroll views, payroll services, payroll URLs, payroll dashboard and payroll API/service tests.
- Restaurant POS: POS order/session models, serializers, views/URLs, `table_service`, order service, session/receipt UI, API configuration and POS regression tests.
- Profile images: shared dashboard profile panel and shared UI lightbox.
- Barcode: shared `barcode-scanner.js`, POS barcode input/lookup handoff, barcode service/views and current automated scanner tests.
- Thermal printing: shared `thermal-receipt.js`, receipt tests for 58 mm/80 mm.
- Frontend regression: complete `frontend/tests-js` suite and `frontend/validate.py`.

## Implemented

### Payroll and compensation

- Added a permission-aware **Edit** action to compensation rows. It loads the selected row and submits a new effective-dated compensation record for the same staff account through the existing backend API.
- The form is prepopulated and requires a reason; effective dates are today/future. This intentionally follows the model's append-only compensation rule and does not rewrite prior terms or approved/paid payroll.
- Changed the submitted-run action label from **Review** to **Approve**. It continues to use the existing protected `review` endpoint, which records an approval/rejection decision, enforces capabilities and maker-checker separation, and keeps approval separate from payment.
- Existing payroll runs/lines, stock-shortage evidence and lateness evidence remain immutable or use the existing revocation/replacement process. No arbitrary direct edit endpoint was added.

### POS receipt visibility

- The POS list only renders **Print receipt** for delivered orders whose backend-returned settlement status is `PAID`.
- The print handler checks that status before opening the printer dialog and fetches the order again after the dialog; it then checks that captured tenders cover the full order total. It no longer fabricates a paid amount from the order total.
- Room-service orders remain excluded from this POS thermal receipt action. The general receipt workflow is frontend-driven; there is no new server-side print endpoint in this update.

### Restaurant table transfer

- Added `POST /api/admin/pos/restaurant-table-sessions/<reference>/transfer/` using the existing `restaurant.order.manage` capability.
- The service rejects self-transfer, inactive/missing/undersized destinations, and occupied destinations. It does not silently merge tables.
- Transfer updates the existing open session in a transaction, preserving orders, their price snapshots, payment references and kitchen tickets. An append-only session event and audit action record the origin/destination and order count.
- Repeated successful requests to the same destination are idempotent. Row locks are acquired in stable table order; the existing unique active-table constraint remains a final concurrency guard. Model persistence also rejects direct table changes without the matching audit event.
- Added the table-session UI action and a confirmation form that summarizes the existing session's order/progress/unpaid counts.

### Profile image viewing

- Staff profile images in the staff-profile modal are keyboard/click accessible and open in the shared lightbox. Alt text, dialog semantics, Escape handling, focus return, a focus loop, backdrop close and a broken-image initials fallback are included.
- The underlying staff profile panel no longer closes when Escape is used to dismiss the image viewer.

### Thermal print and barcode operations

- Thermal print text is explicitly solid black, opaque and medium-weight. The 58 mm font remains compact; 80 mm uses a slightly larger font. Existing paper width, receipt wrapping, totals and logo handling are retained.
- Added `docs/BARCODE_SCANNER_OPERATOR_GUIDE.md`. The existing scanner implementation already has keyboard-emulation handling, exact backend lookup and duplicate suppression; automated tests exercise the supported keyboard workflow.
- Removed the stale root-level public manifest because the checked-in frontend contract and manifest tests specify a dashboard-only PWA; dashboard manifest behavior is unchanged.

## Not implemented / owner or design follow-up required

These requirements remain open, and the acceptance criteria for the entire master prompt are therefore **not all met**:

1. **Payroll line editing, stock-shortage correction, lateness correction:** the current models preserve run snapshots and evidence as immutable, and approved/paid history must not be rewritten. Existing rejection/replacement, stock-shortage decision and lateness revocation paths were preserved. A separate, auditable correction/adjustment workflow and its approval policy would need to be designed and tested before enabling edits.
2. **Unfinished-item returns:** no return/waste/refund model or policy currently exists. The system must define manager authorization, opened/partially consumed/unopened stock treatment, bill credits and already-paid refunds before a return endpoint can safely affect inventory or finance. No delete/negative-quantity shortcut was added.
3. **Unified restaurant-table food plus bar ordering:** current table sessions accept `RESTAURANT` orders, while bar orders and kitchen tickets have separate fulfillment modes; one submitted order currently gets one station ticket. Simply adding bar items to the table cart would route some drinks to the kitchen or risk inconsistent stock handling. A per-line fulfillment/ticket design is needed before enabling mixed dining-session orders.
4. **Guest profile image viewing:** this change wires the existing staff-profile modal only; guest-profile image surfaces were not changed.
5. **Room-service receipt gating:** the ordinary POS thermal receipt action does not apply to room-service orders. A separate room-service receipt surface was not modified or certified by this work.
6. **Hardware verification:** no physical USB barcode scanner or 58/80 mm thermal printer was available. Browser/device-specific scanner input, print preview and actual thermal density remain unverified. Printer driver density/speed, scale, paper quality and head condition still need on-site checks.
7. **Database engine:** automated Django tests use the configured isolated test database (SQLite in this environment); no production MySQL server was available. The migration is additive at the Django migration level and must be reviewed/applied in staging before production deployment.

## Migration, API and deployment

- Migration: `backend/apps/pos/migrations/0007_alter_restauranttablesessionevent_type.py` adds the `TRANSFERRED` session-event choice; `type` remains `VARCHAR(20)`. No existing records are reset or reseeded.
- New API contract: `POST /api/admin/pos/restaurant-table-sessions/{reference}/transfer/` with JSON `{ "destination_table_id": <integer> }`.
- Required capability: `restaurant.order.manage` (enforced by DRF permission and explicitly checked in the view).
- Deployment: deploy the code, run `python manage.py migrate` first in staging and verify the live session/order workflows against the production MySQL version. No rollback or destructive data operation was performed here.

## Tests executed (actual results)

- `cd backend && python manage.py test` — **1,638 tests passed; 17 skipped; 0 failures** (436.38 seconds). This included the new transfer cases and all existing backend tests. SQLite test database; it does not prove production MySQL behavior.
- `cd backend && python manage.py check` — **no issues**.
- `cd backend && python manage.py makemigrations --check --dry-run` — **no changes detected** after migration generation.
- `cd frontend && node --test tests-js/` — **561 passed; 1 skipped; 0 failures**.
- `cd frontend && python validate.py` — **70 HTML files and 31 standalone JS files checked; ALL OK**.
- JS syntax checks for `ui.js`, `dashboard.js`, `thermal-receipt.js` — **passed**.
- Physical barcode/thermal printer tests — **not performed**.

The broader application test run validates existing backend coverage, not a manual end-to-end production acceptance run. No production data or transactions were changed.
