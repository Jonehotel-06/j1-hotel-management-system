"""Procurement draft → approval → receipt → payable → Accounting hand-off.

Pins the fixes of the procurement audit (2026-10-29):

* a Procurement Officer (``procurement.manage`` only) can search the delivery
  locations and stock items a purchase order needs, through read-only,
  procurement-scoped lookups — the inventory directories stay inventory-only;
* an idempotency key replays exactly ONE create request: the same payload
  returns the stored draft (no duplicate), a changed payload is refused;
* unknown/inactive suppliers and locations, bad quantities/costs, duplicate
  and inactive item lines are refused server-side with field-level messages
  that name the offending item;
* approval keeps its maker-checker rule; rejection returns a submitted order
  to DRAFT with a reason; cancellation keeps the record (never deletes);
* approval never receives goods, a receipt never pays the supplier, and the
  derived receipt/payment progress stays truthful through partial receipts
  and partial payments;
* Accounting can read submitted orders (never drafts) with totals, receipts
  and payments, read-only, without opening procurement to cashiers/waiters.

Run: python manage.py test tests.test_procurement_agent_workflow
"""
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.inventory.models import (
    GoodsReceiptLine, PurchaseOrder, PurchaseOrderLine, StockItem, StockLocation, Supplier,
)

from .base import BaseAPITestCase
from .factories import make_staff

PO_URL = "/api/admin/inventory/purchase-orders/"
LOOKUP_LOCATIONS = "/api/admin/inventory/procurement/locations/"
LOOKUP_ITEMS = "/api/admin/inventory/procurement/items/"
ACCOUNTING_URL = "/api/admin/inventory/accounting/purchase-orders/"


class ProcurementWorkflowBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.officer = make_staff("agent-officer@staff.dev", role=User.Role.PROCUREMENT_OFFICER)
        self.storekeeper = make_staff("agent-store@staff.dev", role=User.Role.STOREKEEPER)
        self.gm = make_staff("agent-gm@staff.dev", role=User.Role.GENERAL_MANAGER)
        self.accountant = make_staff("agent-accountant@staff.dev", role=User.Role.ACCOUNTANT)
        self.waiter = make_staff("agent-waiter@staff.dev", role=User.Role.WAITER)
        self.supplier = Supplier.objects.create(reference="SUP-AGENT-1", name="Agent Beverages Ltd")
        self.dormant_supplier = Supplier.objects.create(
            reference="SUP-AGENT-2", name="Dormant Foods Ltd", status=Supplier.Status.INACTIVE,
        )
        self.location = StockLocation.objects.create(code="AGENT-STORE", name="Agent main store")
        self.closed_location = StockLocation.objects.create(code="AGENT-OLD", name="Old store", is_active=False)
        self.water = StockItem.objects.create(
            sku="AG-WATER", name="Bottled water 75cl", base_unit="BOTTLE",
            units_per_pack=Decimal("24"), pack_price=Decimal("2400"), standard_cost=Decimal("100"),
        )
        self.rice = StockItem.objects.create(sku="AG-RICE", name="Rice 50kg", base_unit="BAG", standard_cost=Decimal("65000"))
        self.retired = StockItem.objects.create(sku="AG-RETIRED", name="Discontinued juice", is_active=False)

    # ------------------------------------------------------------------ helpers
    def payload(self, **overrides):
        data = {
            "supplier_id": self.supplier.pk,
            "delivery_location_id": self.location.pk,
            "supplier_reference": "QUOTE-77",
            "notes": "Weekly restock",
            "lines": [
                {"item_id": self.water.pk, "quantity_ordered": "48", "unit_cost": "41.67", "notes": ""},
                {"item_id": self.rice.pk, "quantity_ordered": "2", "unit_cost": "65000", "notes": ""},
            ],
        }
        data.update(overrides)
        return data

    def create(self, user=None, **overrides):
        self.auth(user or self.officer)
        return self.client.post(PO_URL, self.payload(**overrides), format="json")

    def create_ref(self, user=None, **overrides):
        response = self.create(user, **overrides)
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["data"]["reference"]

    def action(self, user, reference, name, body=None):
        self.auth(user)
        return self.client.post(f"{PO_URL}{reference}/{name}/", body or {}, format="json")

    def approved_ref(self, **overrides):
        reference = self.create_ref(**overrides)
        self.assertEqual(self.action(self.officer, reference, "submit").status_code, 200)
        response = self.action(self.gm, reference, "approve")
        self.assertEqual(response.status_code, 200, response.content)
        return reference

    def detail(self, reference, user=None):
        self.auth(user or self.gm)
        response = self.client.get(f"{PO_URL}{reference}/")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]

    def receive(self, reference, quantities, *, key, unit_cost=None):
        order = self.detail(reference)
        by_item = {line["item_sku"]: line["id"] for line in order["lines"]}
        lines = []
        for sku, quantity in quantities.items():
            line = {"purchase_order_line_id": by_item[sku], "quantity_received": quantity}
            if unit_cost is not None:
                line["unit_cost"] = unit_cost
            lines.append(line)
        return self.action(self.storekeeper, reference, "receipts", {"idempotency_key": key, "lines": lines})

    def pay(self, reference, amount, reference_text):
        self.auth(self.gm)
        return self.client.post(
            f"{PO_URL}{reference}/pay/",
            {"amount": amount, "method": "BANK_TRANSFER", "reference": reference_text},
            format="json",
        )


