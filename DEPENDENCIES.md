# Batch 07 — DEPENDENCIES

## New dependencies introduced

**None.** No package, library, service, migration dependency beyond the existing
`accounts → 0031` + `audit` chain, and no new environment variable was added.

## Existing infrastructure reused (no changes to these modules)

| Infrastructure | Reused for |
|---|---|
| `apps.accounts.capabilities` (`has_capability`, `ROLE_CAPABILITY_CODES`, persisted `RoleCapability`) | Manager/GM approval authorization |
| `apps.core.permissions.HasCapability` | All five decision endpoints (unchanged gates) |
| `apps.core.approvals.require_independent_approver` | Maker-checker separation (untouched) |
| `apps.audit.services.log_action` + `AuditLog` | Uniform decision evidence (metadata extended only) |
| Per-workflow services (`review_leave`, `approve_stock_count`, `approve_purchase_order`, `review_payroll_period`, `review_expense`) | Approval logic (untouched — Batch 07 calls them as-is) |
| `frontend/js/auth.js` role normalization (`manager` → `general_manager`) | Manager page visibility (pre-existing) |
| `PAGE_ACCESS` registry + `canOpenPage` + `navItemAllowed` | Sidebar/page gating (registry values updated, mechanism untouched) |

## Cross-batch notes (documented, not changed)

* **Leave balances/validation, payroll calculations, inventory postings,
  procurement/supplier workflows, expense posting rules, attendance** — owned
  by other batches; Batch 07 modified none of their logic. Verification tests
  assert their behavior is preserved (amounts unchanged, no silent
  adjustments, validations enforced).
* **Stock-count rejection:** the stock-count workflow has no reject/return
  transition by design (approve-and-post only). Batch 07 did not invent one;
  Task 5 requires only review-and-approve for this category.
* **Retired `MANAGER` title:** new `MANAGER` assignments remain refused by
  `assert_assignable_role` (approved-11-departments consolidation). Batch 07
  grants the five approval capabilities to the `MANAGER` role itself, so any
  account holding that title — past, restored, or reactivated — carries exactly
  the defined approval authority. No hierarchy was created or altered.
* **Frontend capability bootstrap:** approval buttons that are capability-gated
  (`payroll.approve`, `expense.approve`, `procurement.approve`,
  `inventory.adjust.approve`) render from `Auth.loadCapabilities()`; where a
  role fallback exists it names `["admin", "general_manager"]`, which covers
  `MANAGER` accounts via normalization. No fallback list was widened.
