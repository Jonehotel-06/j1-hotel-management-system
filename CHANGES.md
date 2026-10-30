# Batch 02 changes

- Stopped *new* hotel statutory-tax expense/liability accruals for accommodation and POS; removed the dormant statutory calculator and settings API/admin/UI controls. Existing tax snapshots, historic posted balances and payroll obligations remain intact. Preserved historical chart accounts and hotel-settings columns, with no data deletion.
- Corrected signed P&L revenue/expense/discount aggregates and department revenue, including reversals. Retained all supported ledger `REVENUE.*` sources. Labeled all-collection card **Total Collections**, not booking-only. Kept recognized revenue hotel-wide; operating result is distinct from it.
- Netted explicitly linked cash disbursement reversals in the ledger tender total and salary component; staged unpaid supplier obligations remain outside cash disbursed. Corrected Excel's zero-number format.
- Added read-only View on transaction rows. Existing permission-protected detail endpoint now returns reversal reference/history, eager-loaded; modal presents actual header, ledger lines, source/external refs, staff, dates and metadata without edit actions.
- Fixed shared export helper spreading options over the computed start/end/export params. Both pages validate incomplete custom dates, enable native date inputs, reject stale CSV and pass ISO dates for Excel/PDF; occupancy export also sends its own accepted range names.
- Removed Chart of Accounts accounting tab and its loader but retained API/model/chart for manual journals and statements.
- Removed management-command instruction from statement UI. AR aging now includes posted folio charges for every folio in a multi-room booking, including paid bookings with unpaid extras; the normal folio and transaction APIs supply drilldown.
- Receipt form modal cancellation now removes its overlay synchronously, unregisters Escape listener, is idempotent and cannot submit on Cancel.

## Cross-batch boundaries

`backend/apps/pos/services/order_service.py` is touched **only** to remove hotel statutory accrual from POS ledger lines, not to modify POS ordering or inventory workflows. `backend/apps/hotel/{admin,serializers}.py` are touched only for statutory settings removal. No inventory/procurement/booking/staff approval service was modified. Historical `tax_amount` snapshots and ledger account constants are retained deliberately. Payroll statutory/employee handling was not changed.
