# Implementation report

## Delivery decision
**Partial, isolated repair; not a production-ready completion.** Original repository HEAD `b613cd20e8de7b6a610a6145ff02392596122a90`. Cloned to `/home/user/j1-isolated-work`; no original project existed in the workspace and no original source folder was modified. Output contains only modified/new files and reports, preserving paths. No automatic copy, deployment, migration, production database access or historic correction occurred.

## Changes
1. `backend/apps/finance/services/payment_collection_service.py`: subtract immutable refund applications against unallocated deposits when computing available amount in both allocation paths. Lock the payment in `ensure_processed_refund` as the allocation paths already do. Does not subtract refunds of allocated money twice, does not invent a ledger movement, and leaves idempotency/approval workflows intact. Existing histories are not corrected by this change.
2. `backend/apps/finance/services/guest_receivables_service.py`: compute no-folio AR independently as a conditional aggregate over posted lines; compare it to control and all folio balances. Same query budget and output contract.
3. `backend/apps/finance/services/procurement_payment_posting_service.py`: stop attributing the same PO's unrelated posted expense to a different procurement payment. Require actual posted status and a matching, directly sourced handover for automatic POSTED status; flag ambiguous associations for review. This does not post a payment or verify the bank.
4. `backend/apps/finance/services/statements_service.py`: restrict the NGN-labelled balance sheet and cash-flow queries to NGN, including opening/closing and cash-flow transaction lines; expose additive currency-basis metadata. Foreign entries remain in ledger and are excluded, not converted. No foreign currency statement exists in these APIs.
5. `backend/tests/test_finance_repair_isolated.py`: five focused tests for partial/full refunded deposits, refund of allocated money, distinct PO instalments, and mixed-currency statement exclusion.

No frontend or configuration changes, schema changes, migrations, posted transaction mutations, approval permission changes or new accounting architecture. `makemigrations --check --dry-run` reports no changes. SQLite was used for test databases, not the production MySQL engine; row locking/concurrency not established by these tests.

## Deployment blockers
- Correct partial-room nightly accommodation accounting and establish accurate per-room timestamps/pricing (see guest audit). Existing full-booking nightly charge can overstate earnings for partial arrival.
- Safely reconcile already-executed external supplier payments to payable records without a second disbursement, including partials, supporting evidence and mandatory approvals; address stale statuses and UI traceability (see supplier audit).
- Actual production records, reconciliation proofs and historical adjusting entries have not been examined. Balance-sheet credit reclassification, detailed scenario coverage and frontend failures remain open. Do not apply this overlay directly to production until these are addressed, independently reviewed, tested against a secure copy and approved.