class ProcurementLookupTests(ProcurementWorkflowBase):
    def test_procurement_officer_can_search_locations_and_items_and_create_a_draft(self):
        self.auth(self.officer)
        locations = self.client.get(LOOKUP_LOCATIONS, {"search": "AGENT"})
        items = self.client.get(LOOKUP_ITEMS, {"search": "AG-"})
        self.assertEqual(locations.status_code, 200, locations.content)
        self.assertEqual(items.status_code, 200, items.content)
        self.assertEqual([row["code"] for row in locations.json()["data"]], ["AGENT-STORE"])
        self.assertEqual(sorted(row["sku"] for row in items.json()["data"]), ["AG-RICE", "AG-WATER"])

        response = self.create(self.officer)
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()["data"]
        self.assertTrue(data["reference"].startswith("PO-"))
        self.assertEqual(data["status"], "DRAFT")
        self.assertIsNone(data["approved_by_email"])

    def test_lookups_expose_purchase_side_fields_only(self):
        self.auth(self.officer)
        water = next(row for row in self.client.get(LOOKUP_ITEMS, {"search": "AG-WATER"}).json()["data"])
        self.assertEqual(Decimal(water["units_per_pack"]), Decimal("24"))
        self.assertEqual(water["base_unit"], "BOTTLE")
        self.assertEqual(Decimal(water["standard_cost"]), Decimal("100"))
        for selling_side in ("direct_sale_menu_item", "direct_sale_quantity", "direct_sale_location", "price"):
            self.assertNotIn(selling_side, water)
        location = self.client.get(LOOKUP_LOCATIONS).json()["data"][0]
        self.assertEqual(set(location), {"id", "code", "name", "is_active"})

    def test_lookups_never_offer_inactive_records(self):
        self.auth(self.officer)
        self.assertNotIn("AGENT-OLD", [row["code"] for row in self.client.get(LOOKUP_LOCATIONS).json()["data"]])
        self.assertNotIn("AG-RETIRED", [row["sku"] for row in self.client.get(LOOKUP_ITEMS).json()["data"]])

    def test_lookups_need_the_procurement_capability_and_are_read_only(self):
        for user in (self.accountant, self.waiter):
            self.auth(user)
            self.assertEqual(self.client.get(LOOKUP_ITEMS).status_code, 403)
            self.assertEqual(self.client.get(LOOKUP_LOCATIONS).status_code, 403)
        self.auth(self.officer)
        self.assertEqual(self.client.post(LOOKUP_ITEMS, {"sku": "X", "name": "X"}, format="json").status_code, 405)
        self.assertEqual(self.client.post(LOOKUP_LOCATIONS, {"code": "X", "name": "X"}, format="json").status_code, 405)

    def test_inventory_directories_were_not_widened(self):
        self.auth(self.officer)
        self.assertEqual(self.client.get("/api/admin/inventory/items/").status_code, 403)
        self.assertEqual(self.client.get("/api/admin/inventory/locations/").status_code, 403)


