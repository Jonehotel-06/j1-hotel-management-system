"""Purchase-order editing, approval receipts and expense invoice uploads.

The three staff requests covered here share one principle: a commercial record
may be corrected only while it is still a proposal, and the evidence that money
moved is stored on the record it belongs to — as a real file, validated before
anything is written.

Run: python manage.py test tests.test_procurement_receipts_and_editing
"""
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from apps.accounts.models import User
from apps.finance.models import Expense
from apps.inventory.models import PurchaseOrder, StockItem, StockLocation, Supplier
from apps.inventory.services.inventory_service import (
    approve_purchase_order,
    create_purchase_order,
    mark_purchase_order_ordered,
    receive_goods,
    submit_purchase_order,
)

from .base import BaseAPITestCase
from .factories import make_staff

#: Smallest byte sequences that pass the real content checks.
PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def pdf_upload(name="supplier-invoice.pdf"):
    return SimpleUploadedFile(name, PDF_BYTES, content_type="application/pdf")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="jone-test-media-"))
class PurchaseOrderEditingTests(BaseAPITestCase):
    """Edit is a pre-approval right, and the server is the boundary."""

    @classmethod
    def tearDownClass(cls):
        from django.conf import settings

        shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.clerk = make_staff("po-edit-clerk@staff.dev", role=User.Role.INVENTORY_CLERK)
        self.manager = make_staff("po-edit-manager@staff.dev", role=User.Role.MANAGER)
        self.supplier = Supplier.objects.create(reference="SUP-EDIT-1", name="Edit Supplies Ltd")
        self.other_supplier = Supplier.objects.create(reference="SUP-EDIT-2", name="Second Supplies Ltd")
        self.location = StockLocation.objects.create(code="EDIT-STORE", name="Edit Store")
        self.item = StockItem.objects.create(
            sku="EDIT-RICE", name="Rice (50kg bag)", base_unit="PIECE", standard_cost="1500.00",
        )
        self.item2 = StockItem.objects.create(
            sku="EDIT-OIL", name="Cooking oil", base_unit="PIECE", standard_cost="900.00",
        )

    # ------------------------------------------------------------------ helpers
    def _create_order(self, *, quantity="4", status="DRAFT"):
        self.auth(self.clerk)
        response = self.client.post(
            "/api/admin/inventory/purchase-orders/",
            {
                "supplier_id": self.supplier.pk,
                "delivery_location_id": self.location.pk,
                "supplier_reference": "QUOTE-1",
                "notes": "Initial draft",
                "idempotency_key": "edit-po-one",
                "lines": [{"item_id": self.item.pk, "quantity_ordered": quantity, "unit_cost": "1500"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        reference = response.json()["data"]["reference"]
        if status in {"SUBMITTED", "APPROVED"}:
            self.assertEqual(
                self.client.post(f"/api/admin/inventory/purchase-orders/{reference}/submit/", {}, format="json").status_code,
                200,
            )
        if status == "APPROVED":
            self.auth(self.manager)
            self.assertEqual(
                self.client.post(f"/api/admin/inventory/purchase-orders/{reference}/approve/", {}, format="json").status_code,
                200,
            )
        return reference

    # -------------------------------------------------------------------- tests
    def test_a_draft_can_be_edited_and_its_lines_replaced(self):
        reference = self._create_order(status="DRAFT")
        self.auth(self.clerk)
        response = self.client.patch(
            f"/api/admin/inventory/purchase-orders/{reference}/",
            {
                "supplier_id": self.other_supplier.pk,
                "supplier_reference": "QUOTE-1-REVISED",
                "notes": "Supplier raised the price",
                "lines": [
                    {"item_id": self.item.pk, "quantity_ordered": "6", "unit_cost": "1550"},
                    {"item_id": self.item2.pk, "quantity_ordered": "2", "unit_cost": "900"},
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["supplier"], self.other_supplier.pk)
        self.assertEqual(data["supplier_reference"], "QUOTE-1-REVISED")
        self.assertEqual(data["status"], PurchaseOrder.Status.DRAFT)
        self.assertEqual(len(data["lines"]), 2)
        quantities = {line["item_sku"]: line["quantity_ordered"] for line in data["lines"]}
        self.assertEqual(quantities["EDIT-RICE"], "6.0000")
        self.assertEqual(quantities["EDIT-OIL"], "2.0000")

    def test_a_submitted_order_can_still_be_corrected_before_approval(self):
        reference = self._create_order(status="SUBMITTED")
        self.auth(self.clerk)
        response = self.client.patch(
            f"/api/admin/inventory/purchase-orders/{reference}/",
            {"notes": "Corrected before the approver saw it"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["notes"], "Corrected before the approver saw it")

    def test_an_approved_order_can_no_longer_be_edited(self):
        reference = self._create_order(status="APPROVED")
        self.auth(self.clerk)
        response = self.client.patch(
            f"/api/admin/inventory/purchase-orders/{reference}/",
            {"notes": "Too late"},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(response.json()["success"])
        order = PurchaseOrder.objects.get(reference=reference)
        self.assertNotEqual(order.notes, "Too late")

    def test_an_order_with_received_stock_can_no_longer_be_edited(self):
        reference = self._create_order(status="APPROVED")
        order = PurchaseOrder.objects.get(reference=reference)
        line = order.lines.get()
        mark_purchase_order_ordered(purchase_order=order, actor=self.clerk)
        receive_goods(
            purchase_order=order, actor=self.clerk, source_key="edit-receipt-1",
            receipt_lines=[{"purchase_order_line_id": line.pk, "quantity_received": "1"}],
        )
        self.auth(self.clerk)
        response = self.client.patch(
            f"/api/admin/inventory/purchase-orders/{reference}/",
            {"notes": "Should be refused"},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)

    def test_edit_is_refused_without_the_procurement_capability(self):
        reference = self._create_order(status="DRAFT")
        watcher = make_staff("po-edit-housekeeping@staff.dev", role=User.Role.HOUSEKEEPER)
        self.auth(watcher)
        response = self.client.patch(
            f"/api/admin/inventory/purchase-orders/{reference}/",
            {"notes": "Not mine to change"},
            format="json",
        )
        self.assertIn(response.status_code, (403, 400), response.content)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="jone-test-media-"))
class PurchaseOrderReceiptTests(BaseAPITestCase):
    """Approval and the later receipt upload both file the supplier document."""

    @classmethod
    def tearDownClass(cls):
        from django.conf import settings

        shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.clerk = make_staff("po-receipt-clerk@staff.dev", role=User.Role.INVENTORY_CLERK)
        self.manager = make_staff("po-receipt-manager@staff.dev", role=User.Role.MANAGER)
        self.supplier = Supplier.objects.create(reference="SUP-RCPT-1", name="Receipt Supplies Ltd")
        self.location = StockLocation.objects.create(code="RCPT-STORE", name="Receipt Store")
        self.item = StockItem.objects.create(sku="RCPT-FLOUR", name="Flour", base_unit="PIECE")
        self.order, _ = create_purchase_order(
            supplier=self.supplier, delivery_location=self.location, requested_by=self.clerk,
            lines=[{"item_id": self.item.pk, "quantity_ordered": "10", "unit_cost": "100"}],
            idempotency_key="receipt-po-one",
        )
        submit_purchase_order(purchase_order=self.order, actor=self.clerk)

    def test_approval_accepts_a_payment_reference_and_invoice_file(self):
        self.auth(self.manager)
        response = self.client.post(
            f"/api/admin/inventory/purchase-orders/{self.order.reference}/approve/",
            {"payment_reference": "TRF-889900", "approval_receipt": pdf_upload()},
            format="multipart",
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["status"], PurchaseOrder.Status.APPROVED)
        self.assertEqual(data["payment_reference"], "TRF-889900")
        self.assertTrue(data["approval_receipt_url"])
        self.assertTrue(data["approval_receipt_uploaded_at"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.approval_receipt_uploaded_by, self.manager)

    def test_approval_without_a_receipt_still_works(self):
        self.auth(self.manager)
        response = self.client.post(
            f"/api/admin/inventory/purchase-orders/{self.order.reference}/approve/", {}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["status"], PurchaseOrder.Status.APPROVED)
        self.order.refresh_from_db()
        self.assertFalse(self.order.approval_receipt)

    def test_a_receipt_can_be_uploaded_after_approval(self):
        self.auth(self.manager)
        self.client.post(
            f"/api/admin/inventory/purchase-orders/{self.order.reference}/approve/", {}, format="json",
        )
        response = self.client.post(
            f"/api/admin/inventory/purchase-orders/{self.order.reference}/approval-receipt/",
            {"approval_receipt": pdf_upload("bank-transfer.pdf"), "payment_reference": "TRF-LATER-1"},
            format="multipart",
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertTrue(data["approval_receipt_url"])
        self.assertEqual(data["payment_reference"], "TRF-LATER-1")

    def test_a_disguised_executable_is_refused(self):
        self.auth(self.manager)
        payload = SimpleUploadedFile("invoice.pdf", b"MZ\x90\x00not-a-pdf", content_type="application/pdf")
        response = self.client.post(
            f"/api/admin/inventory/purchase-orders/{self.order.reference}/approve/",
            {"approval_receipt": payload},
            format="multipart",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, PurchaseOrder.Status.SUBMITTED)
        self.assertFalse(self.order.approval_receipt)

    def test_an_unsupported_extension_is_refused(self):
        self.auth(self.manager)
        payload = SimpleUploadedFile("invoice.exe", b"MZ\x90\x00binary", content_type="application/octet-stream")
        response = self.client.post(
            f"/api/admin/inventory/purchase-orders/{self.order.reference}/approval-receipt/",
            {"approval_receipt": payload},
            format="multipart",
        )
        self.assertEqual(response.status_code, 400, response.content)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="jone-test-media-"))
class ExpenseReceiptUploadTests(BaseAPITestCase):
    """An expense accepts an optional invoice/receipt at creation time."""

    @classmethod
    def tearDownClass(cls):
        from django.conf import settings

        shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.accountant = make_staff("expense-receipt@staff.dev", role=User.Role.ACCOUNTANT)

    def _base_payload(self, key):
        return {
            "category": "Utilities",
            "amount": "12500.00",
            "payment_method": "BANK_TRANSFER",
            "payee": "AEDC",
            "incurred_on": "2026-10-17",
            "idempotency_key": key,
        }

    def test_an_expense_can_be_created_with_its_invoice(self):
        self.auth(self.accountant)
        payload = self._base_payload("expense-with-receipt")
        response = self.client.post(
            "/api/admin/finance/expenses/",
            {**payload, "receipt_file": pdf_upload("aedc-bill.pdf")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()["data"]
        self.assertTrue(data["receipt_file_url"])
        expense = Expense.objects.get(reference=data["reference"])
        self.assertTrue(expense.receipt_file.name.endswith(".pdf"))

    def test_an_expense_without_a_receipt_is_unchanged(self):
        self.auth(self.accountant)
        response = self.client.post(
            "/api/admin/finance/expenses/", self._base_payload("expense-no-receipt"), format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertIn(response.json()["data"]["receipt_file_url"], (None, ""))
        expense = Expense.objects.get(reference=response.json()["data"]["reference"])
        self.assertFalse(expense.receipt_file)

    def test_a_bad_attachment_is_refused_before_the_expense_is_written(self):
        self.auth(self.accountant)
        payload = self._base_payload("expense-bad-receipt")
        before = Expense.objects.count()
        response = self.client.post(
            "/api/admin/finance/expenses/",
            {**payload, "receipt_file": SimpleUploadedFile("bill.exe", b"MZ\x90\x00", content_type="application/octet-stream")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(Expense.objects.count(), before)
