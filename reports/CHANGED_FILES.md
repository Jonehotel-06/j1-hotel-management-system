# Changed-files manifest

All paths in the first column are relative to the original project root; the second column is relative to `ai_updates/refactored_system/`. Reports are new deliverables, not pre-existing project files. No unchanged project file is included.

| Original project path | Isolated path | Purpose |
|---|---|---|
| `backend/apps/finance/services/payment_collection_service.py` | `backend/apps/finance/services/payment_collection_service.py` | Exclude refunded deposit from allocatable funds; lock shared payment. |
| `backend/apps/finance/services/guest_receivables_service.py` | `backend/apps/finance/services/guest_receivables_service.py` | Independent unassigned-AR aggregate. |
| `backend/apps/finance/services/procurement_payment_posting_service.py` | `backend/apps/finance/services/procurement_payment_posting_service.py` | Avoid false posted linkage of unrelated PO instalments. |
| `backend/apps/finance/services/statements_service.py` | `backend/apps/finance/services/statements_service.py` | Exclude non-NGN ledger lines from NGN balance sheet and cash flow. |
| `backend/tests/test_finance_repair_isolated.py` (new) | `backend/tests/test_finance_repair_isolated.py` | Five focused regression tests. |
| `reports/GUEST_RECEIVABLES_AUDIT.md` (new) | `reports/GUEST_RECEIVABLES_AUDIT.md` | Guest findings, blockers, historical review. |
| `reports/SUPPLIER_PAYMENT_RECONCILIATION_AUDIT.md` (new) | `reports/SUPPLIER_PAYMENT_RECONCILIATION_AUDIT.md` | Supplier findings, blockers, historical review. |
| `reports/IMPLEMENTATION_REPORT.md` (new) | `reports/IMPLEMENTATION_REPORT.md` | Change rationale and database impact. |
| `reports/TEST_REPORT.md` (new) | `reports/TEST_REPORT.md` | Results and testing limitations. |
| `reports/CHANGED_FILES.md` (new) | `reports/CHANGED_FILES.md` | This manifest. |
| `reports/ROLLBACK_GUIDE.md` (new) | `reports/ROLLBACK_GUIDE.md` | Safe discard and rollback. |
