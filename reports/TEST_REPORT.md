# Test report

Test environment: isolated clone of `b613cd20e8de7b6a610a6145ff02392596122a90`, Python 3.13, Django 5.2.18, Node 20.20.2, Django temporary SQLite test database. No live DB was connected. Dependencies installed from repository requirements. `python manage.py check` passed and `makemigrations --check --dry-run` returned no changes. `git diff --check` passed.

## Passed backend
- Final targeted run: **170 passed**, 0 failed: `python manage.py test tests.test_finance_repair_isolated tests.test_guest_receivables_batch01 tests.test_guest_receivable_settlement_states tests.test_folio_balance_classification tests.test_finance tests.test_checkout_folio_clearance tests.test_critical_repair_integration tests.test_procurement_payment_settlement_link tests.test_accounting_agent_supplier_payments tests.test_expenses tests.test_supplier_payment_visibility tests.test_accounting_inventory_reconciliation tests.test_accounting_agent_procurement_payables --noinput`.
- Focused statements run after all edits: **35 passed**, including new USD-vs-NGN GL test, existing statements, accounting batch and guest-check suites: `tests.test_finance_repair_isolated tests.test_payments_statements_exports_laundry tests.test_accounting_batch01 tests.test_guest_receivable_check`.
- Broader operational run before final statement-currency change: **191 passed**, 0 failed: `tests.test_guest_deposit_deferral tests.test_bookings tests.test_booking_balances tests.test_multi_room_booking tests.test_stays tests.test_pos tests.test_pos_api tests.test_pos_room_service_availability tests.test_pos_tender_balance tests.test_night_audit_journals tests.test_payments tests.test_payments_statements_exports_laundry tests.test_purchase_order_payments tests.test_expense_payment_evidence tests.test_revenue_posting_confirmations`. The changed statements were retested afterward in the 35-test run. These passes do not prove all requested cases, notably partially occupied multi-room accommodation posting or external-settlement duplicate prevention.
- New tests post real balanced test ledger entries: refunded deposit partially/full not spent again; allocated refund reopens AR; unproven PO instalment not marked posted; NGN statement excludes USD. Existing tests cover a room-service charge, room/extra-charge separation, deposit deferral, posted-only report cases, approvals and settlement idempotency. Synthetic data are not historical hotel reconciliation.

## Intermediate backend failure
The first 169-test targeted run had **1 failure**: `tests.test_supplier_payment_visibility.SupplierPaymentVisibilityTests.test_the_whole_supplier_block_costs_two_statements_not_one_per_measure`, 16 queries vs budget 15 after an extra reconciliation query. Changed to a conditional aggregate in the existing control query; the final 170-test run passed including that query-budget test.

## Frontend JavaScript
Ran `node` on the 12 distinct `frontend/tests-js/test-*accounting*.js`, `test-*expense*.js`, `test-*supplier*.js`, and `test-*night-audit*.js` matches. Nine files passed; **three failed**. No frontend source or tests were modified by this overlay, so these failures are on the same frontend contents as the repository snapshot:
- `test-accounting-posted-transactions-removal.js`: 6 of 10 assertions fail because the page still includes a Posted transactions section and associated paths/journal placement.
- `test-night-audit-accounting-ui.js`: 1 of 8 assertions fails on accounting night-audit rendering expectations.
- `test-dashboard-expenses-ui.js`: 1 of 2 assertions fails, expecting an accounting goods-received KPI.
The night-audit file matched two globs and ran twice with the same failure; it is counted once above. These are not silently counted as passes. No UI fix was attempted without resolving conflicting existing expectations.

## Remaining validation
No full project suite, MySQL concurrency/race tests, browser E2E, real provider/bank/cash evidence, production data scan or exhaustive scenario matrix was run. Multi-room partial occupancy finance, supplier external-payment settlement without double payment, account reclassification for credit balances, other currencies' statements and actual cash/AP reconciliation remain blockers. Passing tests cannot verify unrelated working functionality in every situation.