class DraftValidationTests(ProcurementWorkflowBase):
    def test_required_fields_are_reported_per_field(self):
        self.auth(self.officer)
        response = self.client.post(PO_URL, {"notes": "nothing else"}, format="json")
        self.assertEqual(response.status_code, 400)
        errors = response.json()["errors"]
        for field in ("supplier_id", "delivery_location_id", "lines"):
            self.assertIn(field, errors)
        empty_lines = self.create(lines=[])
        self.assertEqual(empty_lines.status_code, 400)
        self.assertIn("lines", empty_lines.json()["errors"])
        self.assertEqual(PurchaseOrder.objects.count(), 0)

    def test_unknown_or_inactive_supplier_and_location_are_field_errors(self):
        cases = [
            ({"supplier_id": 999999}, "supplier_id"),
            ({"delivery_location_id": 999999}, "delivery_location_id"),
            ({"supplier_id": self.dormant_supplier.pk}, "supplier_id"),
            ({"delivery_location_id": self.closed_location.pk}, "delivery_location_id"),
        ]
        for overrides, field in cases:
            with self.subTest(field=field, overrides=overrides):
                response = self.create(**overrides)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn(field, response.json()["errors"])
        self.assertEqual(PurchaseOrder.objects.count(), 0)

    def test_invalid_quantities_and_costs_are_refused_by_the_server(self):
        bad_lines = [
            {"item_id": self.rice.pk, "quantity_ordered": "0", "unit_cost": "10"},
            {"item_id": self.rice.pk, "quantity_ordered": "-3", "unit_cost": "10"},
            {"item_id": self.rice.pk, "quantity_ordered": "1", "unit_cost": "-0.01"},
            {"item_id": self.rice.pk, "quantity_ordered": "99999999999", "unit_cost": "10"},
            {"item_id": self.rice.pk, "quantity_ordered": "1.00001", "unit_cost": "10"},
            {"item_id": self.rice.pk, "quantity_ordered": "abc", "unit_cost": "10"},
        ]
        for line in bad_lines:
            with self.subTest(line=line):
                response = self.create(lines=[line])
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn("lines", response.json()["errors"])
        self.assertEqual(PurchaseOrder.objects.count(), 0)

    def test_duplicate_item_lines_are_refused_naming_the_item(self):
        response = self.create(lines=[
            {"item_id": self.rice.pk, "quantity_ordered": "1", "unit_cost": "10"},
            {"item_id": self.rice.pk, "quantity_ordered": "2", "unit_cost": "10"},
        ])
        self.assertEqual(response.status_code, 400, response.content)
        message = " ".join(response.json()["errors"]["lines"])
        self.assertIn("AG-RICE", message)
        self.assertEqual(PurchaseOrder.objects.count(), 0)

    def test_inactive_or_missing_items_are_refused_naming_them(self):
        response = self.create(lines=[
            {"item_id": self.retired.pk, "quantity_ordered": "1", "unit_cost": "10"},
            {"item_id": 987654, "quantity_ordered": "1", "unit_cost": "10"},
        ])
        self.assertEqual(response.status_code, 400, response.content)
        message = " ".join(response.json()["errors"]["lines"])
        self.assertIn("AG-RETIRED", message)
        self.assertIn("item #987654", message)
        self.assertEqual(PurchaseOrder.objects.count(), 0)

    def test_permission_failures_are_explicit(self):
        for user in (self.accountant, self.waiter):
            with self.subTest(role=user.role):
                response = self.create(user)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["code"], "FORBIDDEN")
        self.assertEqual(PurchaseOrder.objects.count(), 0)


