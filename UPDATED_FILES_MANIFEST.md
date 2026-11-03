# Exact repository-relative changed files

| Phase | File / original repository location | Reason |
|---|---|---|
| Previous modification | `frontend/js/api.js` | GET read-only return preview |
| Previous new | `frontend/tests-js/test-pos-return-preview-method.js` | Return HTTP method contract |
| Previous new | `backend/apps/finance/management/commands/initialize_financial_policy.py` | Explicit conservative empty-table policy initializer |
| Continuation new | `backend/tests/test_financial_policy_initialization_repair.py` | Fresh/empty/inactive/future policy DB tests |
| Continuation modification | `frontend/dashboard/accounting.html` | Display same received-value metric used by dashboard |
| Continuation new | `frontend/dashboard/menu-print.html` | Missing read-only printable menu page |
| Continuation modification | `frontend/dashboard/barcode-labels.html` | Load existing barcode labels module as expected by print separation test |

Each repository file above is copied in this folder at the same relative path. Other top-level files here are reports and test results, not repository source replacements. No migration added.