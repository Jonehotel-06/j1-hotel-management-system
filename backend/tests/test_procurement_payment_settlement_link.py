"""A4 — procurement supplier payments vs the Accounting settlement.

Before the repair, the Procurement page's "Record supplier payment" row was
an independent money-out record ("It does not post to the finance ledger")
that could silently contradict what Accounting had actually paid. Critical
repair A4 binds the two sides:

* ``PurchaseOrderPayment.accounting_settlement_reference`` names the
  authoritative Accounting settlement (the executed ``SupplierPaymentRequest``)
  the record mirrors;
* when that link is claimed it is verified — the request must exist, be
  EXECUTED, settle THIS order's payable and move exactly this amount, and one
  settlement backs at most one record — so a procurement record can never
  contradict the ledger;
* a payment settled outside the Accounting payables workflow stays
  recordable with a blank link (no existing rows or flows are disturbed).
"""
from decimal import Decimal

from django.core.exceptions import ValidationError

from apps.accounts.models import User
from apps.finance.models import SupplierPaymentRequest
from apps.finance.services.expense_service import post_expense, review_expense, submit_expense
from apps.finance.services.procurement_payable_service import raise_payable_from_purchase_order
from apps.finance.services.supplier_payment_service import (
    execute_supplier_payment,
    propose_supplier_payment,
    review_supplier_payment,
    submit_supplier_payment,
)
from apps.inventory.models import PurchaseOrderPayment, StockItem, StockLocation, Supplier
from apps.inventory.services.inventory_service import (
    approve_purchase_order,
    create_purchase_order,
    receive_goods,
    record_purchase_order_payment,
    submit_purchase_order,
)

from .base import BaseAPITestCase
from .factories import make_staff

PAY_URL = "/api/admin/inventory/purchase-orders/{ref}/pay/"


class ProcurementPaymentSettlementLinkTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.maker = make_staff("a4-maker@staff.dev", role=User.Role.PROCUREMENT_OFFICER)
        self.manager = make_staff("a4-manager@staff.dev", role=User.Role.MANAGER)
        self.storekeeper = make_staff("a4-store@staff.dev", role=User.Role.STOREKEEPER)
        self.accountant = make_staff("a4-accountant@staff.dev", role=User.Role.ACCOUNTANT)
        self.supplier = Supplier.objects.create(reference="A4-SUP", name="A4 Supplies Ltd")
        self.location = StockLocation.objects.create(code="A4-STORE", name="A4 Store")
        self.item = StockItem.objects.create(sku="A4-ITEM", name="A4 Item", base_unit="piece")
        self.po = self._make_po()
        self.line = self.po.lines.get()

    # ------------------------------------------------------------------ helpers
    def _make_po(self, unit_cost="1000.00", quantity="1"):
        po, _ = create_purchase_order(
            supplier=self.supplier, delivery_location=self.location,
            requested_by=self.maker,
            lines=[{"item_id": self.item.pk, "quantity_ordered": quantity, "unit_cost": unit_cost}],
        )
        submit_purchase_order(purchase_order=po, actor=self.maker)
        approve_purchase_order(purchase_order=po, actor=self.manager)
        return po

    def _make_settlement(self, *, po=None, amount="1000.00", key="a4-spr-1", execute=True):
        """Goods-receive one unit on ``po``, raise + post its payable, and run
        one supplier-payment request through to (optionally) EXECUTED. The
        ledger settles an on-account payable in a single full payment, so
        ``amount`` must be the received value (unit cost here)."""
        po = po or self.po
        receive_goods(
            purchase_order=po, actor=self.storekeeper, source_key=f"{key}-grn",
            receipt_lines=[{"purchase_order_line_id": po.lines.get().pk, "quantity_received": "1"}],
        )
        expense, _ = raise_payable_from_purchase_order(
            purchase_order=po, actor=self.accountant, idempotency_key=f"{key}-payable",
        )
        expense = submit_expense(expense=expense, actor=self.accountant)
        expense = review_expense(expense=expense, reviewer=self.manager, approved=True)
        expense, _ = post_expense(expense=expense, actor=self.accountant)
        request, _ = propose_supplier_payment(
            expense=expense, actor=self.accountant, method="BANK_TRANSFER",
            amount=Decimal(amount), payment_reference=f"TRF-{key}", idempotency_key=f"{key}-spr",
        )
        request = submit_supplier_payment(request=request, actor=self.accountant)
        request = review_supplier_payment(request=request, reviewer=self.manager, approved=True)
        if execute:
            request, _ = execute_supplier_payment(request=request, actor=self.accountant)
        return request

    def _record(self, *, po=None, amount="1000.00", reference="TRF-A4-1", settlement=""):
        return record_purchase_order_payment(
            purchase_order=po or self.po, actor=self.manager, method="BANK_TRANSFER",
            amount=Decimal(amount), reference=reference,
            accounting_settlement_reference=settlement,
        )

    # ------------------------------------------------------- the link is truthful
    def test_a_linked_payment_mirrors_the_executed_settlement(self):
        request = self._make_settlement()
        payment = self._record(settlement=request.reference)
        self.assertEqual(payment.accounting_settlement_reference, request.reference)
        self.po.refresh_from_db()
        self.assertEqual(self.po.total_paid, Decimal("1000.00"))

    def test_an_unknown_settlement_reference_is_refused(self):
        with self.assertRaises(ValidationError):
            self._record(settlement="SPR-DOES-NOT-EXIST")
        self.assertEqual(PurchaseOrderPayment.objects.count(), 0)

    def test_an_unexecuted_settlement_cannot_be_mirrored(self):
        request = self._make_settlement(execute=False)  # APPROVED, money not moved
        self.assertEqual(request.status, SupplierPaymentRequest.Status.APPROVED)
        with self.assertRaises(ValidationError):
            self._record(settlement=request.reference)
        self.assertEqual(PurchaseOrderPayment.objects.count(), 0)

    def test_a_settlement_for_another_orders_payable_is_refused(self):
        other_po = self._make_po()
        other_request = self._make_settlement(po=other_po, key="a4-spr-other")
        with self.assertRaises(ValidationError):
            self._record(settlement=other_request.reference)
        self.assertEqual(PurchaseOrderPayment.objects.count(), 0)

    def test_an_amount_that_contradicts_the_settlement_is_refused(self):
        request = self._make_settlement()          # settlement moved 1000.00
        with self.assertRaises(ValidationError):
            self._record(amount="900.00", settlement=request.reference)  # would lie
        self.assertEqual(PurchaseOrderPayment.objects.count(), 0)

    def test_one_settlement_cannot_back_two_payment_records(self):
        # A partially received order: two units ordered, one received, so the
        # single ₦1,000 settlement is mirrored while ₦1,000 of the order is
        # still outstanding. The SECOND record claiming the same settlement
        # would double-count one real payment — refused.
        po = self._make_po(quantity="2")
        request = self._make_settlement(po=po, amount="1000.00", key="a4-spr-partial")
        self._record(po=po, amount="1000.00", reference="TRF-A4-A", settlement=request.reference)
        with self.assertRaises(ValidationError):
            self._record(po=po, amount="1000.00", reference="TRF-A4-B", settlement=request.reference)
        self.assertEqual(PurchaseOrderPayment.objects.count(), 1)

    # ------------------------------------------------- the ledger-free path stays
    def test_a_payment_without_a_settlement_link_still_records(self):
        payment = self._record(reference="TRF-LEGACY")
        self.assertEqual(payment.accounting_settlement_reference, "")
        self.po.refresh_from_db()
        self.assertEqual(self.po.total_paid, Decimal("1000.00"))

    def test_the_pay_endpoint_carries_and_returns_the_settlement_link(self):
        request = self._make_settlement()
        self.auth(self.manager)
        res = self.client.post(
            PAY_URL.format(ref=self.po.reference),
            {
                "method": "BANK_TRANSFER", "amount": "1000.00", "reference": "TRF-API-1",
                "accounting_settlement_reference": request.reference,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        data = res.json()["data"]
        payments = data["payments"] if isinstance(data, dict) else []
        self.assertTrue(payments, "the detail response carries the payment rows")
        self.assertEqual(payments[-1]["accounting_settlement_reference"], request.reference)

    def test_the_pay_endpoint_refuses_a_contradicting_link(self):
        self.auth(self.manager)
        res = self.client.post(
            PAY_URL.format(ref=self.po.reference),
            {
                "method": "BANK_TRANSFER", "amount": "1000.00", "reference": "TRF-API-2",
                "accounting_settlement_reference": "SPR-GHOST",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(PurchaseOrderPayment.objects.count(), 0)