class DuplicateSubmissionTests(ProcurementWorkflowBase):
    def test_replaying_the_same_request_returns_the_same_draft(self):
        first = self.create(idempotency_key="dialog-key-1")
        second = self.create(idempotency_key="dialog-key-1")
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(first.json()["data"]["reference"], second.json()["data"]["reference"])
        self.assertEqual(second.json()["message"], "Existing purchase order returned.")
        self.assertEqual(PurchaseOrder.objects.count(), 1)
        self.assertEqual(AuditLog.objects.filter(action="PURCHASE_ORDER_CREATED").count(), 1)

    def test_a_key_cannot_replay_different_details(self):
        reference = self.create(idempotency_key="dialog-key-2").json()["data"]["reference"]
        changed = self.create(
            idempotency_key="dialog-key-2",
            lines=[{"item_id": self.rice.pk, "quantity_ordered": "3", "unit_cost": "65000", "notes": ""}],
        )
        self.assertEqual(changed.status_code, 400, changed.content)
        message = " ".join(changed.json()["errors"]["idempotency_key"])
        self.assertIn(reference, message)
        self.assertEqual(PurchaseOrder.objects.count(), 1)
        self.assertEqual(PurchaseOrderLine.objects.filter(purchase_order__reference=reference).count(), 2)

    def test_replay_is_tolerant_of_equivalent_number_formats(self):
        self.create(idempotency_key="dialog-key-3")
        same = self.create(idempotency_key="dialog-key-3", lines=[
            {"item_id": self.rice.pk, "quantity_ordered": "2.0000", "unit_cost": "65000.00", "notes": ""},
            {"item_id": self.water.pk, "quantity_ordered": "48.0", "unit_cost": "41.67", "notes": ""},
        ])
        self.assertEqual(same.status_code, 200, same.content)
        self.assertEqual(PurchaseOrder.objects.count(), 1)

    def test_a_retry_still_returns_the_draft_after_the_supplier_is_deactivated(self):
        reference = self.create(idempotency_key="dialog-key-4").json()["data"]["reference"]
        Supplier.objects.filter(pk=self.supplier.pk).update(status=Supplier.Status.INACTIVE)
        retry = self.create(idempotency_key="dialog-key-4")
        self.assertEqual(retry.status_code, 200, retry.content)
        self.assertEqual(retry.json()["data"]["reference"], reference)
        self.assertEqual(PurchaseOrder.objects.count(), 1)

    def test_keys_are_scoped_to_the_person(self):
        mine = self.create(self.officer, idempotency_key="shared-key")
        theirs = self.create(self.storekeeper, idempotency_key="shared-key")
        self.assertEqual(mine.status_code, 201)
        self.assertEqual(theirs.status_code, 201)
        self.assertNotEqual(mine.json()["data"]["reference"], theirs.json()["data"]["reference"])

    def test_without_a_key_each_request_is_a_new_draft(self):
        self.create()
        self.create()
        self.assertEqual(PurchaseOrder.objects.count(), 2)


