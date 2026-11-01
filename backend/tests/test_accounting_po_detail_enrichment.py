"""A3 — the accountant's review material is on the PO detail, and the
verified supplier purchase-order reference can be recorded from Accounting.

Before the repair the accounting PO detail modal showed the money maths and
nothing else: no request date, no procurement justification/notes, no
supporting documents, and ``supplier_reference`` (the supplier's own number
for the order — the thing the accountant verifies against the invoice) could
only be edited while the order was still a DRAFT or SUBMITTED with
Procurement. Critical repair A3:

* the accounting hand-off detail carries the request date, the procurement
  notes/justification, the approval evidence, and every recorded supplier
  payment with its own evidence and (A4) its settlement link;
* the verified supplier reference can be recorded — or corrected — from the
  accounting side at any point after submission, with an audit entry, and no
  other field is touched;
* the hand-off stays closed to roles outside the accounting/oversight gate
  (a cashier cannot record it), and drafts remain invisible.
"""
from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.inventory.models import PurchaseOrder, StockItem, StockLocation, Supplier
from apps.inventory.services.inventory_service import (
    approve_purchase_order,
    create_purchase_order,
    submit_purchase_order,
)

from .base import BaseAPITestCase
from .factories import make_staff

DETAIL_URL = "/api/admin/inventory/accounting/purchase-orders/{ref}/"
SUPPLIER_REF_URL = "/api/admin/inventory/accounting/purchase-orders/{ref}/supplier-reference/"


class AccountingPoDetailEnrichmentTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.maker = make_staff("a3-maker@staff.dev", role=User.Role.PROCUREMENT_OFFICER)
        self.manager = make_staff("a3-manager@staff.dev", role=User.Role.MANAGER)
        self.accountant = make_staff("a3-accountant@staff.dev", role=User.Role.ACCOUNTANT)
        self.supplier = Supplier.objects.create(reference="A3-SUP", name="A3 Supplies Ltd")
        self.location = StockLocation.objects.create(code="A3-STORE", name="A3 Store")
        self.item = StockItem.objects.create(sku="A3-ITEM", name="A3 Item", base_unit="piece")
        self.po, _ = create_purchase_order(
            supplier=self.supplier, delivery_location=self.location,
            requested_by=self.maker,
            lines=[{"item_id": self.item.pk, "quantity_ordered": "2", "unit_cost": "5000.00"}],
            notes="Two replacement AC compressors for the west wing (justification: room closures cost more).",
            supplier_reference="SUP-PO-778",
        )
        submit_purchase_order(purchase_order=self.po, actor=self.maker)
        approve_purchase_order(purchase_order=self.po, actor=self.manager)

    def _detail(self):
        self.auth(self.accountant)
        res = self.client.get(DETAIL_URL.format(ref=self.po.reference))
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()["data"]

    # --------------------------------------------------------- review material
    def test_the_accounting_detail_carries_the_review_material(self):
        data = self._detail()
        # Request date (the order's creation) is present.
        self.assertTrue(data.get("created_at"), "request date must be on the detail")
        # Procurement justification/notes are present.
        self.assertIn("replacement AC compressors", data.get("notes", ""))
        # The supplier reference is present for verification.
        self.assertEqual(data.get("supplier_reference"), "SUP-PO-778")
        # Supporting documents: the approval evidence field and its trail.
        self.assertIn("approval_receipt_url", data)
        self.assertIn("payment_reference", data)
        # Every recorded supplier payment carries its own evidence link and
        # (A4) the settlement link field, so the desk's records can be
        # checked against the ledger.
        self.assertIn("payments", data)
        for payment in data["payments"]:
            self.assertIn("evidence_url", payment)
            self.assertIn("accounting_settlement_reference", payment)

    # ------------------------------------------- the verified reference, recorded
    def test_the_verified_supplier_reference_is_recorded_from_accounting(self):
        self.auth(self.accountant)
        res = self.client.post(
            SUPPLIER_REF_URL.format(ref=self.po.reference),
            {"supplier_reference": "SUP-INV-9921"}, format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["data"]["supplier_reference"], "SUP-INV-9921")
        self.po.refresh_from_db()
        self.assertEqual(self.po.supplier_reference, "SUP-INV-9921")
        entry = AuditLog.objects.filter(action="PURCHASE_ORDER_SUPPLIER_REFERENCE_SET").first()
        self.assertIsNotNone(entry, "every verified-reference change is audited")

    def test_the_reference_edit_touches_nothing_else(self):
        self.po.refresh_from_db()
        before_status = self.po.status
        before_notes = self.po.notes
        self.auth(self.accountant)
        self.client.post(
            SUPPLIER_REF_URL.format(ref=self.po.reference),
            {"supplier_reference": "SUP-INV-1"}, format="json",
        )
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, before_status)
        self.assertEqual(self.po.notes, before_notes)

    def test_the_verified_reference_survives_late_stages(self):
        """The whole point: the order is past Procurement's edit window."""
        self.po.status = PurchaseOrder.Status.RECEIVED
        self.po.save(update_fields=["status", "updated_at"])
        self.auth(self.accountant)
        res = self.client.post(
            SUPPLIER_REF_URL.format(ref=self.po.reference),
            {"supplier_reference": "SUP-INV-LATE"}, format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.po.refresh_from_db()
        self.assertEqual(self.po.supplier_reference, "SUP-INV-LATE")

    # --------------------------------------------------------------- the gates
    def test_a_cashier_cannot_record_the_verified_reference(self):
        cashier = make_staff("a3-cashier@staff.dev", role=User.Role.CASHIER)
        self.auth(cashier)
        res = self.client.post(
            SUPPLIER_REF_URL.format(ref=self.po.reference),
            {"supplier_reference": "NOPE"}, format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.po.refresh_from_db()
        self.assertEqual(self.po.supplier_reference, "SUP-PO-778")

    def test_a_draft_order_is_invisible_to_the_handoff(self):
        draft, _ = create_purchase_order(
            supplier=self.supplier, delivery_location=self.location,
            requested_by=self.maker,
            lines=[{"item_id": self.item.pk, "quantity_ordered": "1", "unit_cost": "100.00"}],
        )
        self.auth(self.accountant)
        self.assertEqual(self.client.get(DETAIL_URL.format(ref=draft.reference)).status_code, 404)
        self.assertEqual(
            self.client.post(
                SUPPLIER_REF_URL.format(ref=draft.reference),
                {"supplier_reference": "X"}, format="json",
            ).status_code,
            404,
        )
