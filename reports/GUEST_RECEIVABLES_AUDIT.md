# Guest receivables audit

**Status: PARTIAL; not approved for production.** Source: GitHub HEAD `b613cd20e8de7b6a610a6145ff02392596122a90`, inspected in an isolated clone. No live hotel database, bank evidence, or actual hotel records were supplied. No statement about particular historic guest references, dates, amounts, or corrections can be made from source code alone.

## What the existing workflow actually does
- Collection (`payment_collection_service.ensure_payment_collection`) posts debit cash/bank/card clearing, credit guest deposits, once per payment. A booking projection is not a posted receivable.
- Stay creation opens a main folio; accommodation is posted for elapsed nights, debit guest receivable and credit accommodation revenue (plus historical tax/fees where applicable), using source-keyed nightly transactions. Allocation debits deposits and credits AR only up to recognised accommodation charges. The requested check-in example therefore becomes receivable **when the accommodation is earned**, not automatically on the day of check-in; making the full booking receivable at check-in would also recognise future nights prematurely.
- POS delivered room service creates a debit to folio AR and credit food/beverage revenue; allocation/settlement credits AR; `booking_folio_charge_map` excludes accommodation-settling payments from extra charges, avoiding using a room payment twice. `test_pos`, `test_folio_balance_classification` and receivable suites exercised these paths; they do not prove the specific reported production record.
- `guest_receivables_service` uses posted GL lines with debit-positive signs, date and currency filtering, preserves negative folio balances and shows deposits separately. Booking/checkout operational amounts can still include future contract amounts and are not financial statement receivables.
- `statements_service` presents the posted AR control balance within Other assets, not a distinct AR line. This is existing presentation policy; the negative balance or misallocations in actual data cannot be diagnosed without the data.

## Confirmed source defects and implemented changes
1. **Refunded deposit could be spent again.** Both allocation paths used payment amount minus allocations, ignoring processed refund applications against the unallocated deposit. A ₦70,000 deposit with ₦20,000 returned could have ₦70,000 allocated to a later charge, understating AR and making deposits debit-normal by ₦20,000. Allocation now subtracts deposit-portion refund applications; refund and allocation lock the same payment row. Refunds against previously allocated money reopen AR and do not reduce remaining unallocated deposit a second time.
2. **Self-proving AR identity.** The report previously derived unassigned movements by subtracting folio totals from control, so the advertised reconciliation difference was necessarily zero. It now independently aggregates actual posted lines with no folio, in the same SQL aggregate as the control amount. Formula: control = gross folio debit balances − folio credit balances + signed unassigned.
3. **Mixed-currency financial statements.** `statements_service._balances` and cash-flow classification included every currency while returning `NGN`. Both now restrict to posted NGN transactions and explicitly state that foreign currencies are excluded, not translated. The existing APIs still do not offer foreign-currency statements or FX translation.

## Remaining material risks — not repaired
- **Partial multi-room occupancy:** `accommodation_service._night_components` splits the *whole booking* amount by nights, not by occupied room. A one-room arrival from a multi-room booking can therefore recognise the full nightly booking amount. `stays/services.py` also uses the booking's check-in timestamp when later rooms are appended, complicating accurate room-night reconstruction. Existing multi-room tests cover booking inventory, not this accounting case. Determining the correct share of discounts, taxes, extra guest fees and room moves needs verified room-night pricing and timestamps; don't deploy this overlay as a complete fix. Relevant files: `backend/apps/finance/services/accommodation_service.py`, `backend/apps/finance/services/accommodation_reconciliation.py`, `backend/apps/stays/services.py`, `backend/apps/bookings/services/booking_service.py`, associated room-night and night-audit tests.
- `allocation_capacity(... FOLIO_CHARGES)` is booking-wide while the credited line is attached to one folio; split-folio or cross-folio payments need specific review. Checkout constraints, all refund variations, late occupancy and historic reversal/as-of behavior need more end-to-end validation.
- Statements still fold net AR into Other assets; guest credit balances are not reclassified to liabilities in the statement, although AR detail displays them separately. Reclassification needs an explicit, authorized policy and traceable journal, not a fabricated total.
- Existing `guest_receivable_check` reads all currencies in some totals and warns about currency mixing; use the currency-filtered AR API for per-currency totals. Do not treat its combined totals as NGN.

## Historical reconciliation procedure (read-only, authorized replica)
1. Take a verified backup and work on a read-only snapshot. Run `python manage.py guest_receivable_check --as-of YYYY-MM-DD --json` and the AR aging/report endpoint separately for each currency. Preserve output securely: it contains guest financial information. Inspect every negative folio, unassigned GL line, posted AR/deposit movement and its original transaction/date, including refunds and room-service references. The command is limited and some totals mix currencies; verify rows against currency-filtered ledger.
2. Identify payments whose total allocations plus **unallocated-deposit** refund applications exceed their original payment, by payment/booking reference, amount, currency, refund and allocation dates. See the read-only shell snippet below. This flags a possible defect, not an automatic correction; also check postings, adjustments and provider evidence.
3. For multi-room stays, compare each nightly transaction with actual room-night occupancy and booking terms; examine room assignment, move and release timestamps and pre-existing adjustment journals. This code alone cannot reliably infer the hotel's intended charge.
4. Review deposit liabilities, unapplied collections, credits and posted corrections per folio and currency. Reconcile control and unassigned lines to the GL and balance sheet. Any correction requires accountant review and authorized, balanced reversal/adjusting entries that preserve original references. No historic entry was edited.

```python
# python manage.py shell  (on an authorized, read-only replica)
from decimal import Decimal
from apps.payments.models import Payment
from apps.finance.models import PaymentAllocation, RefundApplication
for p in Payment.objects.select_related('booking').iterator():
    allocations = list(PaymentAllocation.objects.filter(payment=p).select_related('transaction'))
    refunds = list(RefundApplication.objects.filter(payment=p, payment_allocation__isnull=True).select_related('refund', 'transaction'))
    used = sum((a.amount for a in allocations), Decimal('0')) + sum((r.amount for r in refunds), Decimal('0'))
    if used > p.amount:
        print({'payment':p.reference, 'booking':p.booking.booking_reference, 'currency':p.currency,
               'payment_amount':str(p.amount), 'excess':str(used-p.amount),
               'allocations':[(a.transaction.reference, str(a.amount), str(a.transaction.business_date)) for a in allocations],
               'deposit_refunds':[(r.refund_id, r.transaction.reference, str(r.amount), str(r.transaction.business_date)) for r in refunds]})
```