class DraftEditingTests(ProcurementWorkflowBase):
    def test_a_draft_edit_replaces_lines_and_the_total_follows(self):
        reference = self.create_ref()
        self.auth(self.officer)
        response = self.client.patch(f"{PO_URL}{reference}/", {
            "lines": [{"item_id": self.rice.pk, "quantity_ordered": "3", "unit_cost": "64000"}], "notes": "",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual([line["item_sku"] for line in data["lines"]], ["AG-RICE"])
        self.assertEqual(data["total_amount"], "192000.00")
        self.assertEqual(data["status"], "DRAFT")

    def test_an_edit_to_an_unknown_supplier_is_a_field_error(self):
        reference = self.create_ref()
        self.auth(self.officer)
        response = self.client.patch(f"{PO_URL}{reference}/", {"supplier_id": 424242}, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("supplier_id", response.json()["errors"])

    def test_a_cancelled_order_cannot_be_edited(self):
        reference = self.create_ref()
        self.assertEqual(self.action(self.officer, reference, "cancel", {"reason": "Duplicate draft"}).status_code, 200)
        self.auth(self.officer)
        response = self.client.patch(f"{PO_URL}{reference}/", {"notes": "late edit"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("cancelled", response.content.decode().lower())

    def test_an_approved_order_cannot_be_edited(self):
        reference = self.approved_ref()
        self.auth(self.officer)
        response = self.client.patch(f"{PO_URL}{reference}/", {
            "lines": [{"item_id": self.rice.pk, "quantity_ordered": "99", "unit_cost": "1"}],
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.detail(reference)["total_amount"], "132000.16")


class ApprovalAndRejectionTests(ProcurementWorkflowBase):
    def test_a_draft_is_neither_approved_nor_received_on_creation(self):
        data = self.create().json()["data"]
        self.assertEqual(data["status"], "DRAFT")
        self.assertIsNone(data["approved_at"])
        self.assertEqual(data["receipt_progress"], "NOT_RECEIVED")
        self.assertEqual(data["payment_progress"], "UNPAID")
        # A draft cannot skip review.
        response = self.action(self.gm, data["reference"], "approve")
        self.assertEqual(response.status_code, 400)

    def test_submission_needs_procurement_rights(self):
        reference = self.create_ref()
        self.assertEqual(self.action(self.accountant, reference, "submit").status_code, 403)
        self.assertEqual(self.action(self.officer, reference, "submit").status_code, 200)
        self.assertEqual(self.detail(reference)["status"], "SUBMITTED")

    def test_approval_needs_the_approver_capability_and_a_different_person(self):
        reference = self.create_ref(self.gm)
        self.assertEqual(self.action(self.gm, reference, "submit").status_code, 200)
        self.assertEqual(self.action(self.officer, reference, "approve").status_code, 403)
        own = self.action(self.gm, reference, "approve")
        self.assertEqual(own.status_code, 403, own.content)
        self.assertIn("cannot approve their own", own.json()["message"])
        self.assertEqual(self.detail(reference)["status"], "SUBMITTED")

        other = self.create_ref(self.officer)
        self.action(self.officer, other, "submit")
        approved = self.action(self.gm, other, "approve")
        self.assertEqual(approved.status_code, 200, approved.content)
        data = approved.json()["data"]
        self.assertEqual(data["status"], "APPROVED")
        self.assertEqual(data["receipt_progress"], "NOT_RECEIVED")  # approved is not received
        self.assertEqual(data["payment_progress"], "UNPAID")        # approved is not paid

    def test_rejection_returns_the_order_to_draft_with_the_reason(self):
        reference = self.create_ref()
        self.action(self.officer, reference, "submit")
        response = self.action(self.gm, reference, "reject", {"reason": "Prices above the agreed quote."})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["status"], "DRAFT")
        self.assertIsNone(data["approved_by_email"])
        self.assertIn("Prices above the agreed quote.", data["notes"])
        self.assertIn("agent-gm@staff.dev", data["notes"])
        log = AuditLog.objects.get(action="PURCHASE_ORDER_REJECTED")
        self.assertEqual(log.metadata["reference"], reference)
        # The requester can correct and resubmit.
        self.assertEqual(self.action(self.officer, reference, "submit").status_code, 200)

    def test_rejection_needs_a_reason_the_right_role_and_a_submitted_order(self):
        reference = self.create_ref()
        self.assertEqual(self.action(self.gm, reference, "reject", {"reason": "x"}).status_code, 400)  # still DRAFT
        self.action(self.officer, reference, "submit")
        self.assertEqual(self.action(self.officer, reference, "reject", {"reason": "x"}).status_code, 403)
        self.assertEqual(self.action(self.gm, reference, "reject", {"reason": "   "}).status_code, 400)
        self.assertEqual(self.action(self.gm, reference, "reject", {}).status_code, 400)
        self.assertEqual(self.detail(reference)["status"], "SUBMITTED")

    def test_cancellation_keeps_the_record_and_ends_the_workflow(self):
        reference = self.create_ref()
        response = self.action(self.officer, reference, "cancel", {"reason": "Raised twice by mistake"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["status"], "CANCELLED")
        self.assertTrue(PurchaseOrder.objects.filter(reference=reference).exists())
        self.assertEqual(PurchaseOrderLine.objects.filter(purchase_order__reference=reference).count(), 2)
        self.assertEqual(self.action(self.officer, reference, "submit").status_code, 400)
        replay = self.action(self.officer, reference, "cancel", {"reason": "again"})
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(AuditLog.objects.filter(action="PURCHASE_ORDER_CANCELLED").count(), 1)

    def test_an_approved_order_cannot_be_cancelled_here(self):
        reference = self.approved_ref()
        response = self.action(self.gm, reference, "cancel", {"reason": "Supplier failed"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.detail(reference)["status"], "APPROVED")

    def test_submit_and_ordered_transitions_are_audited_once(self):
        reference = self.create_ref()
        self.action(self.officer, reference, "submit")
        self.action(self.officer, reference, "submit")  # idempotent replay
        self.action(self.gm, reference, "approve")
        self.action(self.officer, reference, "ordered")
        self.assertEqual(AuditLog.objects.filter(action="PURCHASE_ORDER_SUBMITTED").count(), 1)
        self.assertEqual(AuditLog.objects.filter(action="PURCHASE_ORDER_ORDERED").count(), 1)


class CostAndUnitTests(ProcurementWorkflowBase):
    def test_the_total_is_the_sum_of_exact_line_products_rounded_half_up_once(self):
        data = self.create(lines=[
            {"item_id": self.rice.pk, "quantity_ordered": "0.3333", "unit_cost": "0.15"},   # 0.049995
            {"item_id": self.water.pk, "quantity_ordered": "1", "unit_cost": "0.01"},
        ]).json()["data"]
        # 0.049995 + 0.01 = 0.059995 -> 0.06 (rounded once, at the order level)
        self.assertEqual(data["total_amount"], "0.06")
        self.assertEqual(data["outstanding_amount"], "0.06")

    def test_pack_quantities_arrive_in_base_units_and_rounding_is_visible_in_the_total(self):
        # The page converts 2 packs x NGN 1,000 (24 per pack) into 48 bottles at
        # NGN 41.67, because the order stores cost per BASE unit to 2 decimals.
        data = self.create(lines=[{"item_id": self.water.pk, "quantity_ordered": "48", "unit_cost": "41.67"}]).json()["data"]
        line = data["lines"][0]
        self.assertEqual(Decimal(line["quantity_ordered"]), Decimal("48"))
        self.assertEqual(line["base_unit"], "BOTTLE")
        self.assertEqual(Decimal(line["units_per_pack"]), Decimal("24"))
        self.assertEqual(data["total_amount"], "2000.16")

    def test_purchase_prices_and_item_identity_are_snapshotted_on_the_order(self):
        reference = self.create_ref()
        StockItem.objects.filter(pk=self.water.pk).update(
            name="Water (renamed)", sku="AG-WATER-NEW", standard_cost=Decimal("150"), pack_price=Decimal("3600"),
        )
        data = self.detail(reference)
        water = next(line for line in data["lines"] if line["item"] == self.water.pk)
        self.assertEqual(water["item_sku"], "AG-WATER")
        self.assertEqual(water["item_name"], "Bottled water 75cl")
        self.assertEqual(Decimal(water["unit_cost"]), Decimal("41.67"))
        self.assertEqual(data["total_amount"], "132000.16")

    def test_a_receipt_at_a_different_price_keeps_the_ordered_price(self):
        reference = self.approved_ref()
        response = self.receive(reference, {"AG-RICE": "2"}, key="price-change", unit_cost="66000")
        self.assertEqual(response.status_code, 201, response.content)
        data = self.detail(reference)
        rice = next(line for line in data["lines"] if line["item_sku"] == "AG-RICE")
        self.assertEqual(Decimal(rice["unit_cost"]), Decimal("65000"))
        self.assertEqual(data["total_amount"], "132000.16")
        self.assertEqual(GoodsReceiptLine.objects.get(item=self.rice).unit_cost, Decimal("66000.00"))


class ReceiptAndPayableLinkageTests(ProcurementWorkflowBase):
    def test_a_receipt_never_pays_and_a_payment_never_receives(self):
        reference = self.approved_ref()
        self.assertEqual(self.receive(reference, {"AG-RICE": "1"}, key="r-1").status_code, 201)
        data = self.detail(reference)
        self.assertEqual(data["status"], "PARTIALLY_RECEIVED")
        self.assertEqual(data["receipt_progress"], "PARTIALLY_RECEIVED")
        self.assertEqual(data["payment_progress"], "UNPAID")
        self.assertEqual(data["outstanding_amount"], data["total_amount"])

        self.assertEqual(self.pay(reference, "50000.00", "TRF-1").status_code, 200)
        data = self.detail(reference)
        self.assertEqual(data["status"], "PARTIALLY_RECEIVED")
        self.assertEqual(data["payment_progress"], "PARTIALLY_PAID")
        self.assertEqual(data["outstanding_amount"], "82000.16")

    def test_partial_receipts_then_the_rest(self):
        reference = self.approved_ref()
        self.receive(reference, {"AG-WATER": "24"}, key="r-a")
        self.assertEqual(self.detail(reference)["receipt_progress"], "PARTIALLY_RECEIVED")
        self.receive(reference, {"AG-WATER": "24", "AG-RICE": "2"}, key="r-b")
        data = self.detail(reference)
        self.assertEqual(data["status"], "RECEIVED")
        self.assertEqual(data["receipt_progress"], "RECEIVED")
        self.assertEqual(data["payment_progress"], "UNPAID")
        over = self.receive(reference, {"AG-RICE": "1"}, key="r-c")
        self.assertEqual(over.status_code, 400)

    def test_partial_payments_then_the_balance(self):
        reference = self.approved_ref()
        self.receive(reference, {"AG-WATER": "48", "AG-RICE": "2"}, key="r-full")
        self.assertEqual(self.pay(reference, "100000.00", "TRF-A").status_code, 200)
        self.assertEqual(self.detail(reference)["payment_progress"], "PARTIALLY_PAID")
        self.assertEqual(self.pay(reference, "50000.00", "TRF-B").status_code, 400)  # more than outstanding
        self.assertEqual(self.pay(reference, "32000.16", "TRF-A").status_code, 400)  # same transfer twice
        self.assertEqual(self.pay(reference, "32000.16", "TRF-C").status_code, 200)
        data = self.detail(reference)
        self.assertEqual(data["status"], "PAID")
        self.assertEqual(data["payment_progress"], "PAID")
        self.assertEqual(data["outstanding_amount"], "0.00")
        self.assertEqual(len(data["payments"]), 2)

    def test_an_order_paid_before_delivery_still_shows_the_goods_due(self):
        reference = self.approved_ref()
        self.assertEqual(self.pay(reference, "132000.16", "ADV-1").status_code, 200)
        data = self.detail(reference)
        self.assertEqual(data["status"], "PAID")
        self.assertEqual(data["receipt_progress"], "NOT_RECEIVED")
        self.receive(reference, {"AG-WATER": "48"}, key="after-pay")
        data = self.detail(reference)
        self.assertEqual(data["status"], "PAID")
        self.assertEqual(data["receipt_progress"], "PARTIALLY_RECEIVED")

    def test_unapproved_or_cancelled_orders_cannot_be_paid(self):
        reference = self.create_ref()
        self.assertEqual(self.pay(reference, "1.00", "T-1").status_code, 400)
        self.action(self.officer, reference, "cancel", {"reason": "not needed"})
        self.assertEqual(self.pay(reference, "1.00", "T-2").status_code, 400)


class AccountingHandoffTests(ProcurementWorkflowBase):
    def test_drafts_are_never_shown_to_accounting(self):
        reference = self.create_ref()
        self.auth(self.accountant)
        self.assertEqual(self.client.get(ACCOUNTING_URL).json()["data"], [])
        self.assertEqual(self.client.get(f"{ACCOUNTING_URL}{reference}/").status_code, 404)
        self.assertEqual(self.client.get(ACCOUNTING_URL, {"status": "DRAFT"}).status_code, 400)

    def test_a_submitted_order_reaches_accounting_with_everything_needed_to_verify_it(self):
        reference = self.approved_ref()
        self.receive(reference, {"AG-RICE": "1"}, key="acc-r")
        self.pay(reference, "65000.00", "ACC-TRF-1")
        self.auth(self.accountant)
        listed = self.client.get(ACCOUNTING_URL, {"search": "Agent Beverages"})
        self.assertEqual(listed.status_code, 200, listed.content)
        self.assertEqual([row["reference"] for row in listed.json()["data"]], [reference])
        data = self.client.get(f"{ACCOUNTING_URL}{reference}/").json()["data"]
        self.assertEqual(data["status"], "PARTIALLY_RECEIVED")
        self.assertEqual(data["supplier_name"], "Agent Beverages Ltd")
        self.assertEqual(data["total_amount"], "132000.16")
        self.assertEqual(data["total_paid"], "65000.00")
        self.assertEqual(data["outstanding_amount"], "67000.16")
        self.assertEqual(data["receipt_progress"], "PARTIALLY_RECEIVED")
        self.assertEqual(data["payment_progress"], "PARTIALLY_PAID")
        self.assertEqual(data["received_value"], "65000.00")
        self.assertEqual(len(data["goods_receipts"]), 1)
        self.assertEqual(data["goods_receipts"][0]["received_value"], "65000.00")
        self.assertEqual(data["payments"][0]["reference"], "ACC-TRF-1")
        self.assertEqual(data["approved_by_email"], "agent-gm@staff.dev")
        self.assertEqual(len(data["lines"]), 2)

    def test_the_hand_off_is_read_only(self):
        reference = self.approved_ref()
        self.auth(self.accountant)
        self.assertEqual(self.client.post(ACCOUNTING_URL, {}, format="json").status_code, 405)
        self.assertEqual(self.client.patch(f"{ACCOUNTING_URL}{reference}/", {"notes": "x"}, format="json").status_code, 405)
        # ...and accounting still cannot touch the procurement endpoints.
        self.assertEqual(self.client.get(PO_URL).status_code, 403)
        self.assertEqual(self.client.post(f"{PO_URL}{reference}/ordered/", {}, format="json").status_code, 403)

    def test_only_accounting_and_oversight_roles_can_read_it(self):
        self.approved_ref()
        expectations = {self.accountant: 200, self.gm: 200, self.officer: 403, self.storekeeper: 403, self.waiter: 403}
        for user, expected in expectations.items():
            with self.subTest(role=user.role):
                self.auth(user)
                self.assertEqual(self.client.get(ACCOUNTING_URL).status_code, expected)


class ListQueryBudgetTests(ProcurementWorkflowBase):
    def _queries(self, url, user):
        self.auth(user)
        # Warm the per-user capability/department cache first: those lookups
        # run once per user, not per order, and would blur the comparison.
        self.client.get(url, {"page_size": 50})
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url, {"page_size": 50})
        self.assertEqual(response.status_code, 200, response.content)
        return len(ctx.captured_queries)

    def test_list_endpoints_do_not_grow_with_the_number_of_orders(self):
        reference = self.approved_ref(idempotency_key="budget-0")
        self.receive(reference, {"AG-RICE": "1"}, key="budget-r")
        procurement_one = self._queries(PO_URL, self.gm)
        accounting_one = self._queries(ACCOUNTING_URL, self.accountant)
        for index in range(1, 6):
            other = self.approved_ref(idempotency_key=f"budget-{index}")
            self.receive(other, {"AG-RICE": "1"}, key=f"budget-r-{index}")
        self.assertEqual(self._queries(PO_URL, self.gm), procurement_one)
        self.assertEqual(self._queries(ACCOUNTING_URL, self.accountant), accounting_one)
