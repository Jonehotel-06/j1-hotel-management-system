"""Classify the folio money that already exists, so no paid room hides a bill.

``0013`` adds ``FolioPosting.settles`` (blank = "counted with the folio
charges", the historical reading). This migration labels the rows the data
already proves, so the guest-balance projection is right for every existing
folio the moment the release is deployed — without editing, deleting or
re-posting a single naira:

* a ``PAYMENT`` posting created by a **booking/stay payment** settles the
  ACCOMMODATION. Those allocations come from
  ``apply_unallocated_payments_for_booking`` (the deposit/booking money a guest
  paid for the room, which is already counted in ``booking.amount_paid``);
* a ``PAYMENT`` posting created by the **desk settlement of the whole bill**
  (``settle_outstanding_balance``) settles the FOLIO CHARGES — it is the money
  the guest handed over for their restaurant / bar / room-service / laundry
  bill, marked on its payment as ``settled_via="checkout_folio"``;
* a ``REFUND`` posting inherits the classification of the allocation it
  reverses (its financial line carries ``payment_allocation_id``), so refunding
  folio-charge money re-opens those charges while refunding room money never
  invents a charge the guest did not run up.

Nothing else is touched: charges, discounts, adjustments and reversals keep the
blank default and stay counted with the folio charges. The pass is idempotent,
and its reverse clears only the labels this pass computed.
"""
from django.db import migrations

ACCOMMODATION = "ACCOMMODATION"
FOLIO_CHARGES = "FOLIO_CHARGES"
PAYMENT = "PAYMENT"
REFUND = "REFUND"


def _settled_at_desk(payment) -> bool:
    metadata = payment.metadata or {}
    return str(metadata.get("settled_via") or "").strip() == "checkout_folio"


def _labels(models):
    """``{(transaction_id, folio_id, kind): settles}`` proved by the data."""
    FolioPosting = models["finance.folioposting"]
    PaymentAllocation = models["finance.paymentallocation"]

    labels = {}
    for allocation in PaymentAllocation.objects.select_related("payment").iterator():
        label = FOLIO_CHARGES if _settled_at_desk(allocation.payment) else ACCOMMODATION
        labels[(allocation.transaction_id, allocation.folio_id, PAYMENT)] = label

    # A refund re-charges whichever balance its allocation settled.
    for posting in (
        FolioPosting.objects.filter(kind=REFUND)
        .select_related("line")
        .iterator()
    ):
        allocation_id = (posting.line.metadata or {}).get("payment_allocation_id")
        if not allocation_id:
            continue
        allocation = PaymentAllocation.objects.filter(pk=allocation_id).first()
        if allocation is None:
            continue
        label = labels.get((allocation.transaction_id, allocation.folio_id, PAYMENT))
        if label:
            labels[(posting.transaction_id, posting.folio_id, REFUND)] = label
    return labels


def _apply(models, *, clear=False):
    FolioPosting = models["finance.folioposting"]
    labels = _labels(models)
    for (transaction_id, folio_id, kind), label in labels.items():
        queryset = FolioPosting.objects.filter(
            transaction_id=transaction_id, folio_id=folio_id, kind=kind
        )
        if clear:
            queryset.filter(settles=label).update(settles="")
        else:
            queryset.update(settles=label)


def classify_folio_money(apps, schema_editor):
    models = {model._meta.label_lower: model for model in apps.get_models()}
    _apply(models)


def unclassify_folio_money(apps, schema_editor):
    """Clear only the labels this migration computed (a downgrade helper)."""
    models = {model._meta.label_lower: model for model in apps.get_models()}
    _apply(models, clear=True)


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0013_folioposting_settles"),
    ]

    operations = [
        migrations.RunPython(classify_folio_money, unclassify_folio_money),
    ]
