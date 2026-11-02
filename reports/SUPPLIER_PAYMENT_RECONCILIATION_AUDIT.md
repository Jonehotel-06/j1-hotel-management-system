# Supplier-payment reconciliation audit

**Status: PARTIAL; do not assume the reported payment was posted or paid.** Repository snapshot `b613cd20e8de7b6a610a6145ff02392596122a90`; actual production Procurement, bank, approval and ledger data unavailable.

## Why the Accounting record can be missing
`inventory/services/inventory_service.record_purchase_order_payment` appends an operational payment record and calls `finance/services/procurement_payment_posting_service.reconcile_procurement_payment_with_accounting`. That function intentionally returns `ledger_posting_created: False`: it is **not a posting function**. Without a verified explicit executed Accounting settlement, it links an existing expense or creates one DRAFT handover when the actor has capability. Accountant must prepare and submit; an independent authorized approver must approve; accountant posts. If actor lacks the handover capability it marks review required. Consequently a payment can remain operational evidence, a draft, submitted, rejected, approved but unposted, or posted but not linked/refreshed. A payment record alone does not establish bank success. Which applies to the actual reported payment cannot be determined without its reference and data.

Existing `expense_service.post_expense` posts inventory debit to `ASSET.INVENTORY`, operating debit to `EXPENSE.OPERATING`, and credit payable or selected tender. An on-account settlement debits AP and credits tender, through existing approval/payment controls. Goods receipts update physical inventory separately. `supplier_payment_service` uses controlled request, review and execute; no permissions were changed.

## Corrected false reconciliation claim
Previously the reconciliation linked the first expense on the PO and claimed **every** PO payment was POSTED if that expense had a money-out transaction, even when the payment was a different instalment. It now requires posted transaction status and a matching source payment identity, amount, currency, tender and supplier reference for an automatically matched direct handover. Mismatched instalments and unproven existing posted expense/settlement matches are marked `REQUIRES_REVIEW` and are not assigned a transaction reference. Explicitly linked executed Accounting settlements continue through the existing MIRRORED path. Repeating reconciliation makes no expense, ledger or stock transaction. Status/reference fields may be updated on the operational record as before; history of financial transactions is not altered.

## Remaining limitations and required review
- This is **not a complete supplier-payment posting repair**. An existing payable plus a separately executed external payment is not automatically settled by this overlay; additional controls/evidence review are needed. Do not execute a second bank transfer as a workaround. `supplier_payment_service.py` / `expense_service.py` need an approval-bound, evidenced method of recording an already-executed settlement, reconciling partial payments, tender/date and references, and preventing second execution. Current code does not comprehensively enforce this.
- A single PO expense is enforced by `Expense`'s unique purchase-order constraint; multiple instalments and subsequent receipts cannot automatically become multiple payable/expense records. The existing `procurement_payable_service.py` notes the subsequent-delivery limitation. Do not force duplicates.
- Handover assumes inventory class even when goods have not yet been received; advances need proper documented asset classification and review. Cash handovers default to PAYABLE because no drawer evidence is available. The expense can be approved/posted without verifying actual external settlement in this change. Obtain and independently validate payment evidence and goods receipts before any posting.
- Reconciliation status is not refreshed automatically after every expense approval/posting/settlement; dashboard status and the unposted operational count can therefore be stale. Source-key matching is not bank verification. Existing unlinked posted expenses require manual reference verification; this patch deliberately avoids claiming an unproven link.
- UI display requirements and independent AP/inventory/bank reconciliation remain unverified. A `REQUIRES_REVIEW` flag does not itself prevent subsequent payment execution. Existing data marked `MIRRORED` by historical migration may not have a verified corresponding settlement; audit those records independently.

## Historical investigation on an authorized read-only replica
For each `PurchaseOrderPayment`, list PO reference, supplier, `amount`, PO currency, `paid_on`, `method`, `reference`, evidence filename, accounting settlement reference, finance expense reference, finance transaction reference and posting status. Join to `Expense` by PO/reference and to `SupplierPaymentRequest` and posted `FinancialTransaction`; compare exact amounts, currencies, date, tender and bank evidence. Identify unmatched, pending/rejected/draft/approved-but-unposted, stale status, duplicate payment references, one transaction referenced by multiple instalments, and orphaned expenses. Verify inventory receipt and AP separately. Preserve records and use controlled adjustments only after approval. No actual row-level findings can be listed without the database.

```python
# python manage.py shell -- on an authorized read-only replica
from apps.inventory.models import PurchaseOrderPayment
from apps.finance.models import Expense
for p in PurchaseOrderPayment.objects.select_related('purchase_order', 'purchase_order__supplier').iterator():
    po = p.purchase_order
    expenses = list(Expense.objects.filter(purchase_order=po).values('reference', 'status', 'amount', 'currency', 'payment_method', 'accounting_class', 'financial_transaction__reference', 'settlement_transaction__reference'))
    print({'po':po.reference, 'supplier':po.supplier.name, 'payment_id':p.pk, 'date':str(p.paid_on),
           'amount':str(p.amount), 'currency':po.currency, 'method':p.method, 'payment_reference':p.reference,
           'evidence':str(p.evidence_file or ''), 'settlement_request':p.accounting_settlement_reference,
           'expense_reference':p.finance_expense_reference, 'financial_reference':p.finance_transaction_reference,
           'link_status':p.accounting_posting_status, 'po_expenses':expenses})
```
Printouts contain sensitive supplier information; protect and delete in accordance with policy. Compare with actual bank/cash/card evidence and posted lines; do not treat output as proof of payment.
