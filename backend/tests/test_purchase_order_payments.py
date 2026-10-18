"""Recording supplier payments against purchase orders.

Until now the only money-related evidence on a purchase order was the
receipt filed *at approval* (often an advance). The actual payment —
the money leaving the hotel to the supplier — had nowhere to go. These
tests pin the settlement record: append-only payments, each with its
own receipt or invoice evidence, and the order moving to PAID only
when the payments cover the order total.

Run: python manage.py test tests.test_purchase_order_payments
"""
import shutil
import tempfile
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.inventory.models import PurchaseOrder, PurchaseOrderPayment
from apps.inventory.services.inventory_service import (
    approve_purchase_order,
    create_purchase_order,
    mark_purchase_order_ordered,
    submit_purchase_order,
)

from .base import BaseAPITestCase
from .factories import make_staff

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def pdf_upload(name="supplier-invoice.pdf"):
    return SimpleUploadedFile(name, PDF_BYTES, content_type="application/pdf")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="jone-test-media-"))
class SupplierPaymentTests(BaseAPITestCase):
    @classmethod
    def tearDownClass(cls):
        from django.conf import settings

        shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.clerk = make_staff("pay-clerk@staff.dev", role=User.Role.INVENTORY_CLERK)
        self.manager = make_staff("pay-manager@staff.dev", role=User.Role.MANAGER)
        self.supplier = self._supplier("PAY-SUP-1", "Pay Supplies Ltd")
        self.location = self._location("PAY-STORE", "Pay Store")
        self.item = self._item("PAY-RICE", "Rice (50kg bag)", "1500.00")
        self.reference = self._order_through_approval(quantity="4", unit_cost="1500")

    # ----------------------------------------------------------------- helpers
    def _supplier(self, reference, name):
        from apps.inventory.models import Supplier

        return Supplier.objects.create(reference=reference, name=name)

    def _location(self, code, name):
        from apps.inventory.models import StockLocation

        return StockLocation.objects.create(code=code, name=name)

    def _item(self, sku, name, cost):
        from apps.inventory.models import StockItem

        return StockItem.objects.create(sku=sku, name=name, base_unit="PIECE", standard_cost=cost)

    def _order_through_approval(self, *, quantity="4", unit_cost="1500"):
        """Create a real order through the service and approve it."""
        po, _created = create_purchase_order(
            supplier=self.supplier, delivery_location=self.location,
            requested_by=self.clerk,
            lines=[{"item_id": self.item.pk, "quantity_ordered": quantity, "unit_cost": unit_cost}],
        )
        submit_purchase_order(purchase_order=po, actor=self.clerk)
        approve_purchase_order(purchase_order=po, actor=self.manager)
        return po.reference

    def _pay(self, reference, data, files=None, as_user=None):
        self.auth(as_user or self.manager)
        payload = {}
        for key, value in (data or {}).items():
            payload[key] = value
        for key, value in (files or {}).items():
            payload[key] = value
        return self.client.post(
            reverse("inventory:purchase-order-pay", kwargs={"reference": reference}),
            payload,
            format="multipart",
        )

    # ------------------------------------------------------------------ tests
    def test_a_full_payment_marks_the_order_paid(self):
        response = self._pay(self.reference, {
            "method": "BANK_TRANSFER", "reference": "GTB-9911-PO",
            "paid_on": "2026-10-17",
        })
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["status"], PurchaseOrder.Status.PAID)
        self.assertIsNotNone(data["paid_at"])
        self.assertEqual(data["paid_by_email"], self.manager.email)
        self.assertEqual(data["total_paid"], "6000.00")
        self.assertEqual(data["outstanding_amount"], "0.00")

        po = PurchaseOrder.objects.get(reference=self.reference)
        self.assertEqual(po.status, PurchaseOrder.Status.PAID)
        self.assertIsNotNone(po.paid_at)
        self.assertEqual(po.paid_by, self.manager)
        payment = po.payments.get()
        self.assertEqual(payment.amount, Decimal("6000.00"))
        self.assertEqual(payment.method, PurchaseOrderPayment.PaymentMethod.BANK_TRANSFER)
        self.assertEqual(payment.reference, "GTB-9911-PO")

    def test_amount_defaults_to_the_outstanding_balance(self):
        self._pay(self.reference, {"method": "CASH", "paid_on": "2026-10-17"})
        po = PurchaseOrder.objects.get(reference=self.reference)
        self.assertEqual(po.payments.first().amount, Decimal("6000.00"))
        self.assertEqual(po.status, PurchaseOrder.Status.PAID)

    def test_partial_payments_accumulate_and_only_the_last_one_pays_it_off(self):
        self._pay(self.reference, {"method": "BANK_TRANSFER", "reference": "ADV-1", "amount": "2000"})
        po = PurchaseOrder.objects.get(reference=self.reference)
        self.assertEqual(po.status, PurchaseOrder.Status.APPROVED)
        self.assertEqual(po.outstanding_amount, Decimal("4000.00"))
        self.assertIsNone(po.paid_at)

        response = self._pay(self.reference, {"method": "BANK_TRANSFER", "reference": "BAL-2", "amount": "4000"})
        self.assertEqual(response.status_code, 200, response.content)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.Status.PAID)
        self.assertEqual(po.payments.count(), 2)
        self.assertEqual(po.total_paid, Decimal("6000.00"))

    def test_a_payment_cannot_exceed_the_outstanding_balance(self):
        response = self._pay(self.reference, {"method": "BANK_TRANSFER", "reference": "OVER", "amount": "6000.01"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("outstanding balance", str(response.json()["errors"]).lower())
        self.assertEqual(PurchaseOrderPayment.objects.count(), 0)

    def test_zero_and_negative_amounts_are_refused(self):
        for bad in ("0", "0.00", "-100"):
            response = self._pay(self.reference, {"method": "CASH", "amount": bad})
            self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(PurchaseOrderPayment.objects.count(), 0)

    def test_non_cash_payments_require_a_reference(self):
        for method in ("BANK_TRANSFER", "CARD", "CHEQUE"):
            response = self._pay(self.reference, {"method": method, "amount": "6000"})
            self.assertEqual(response.status_code, 400, response.content)
            self.assertIn("reference", str(response.json()["errors"]).lower())
        self.assertEqual(PurchaseOrderPayment.objects.count(), 0)

    def test_cash_needs_no_reference_and_a_unknown_method_is_refused(self):
        self.assertEqual(
            self._pay(self.reference, {"method": "CASH", "amount": "6000"}).status_code, 200,
        )
        response = self._pay(self.reference, {"method": "GOAT", "amount": "6000"})
        self.assertEqual(response.status_code, 400, response.content)

    def test_a_payment_cannot_be_dated_in_the_future(self):
        response = self._pay(self.reference, {
            "method": "CASH", "paid_on": "2099-01-01",
        })
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("future", str(response.json()["errors"]).lower())

    def test_only_authorised_orders_can_be_paid(self):
        for status, actor in (("DRAFT", self.clerk), ("SUBMITTED", self.clerk)):
            po, _created = create_purchase_order(
                supplier=self.supplier, delivery_location=self.location,
                requested_by=self.clerk,
                lines=[{"item_id": self.item.pk, "quantity_ordered": "1", "unit_cost": "100"}],
            )
            if status == "SUBMITTED":
                submit_purchase_order(purchase_order=po, actor=self.clerk)
            response = self._pay(po.reference, {"method": "CASH", "amount": "100"}, as_user=self.manager)
            self.assertEqual(response.status_code, 400, response.content)
            self.assertIn("pprove", str(response.json()["errors"]))
            self.assertEqual(PurchaseOrderPayment.objects.filter(purchase_order=po).count(), 0)

        po, _created = create_purchase_order(
            supplier=self.supplier, delivery_location=self.location,
            requested_by=self.clerk,
            lines=[{"item_id": self.item.pk, "quantity_ordered": "1", "unit_cost": "100"}],
        )
        submit_purchase_order(purchase_order=po, actor=self.clerk)
        approve_purchase_order(purchase_order=po, actor=self.manager)
        mark_purchase_order_ordered(purchase_order=po, actor=self.manager)
        po.status = PurchaseOrder.Status.CANCELLED
        po.save(update_fields=["status"])
        response = self._pay(po.reference, {"method": "CASH", "amount": "100"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("cancelled", str(response.json()["errors"]).lower())

    def test_a_fully_paid_order_cannot_be_paid_again(self):
        self._pay(self.reference, {"method": "CASH", "amount": "6000"})
        response = self._pay(self.reference, {"method": "CASH", "amount": "1"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("fully paid", str(response.json()["errors"]).lower())

    def test_recording_a_payment_requires_approval_rights(self):
        response = self._pay(self.reference, {"method": "CASH"}, as_user=self.clerk)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(PurchaseOrderPayment.objects.count(), 0)

    def test_the_receipt_or_invoice_is_stored_with_the_payment(self):
        response = self._pay(self.reference, {
            "method": "BANK_TRANSFER", "reference": "GTB-EV",
        }, files={"evidence_file": pdf_upload("payment-evidence.pdf")})
        self.assertEqual(response.status_code, 200, response.content)
        payment = PurchaseOrderPayment.objects.get(reference="GTB-EV")
        self.assertTrue(payment.evidence_file.name.endswith(".pdf"))
        self.assertEqual(payment.evidence_uploaded_by, self.manager)

        detail = self.client.get(
            reverse("inventory:purchase-order-detail", kwargs={"reference": self.reference}),
        )
        self.assertEqual(detail.status_code, 200, detail.content)
        evidence = detail.json()["data"]["payments"][0]["evidence_url"]
        self.assertTrue(evidence and "purchase_orders/payments/" in evidence, evidence)

    def test_an_invalid_evidence_file_is_refused_without_recording_the_payment(self):
        response = self._pay(self.reference, {
            "method": "CASH",
        }, files={"evidence_file": SimpleUploadedFile("bad.exe", b"MZ\x00\x00", content_type="application/octet-stream")})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(PurchaseOrderPayment.objects.count(), 0)

    def test_payments_are_append_only(self):
        self._pay(self.reference, {"method": "CASH", "amount": "6000"})
        payment = PurchaseOrderPayment.objects.get()
        with self.assertRaises(Exception):
            payment.amount = Decimal("1.00")
            payment.save()
        with self.assertRaises(Exception):
            payment.delete()

    def test_the_order_detail_lists_every_payment_with_its_evidence(self):
        self._pay(self.reference, {"method": "BANK_TRANSFER", "reference": "SPLIT-1", "amount": "2500"})
        self._pay(self.reference, {"method": "CASH", "amount": "3500"})
        self.auth(self.manager)
        response = self.client.get(
            reverse("inventory:purchase-order-detail", kwargs={"reference": self.reference}),
        )
        data = response.json()["data"]
        self.assertEqual(len(data["payments"]), 2)
        self.assertEqual(data["total_amount"], "6000.00")
        self.assertEqual(data["total_paid"], "6000.00")
        self.assertEqual(data["outstanding_amount"], "0.00")
        self.assertEqual(data["status"], PurchaseOrder.Status.PAID)
        self.assertEqual(
            [p["method"] for p in data["payments"]],
            [PurchaseOrderPayment.PaymentMethod.BANK_TRANSFER, PurchaseOrderPayment.PaymentMethod.CASH],
        )

    def test_the_list_endpoint_reports_totals_for_every_order(self):
        self.auth(self.manager)
        response = self.client.get(reverse("inventory:purchase-orders"))
        rows = response.json()["data"]
        rows = rows["results"] if isinstance(rows, dict) else rows
        row = next(r for r in rows if r["reference"] == self.reference)
        self.assertEqual(row["total_amount"], "6000.00")
        self.assertEqual(row["total_paid"], "0.00")
        self.assertEqual(row["outstanding_amount"], "6000.00")
