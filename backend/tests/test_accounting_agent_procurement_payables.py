"""Accounting agent: submitted purchase orders must be visible and payable once.

What the audit found: Accounting could see the purchase-order hand-off only by
leaving the page ("Full supplier register, purchase orders and goods receipts
live in Procurement"), and there was no route from a purchase order to a
payable at all — an accountant re-typed the supplier and amount into a free
expense, so order, receipt, payable and payment were four unconnected records
and the same delivery could be billed twice.

These tests pin the repair:

1. an approved order whose goods were received produces a payable linked to the
   order, for the value received — not the ordered total;
2. a draft, a merely submitted order, or an order with nothing received is NOT
   payable: a purchase order is not proof of delivery;
3. partial receipts produce a payable for the part delivered, and an amount
   above the value received is refused;
4. a second payable for the same order is refused by the service *and* by the
   database constraint;
5. the payable travels the existing expense workflow (submit -> approve -> post)
   and, once posted, is a real liability that a supplier payment can settle;
6. the reconciliation pack reports goods received that are not yet billed, and
   stops reporting them once the payable is raised;
7. Accounting can read the procurement hand-off (submitted orders only) and a
   role without financial visibility cannot.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import Expense
from apps.finance.services.expense_service import post_expense, review_expense, submit_expense
from apps.inventory.models import PurchaseOrder, StockItem, StockLocation, Supplier
from apps.inventory.services.inventory_service import (
    approve_purchase_order,
    create_purchase_order,
    receive_goods,
    submit_purchase_order,
)

from .base import BaseAPITestCase
from .factories import make_staff


class PurchaseOrderPayableTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.procurement = make_staff("payables-procurement@staff.dev", role=User.Role.PROCUREMENT_OFFICER)
        self.manager = make_staff("payables-manager@staff.dev", role=User.Role.MANAGER)
        self.accountant = make_staff("payables-accountant@staff.dev", role=User.Role.ACCOUNTANT)
        self.storekeeper = make_staff("payables-storekeeper@staff.dev", role=User.Role.STOREKEEPER)
        self.supplier = Supplier.objects.create(reference="PAY-SUP", name="Payables Supplier Ltd")
        self.location = StockLocation.objects.create(code="PAY-STORE", name="Payables store")
        self.item = StockItem.objects.create(sku="PAY-ITEM", name="Bottled water", base_unit="piece")
        self.po, _ = create_purchase_order(
            supplier=self.supplier, delivery_location=self.location, requested_by=self.procurement,
            lines=[{"item_id": self.item.pk, "quantity_ordered": "100", "unit_cost": "250.00"}],
        )
        self.line = self.po.lines.get()

    # ------------------------------------------------------------------ helpers
    def _submit_and_approve(self):
        submit_purchase_order(purchase_order=self.po, actor=self.procurement)
        approve_purchase_order(purchase_order=self.po, actor=self.manager)
        self.po.refresh_from_db()

    def _receive(self, quantity, key="pay-receipt-1"):
        receipt, created = receive_goods(
            purchase_order=self.po, actor=self.storekeeper, source_key=key,
            receipt_lines=[{"purchase_order_line_id": self.line.pk, "quantity_received": quantity}],
        )
        self.po.refresh_from_db()
        return receipt, created

    def _raise(self, amount=None, key="payable-1"):
        body = {"idempotency_key": key}
        if amount is not None:
            body["amount"] = amount
        return self.client.post(
            f"/api/admin/finance/procurement/purchase-orders/{self.po.reference}/payable/", body, format="json",
        )

    # ------------------------------------------------- order is not a liability
    def test_a_draft_order_cannot_be_billed(self):
        self.auth(self.accountant)
        response = self._raise()
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(Expense.objects.count(), 0)

    def test_a_submitted_order_with_no_receipt_is_not_payable(self):
        submit_purchase_order(purchase_order=self.po, actor=self.procurement)
        self.auth(self.accountant)
        unapproved = self._raise(key="payable-submitted")
        self.assertEqual(unapproved.status_code, 400, unapproved.content)
        self.assertIn("not been approved", str(unapproved.content))
        # Approved, but nothing has physically arrived: still not payable.
        self._submit_and_approve()
        response = self._raise()
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("No goods have been received", str(response.content))
        self.assertEqual(Expense.objects.filter(purchase_order=self.po).count(), 0)

    # --------------------------------------------- payable from value received
    def test_a_partial_receipt_raises_a_payable_for_what_actually_arrived(self):
        self._submit_and_approve()
        self._receive("40")  # 40 of 100 bottles = 10,000.00
        self.auth(self.accountant)
        response = self._raise()
        self.assertEqual(response.status_code, 201, response.content)
        payable = response.json()["data"]
        self.assertEqual(payable["amount"], "10000.00")
        self.assertEqual(payable["payment_method"], "PAYABLE")
        self.assertEqual(payable["status"], Expense.Status.DRAFT)
        self.assertEqual(payable["purchase_order"], self.po.reference)
        expense = Expense.objects.get(reference=payable["reference"])
        self.assertEqual(expense.purchase_order_id, self.po.pk)
        self.assertEqual(expense.metadata["received_value"], "10000.00")

    def test_a_payable_above_the_value_received_is_refused(self):
        self._submit_and_approve()
        self._receive("40")
        self.auth(self.accountant)
        response = self._raise(amount="25000.00")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("cannot exceed the value received", str(response.content))
        # Below the received value is allowed: the supplier may invoice less.
        accepted = self._raise(amount="9000.00", key="payable-part")
        self.assertEqual(accepted.status_code, 201, accepted.content)
        self.assertEqual(accepted.json()["data"]["amount"], "9000.00")

    def test_the_same_order_cannot_be_billed_twice(self):
        self._submit_and_approve()
        self._receive("40")
        self.auth(self.accountant)
        self.assertEqual(self._raise().status_code, 201)
        duplicate = self._raise(key="payable-2")
        self.assertEqual(duplicate.status_code, 400, duplicate.content)
        self.assertIn("already exists", str(duplicate.content))
        self.assertEqual(Expense.objects.filter(purchase_order=self.po).count(), 1)

        # The service check is the friendly message; the constraint is the
        # guarantee. A direct insert that bypasses the service must still fail.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Expense.objects.create(
                    reference="EXP-DUPLICATE-PAYABLE", idempotency_key="duplicate-payable",
                    category="Inventory purchase", amount=Decimal("1.00"),
                    requested_by=self.accountant, purchase_order=self.po,
                    payment_method=Expense.PaymentMethod.PAYABLE,
                )

    def test_the_payable_travels_the_existing_expense_workflow_and_becomes_a_liability(self):
        self._submit_and_approve()
        self._receive("40")
        self.auth(self.accountant)
        reference = self._raise().json()["data"]["reference"]

        # An accountant may raise and submit, but not approve: the existing
        # maker-checker is untouched by this new route.
        submitted = self.client.post(f"/api/admin/finance/expenses/{reference}/submit/", {}, format="json")
        self.assertEqual(submitted.status_code, 200, submitted.content)
        self.auth(self.accountant)
        self.assertEqual(
            self.client.post(f"/api/admin/finance/expenses/{reference}/review/", {"approved": True}, format="json").status_code,
            403,
        )
        self.auth(self.manager)
        approved = self.client.post(f"/api/admin/finance/expenses/{reference}/review/", {"approved": True}, format="json")
        self.assertEqual(approved.status_code, 200, approved.content)
        posted = self.client.post(f"/api/admin/finance/expenses/{reference}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.content)
        self.assertEqual(posted.json()["data"]["status"], Expense.Status.POSTED)

    # -------------------------------------------------- hand-off and visibility
    def test_accounting_sees_submitted_orders_with_the_payable_position(self):
        self._submit_and_approve()
        self._receive("40")
        self.auth(self.accountant)
        handoff = self.client.get("/api/admin/inventory/accounting/purchase-orders/")
        self.assertEqual(handoff.status_code, 200, handoff.content)
        rows = handoff.json()["data"]
        rows = rows["results"] if isinstance(rows, dict) else rows
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reference"], self.po.reference)
        self.assertEqual(rows[0]["supplier_name"], self.supplier.name)
        self.assertEqual(rows[0]["received_value"], "10000.00")
        self.assertEqual(rows[0]["receipt_progress"], "PARTIALLY_RECEIVED")

        payables = self.client.get(
            f"/api/admin/finance/procurement/payables/?references={self.po.reference}"
        )
        self.assertEqual(payables.status_code, 200, payables.content)
        position = payables.json()["data"]["purchase_orders"][self.po.reference]
        self.assertEqual(position["received_value"], "10000.00")
        self.assertEqual(position["payable_raised"], "0.00")
        self.assertEqual(position["unbilled_received_value"], "10000.00")
        self.assertTrue(position["can_raise_payable"])

        self._raise()
        position = self.client.get(
            f"/api/admin/finance/procurement/payables/?references={self.po.reference}"
        ).json()["data"]["purchase_orders"][self.po.reference]
        self.assertEqual(position["payable_raised"], "10000.00")
        self.assertEqual(position["unbilled_received_value"], "0.00")
        self.assertFalse(position["can_raise_payable"])

    def test_a_draft_order_is_never_handed_to_accounting(self):
        self.auth(self.accountant)
        rows = self.client.get("/api/admin/inventory/accounting/purchase-orders/").json()["data"]
        rows = rows["results"] if isinstance(rows, dict) else rows
        self.assertEqual([row for row in rows if row["reference"] == self.po.reference], [])

    def test_a_cashier_cannot_read_the_payable_position(self):
        self._submit_and_approve()
        cashier = make_staff("payables-cashier@staff.dev", role=User.Role.CASHIER)
        self.auth(cashier)
        self.assertEqual(self.client.get("/api/admin/finance/procurement/payables/").status_code, 403)
        self.assertEqual(
            self.client.post(
                f"/api/admin/finance/procurement/purchase-orders/{self.po.reference}/payable/",
                {"idempotency_key": "cashier-payable"}, format="json",
            ).status_code,
            403,
        )

    # -------------------------------------------------- reconciliation coverage
    def test_received_goods_without_a_payable_are_reported_and_then_cleared(self):
        self._submit_and_approve()
        self._receive("40")
        self.auth(self.accountant)
        today = hotel_today().isoformat()
        checks = self.client.get(f"/api/admin/finance/reconciliation/checks/?date={today}").json()["data"]
        unbilled = [row for row in checks["checks"] if row["type"] == "PO_RECEIVED_NOT_BILLED"]
        self.assertEqual(len(unbilled), 1)
        self.assertEqual(unbilled[0]["amount"], "10000.00")
        self.assertEqual(unbilled[0]["severity"], "WARNING")
        self.assertEqual(checks["summary"]["attention_procurement"], 1)

        reference = self._raise().json()["data"]["reference"]
        # A drafted payable is work in progress, not cost in the books: the
        # condition is still reported, and now names the draft.
        checks = self.client.get(f"/api/admin/finance/reconciliation/checks/?date={today}").json()["data"]
        unbilled = [row for row in checks["checks"] if row["type"] == "PO_RECEIVED_NOT_BILLED"]
        self.assertEqual(len(unbilled), 1)
        self.assertIn("still DRAFT", unbilled[0]["why_flagged"])

        self.client.post(f"/api/admin/finance/expenses/{reference}/submit/", {}, format="json")
        self.auth(self.manager)
        self.client.post(f"/api/admin/finance/expenses/{reference}/review/", {"approved": True}, format="json")
        self.client.post(f"/api/admin/finance/expenses/{reference}/post/", {}, format="json")
        self.auth(self.accountant)
        checks = self.client.get(f"/api/admin/finance/reconciliation/checks/?date={today}").json()["data"]
        self.assertEqual([row for row in checks["checks"] if row["type"] == "PO_RECEIVED_NOT_BILLED"], [])

    def test_a_second_delivery_after_the_payable_shows_as_unbilled_remainder(self):
        self._submit_and_approve()
        self._receive("40", key="pay-receipt-1")
        self.auth(self.accountant)
        reference = self._raise().json()["data"]["reference"]
        self.client.post(f"/api/admin/finance/expenses/{reference}/submit/", {}, format="json")
        self.auth(self.manager)
        self.client.post(f"/api/admin/finance/expenses/{reference}/review/", {"approved": True}, format="json")
        self.client.post(f"/api/admin/finance/expenses/{reference}/post/", {}, format="json")
        self.auth(self.accountant)
        # The rest of the order arrives later; the one-payable-per-order rule
        # deliberately refuses a second document and the pack says so.
        self._receive("10", key="pay-receipt-2")
        checks = self.client.get(
            f"/api/admin/finance/reconciliation/checks/?date={hotel_today().isoformat()}"
        ).json()["data"]
        unbilled = [row for row in checks["checks"] if row["type"] == "PO_RECEIVED_NOT_BILLED"]
        self.assertEqual(len(unbilled), 1)
        self.assertEqual(unbilled[0]["amount"], "2500.00")
        second = self.client.post(
            f"/api/admin/finance/procurement/purchase-orders/{self.po.reference}/payable/",
            {"idempotency_key": "payable-second-delivery"}, format="json",
        )
        self.assertEqual(second.status_code, 400, second.content)
        self.assertIn("already exists", str(second.content))
