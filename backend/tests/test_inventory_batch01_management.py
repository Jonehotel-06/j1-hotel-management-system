"""Batch 01 — inventory management, stock control and reordering.

Covers the five batch-01 work items end to end through the real API:

1. Stock UNIT fields — base unit vs opening unit, no unsafe conversions, the
   item and its opening movement are one transaction, impossible unit changes
   are refused once history exists.
2. Administrator-only stock-at-hand correction — capability enforced on the
   backend, previous/new quantity + reason + actor + timestamp recorded, the
   ledger is appended to (never rewritten).
3. Searchable SKU in Add Stock Item — case-insensitive uniqueness, existing-SKU
   search, replenishment of the existing item without touching master data.
4. Search stock at hand — by name, SKU, barcode, category and location, with
   pagination and filters preserved.
5. Reordering — a reorder dialog gets the item's details from the server, the
   total is computed exactly, the purchase-order draft keeps the existing
   approval workflow, and creating it never increases stock.

Run: python manage.py test tests.test_inventory_batch01_management
"""
from decimal import Decimal

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.inventory.models import (
    PurchaseOrder, PurchaseOrderLine, StockBalance, StockBalanceAdjustment, StockItem, StockLocation, StockMovement,
)

from .base import BaseAPITestCase
from .factories import make_staff

ITEMS = "/api/admin/inventory/items/"
BALANCES = "/api/admin/inventory/balances/"
ADJUSTMENTS = "/api/admin/inventory/adjustments/"


class Batch01Base(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.clerk = make_staff("b1-clerk@staff.dev", role=User.Role.INVENTORY_CLERK)
        self.admin = make_staff("b1-admin@staff.dev", role=User.Role.ADMIN)
        self.manager = make_staff("b1-manager@staff.dev", role=User.Role.MANAGER)
        self.procurement = make_staff("b1-procurement@staff.dev", role=User.Role.PROCUREMENT_OFFICER)
        self.cashier = make_staff("b1-cashier@staff.dev", role=User.Role.CASHIER)
        self.store = StockLocation.objects.create(code="B1-MAIN", name="Batch 01 main store")
        self.bar = StockLocation.objects.create(code="B1-BAR", name="Batch 01 bar store")
        self.auth(self.clerk)

    def create_item(self, **overrides):
        payload = {
            "sku": "B1-WATER", "name": "Bottled water", "category": "Bar drinks",
            "base_unit": "PIECE", "units_per_pack": "24", "standard_cost": "50.00",
        }
        payload.update(overrides)
        return self.client.post(ITEMS, payload, format="json")

    def item(self, **overrides):
        response = self.create_item(**overrides)
        self.assertEqual(response.status_code, 201, response.content)
        return StockItem.objects.get(pk=response.json()["data"]["id"])


class StockUnitFieldTests(Batch01Base):
    """Task 1 — base unit vs opening unit, and exact conversions."""

    def test_opening_quantity_is_entered_in_its_own_unit_and_stored_in_the_base_unit(self):
        response = self.client.post(ITEMS, {
            "sku": "B1-CRATE", "name": "Water crate", "base_unit": "PIECE", "units_per_pack": "24",
            "standard_cost": "50.00", "opening_quantity": "3", "opening_location_id": self.store.pk,
            "opening_unit": "CRATE", "opening_conversion_factor": "24",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        item = StockItem.objects.get(sku="B1-CRATE")
        balance = StockBalance.objects.get(item=item, location=self.store)
        self.assertEqual(balance.quantity_on_hand, Decimal("72.0000"))
        movement = StockMovement.objects.get(item=item)
        # The ledger holds the base-unit quantity; what was typed is evidence.
        self.assertEqual(movement.quantity_delta, Decimal("72.0000"))
        self.assertEqual(Decimal(movement.metadata["entered_quantity"]), Decimal("3"))
        self.assertEqual(movement.metadata["entered_unit"], "CRATE")
        self.assertEqual(movement.metadata["base_unit"], "PIECE")
        self.assertEqual(item.base_unit, "PIECE", "the base unit is what balances count")

    def test_mass_and_volume_convert_exactly_inside_their_own_family(self):
        rice = self.item(sku="B1-RICE", name="Rice", base_unit="KG", category="Kitchen", units_per_pack=None)
        response = self.client.post("/api/admin/inventory/opening-stock/", {
            "item_id": rice.pk, "location_id": self.store.pk, "quantity": "2.5", "unit": "kg",
            "idempotency_key": "b1-rice-open",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(StockBalance.objects.get(item=rice, location=self.store).quantity_on_hand, Decimal("2.5000"))

        oil = self.item(sku="B1-OIL", name="Cooking oil", base_unit="L", category="Kitchen", units_per_pack=None)
        grams = self.client.post("/api/admin/inventory/opening-stock/", {
            "item_id": oil.pk, "location_id": self.store.pk, "quantity": "1500", "unit": "ml",
            "idempotency_key": "b1-oil-open",
        }, format="json")
        self.assertEqual(grams.status_code, 201, grams.content)
        self.assertEqual(StockBalance.objects.get(item=oil, location=self.store).quantity_on_hand, Decimal("1.5000"))

    def test_an_impossible_conversion_is_refused_instead_of_passed_through(self):
        rice = self.item(sku="B1-RICE2", name="Rice", base_unit="KG", category="Kitchen", units_per_pack=None)
        # Kilograms are not litres: a silent identity conversion here is how
        # "5 L of oil" becomes "5 kg of rice".
        litres = self.client.post("/api/admin/inventory/opening-stock/", {
            "item_id": rice.pk, "location_id": self.store.pk, "quantity": "5", "unit": "litres",
            "idempotency_key": "b1-rice-litres",
        }, format="json")
        self.assertEqual(litres.status_code, 400, litres.content)
        self.assertIn("unit", litres.json()["errors"])
        self.assertFalse(StockBalance.objects.filter(item=rice).exists())

    def test_a_conversion_factor_that_contradicts_the_exact_ratio_is_refused(self):
        rice = self.item(sku="B1-RICE3", name="Rice", base_unit="KG", category="Kitchen", units_per_pack=None)
        response = self.client.post("/api/admin/inventory/opening-stock/", {
            "item_id": rice.pk, "location_id": self.store.pk, "quantity": "2", "unit": "g",
            "conversion_factor": "500", "idempotency_key": "b1-rice-factor",
        }, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("conversion_factor", response.json()["errors"])

    def test_a_factor_that_contradicts_the_items_own_pack_size_is_refused(self):
        crate = self.item(sku="B1-CRATE2", name="Water crate", base_unit="PIECE", units_per_pack="24")
        response = self.client.post("/api/admin/inventory/opening-stock/", {
            "item_id": crate.pk, "location_id": self.store.pk, "quantity": "2", "unit": "CRATE",
            "conversion_factor": "12", "idempotency_key": "b1-crate-factor",
        }, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(StockMovement.objects.filter(item=crate).exists())

    def test_a_container_opening_unit_needs_its_real_size(self):
        crate = self.item(sku="B1-CRATE3", name="Water crate", base_unit="PIECE", units_per_pack="24")
        response = self.client.post("/api/admin/inventory/opening-stock/", {
            "item_id": crate.pk, "location_id": self.store.pk, "quantity": "2", "unit": "CRATE",
            "idempotency_key": "b1-crate-nofactor",
        }, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("unit", response.json()["errors"])

    def test_a_failed_opening_quantity_leaves_no_half_created_item(self):
        response = self.client.post(ITEMS, {
            "sku": "B1-ATOMIC", "name": "Atomic item", "base_unit": "PIECE", "standard_cost": "10.00",
            "opening_quantity": "1", "opening_location_id": self.store.pk, "opening_unit": "CARTON",
        }, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(StockItem.objects.filter(sku="B1-ATOMIC").exists(),
                         "a rejected opening quantity must not leave a catalog row behind")
        # And the retry is not blocked by a "duplicate" SKU the operator never meant to create.
        retry = self.client.post(ITEMS, {
            "sku": "B1-ATOMIC", "name": "Atomic item", "base_unit": "PIECE", "standard_cost": "10.00",
        }, format="json")
        self.assertEqual(retry.status_code, 201, retry.content)

    def test_units_show_human_labels_next_to_the_stored_token(self):
        item = self.item(sku="B1-LABEL", name="Labelled", base_unit="KG", category="Kitchen", units_per_pack=None)
        body = self.client.get(ITEMS, {"search": "B1-LABEL"}).json()["data"][0]
        self.assertEqual(body["base_unit"], "KG")
        self.assertEqual(body["base_unit_label"], "Kilograms (kg)")
        self.assertFalse(body["pack_conversion_available"])

    def test_the_base_unit_cannot_be_switched_once_stock_history_exists(self):
        item = self.item(sku="B1-HIST", name="History item", base_unit="PIECE", standard_cost="10.00")
        self.assertEqual(self.client.post("/api/admin/inventory/opening-stock/", {
            "item_id": item.pk, "location_id": self.store.pk, "quantity": "5", "idempotency_key": "b1-hist-open",
        }, format="json").status_code, 201)
        blocked = self.client.patch(f"{ITEMS}{item.pk}/", {"base_unit": "KG"}, format="json")
        self.assertEqual(blocked.status_code, 400, blocked.content)
        self.assertIn("base_unit", blocked.json()["errors"])
        item.refresh_from_db()
        self.assertEqual(item.base_unit, "PIECE")
        # The historical movement is untouched by the refusal.
        self.assertEqual(StockMovement.objects.get(item=item).quantity_delta, Decimal("5.0000"))

    def test_a_quantity_that_cannot_fit_the_ledger_is_refused(self):
        item = self.item(sku="B1-HUGE", name="Huge", base_unit="PIECE", units_per_pack="24", standard_cost="1.00")
        response = self.client.post("/api/admin/inventory/opening-stock/", {
            "item_id": item.pk, "location_id": self.store.pk, "quantity": "1000000000", "unit": "CRATE",
            "conversion_factor": "24", "idempotency_key": "b1-huge-open",
        }, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(StockMovement.objects.filter(item=item).exists())


class CrateIssueTests(Batch01Base):
    """Task 1 — crates must stay consistent when stock is ISSUED, not just received."""

    def setUp(self):
        super().setUp()
        self.crate = self.item(sku="B1-ISSUE-CRATE", name="Crate of beer", base_unit="PIECE", units_per_pack="24")
        self.assertEqual(self.client.post("/api/admin/inventory/opening-stock/", {
            "item_id": self.crate.pk, "location_id": self.store.pk, "quantity": "5", "unit": "CRATE",
            "conversion_factor": "24", "idempotency_key": "b1-issue-open",
        }, format="json").status_code, 201)

    def test_issuing_two_crates_deducts_exactly_forty_eight_pieces(self):
        response = self.client.post("/api/admin/inventory/issues/", {
            "item_id": self.crate.pk, "location_id": self.store.pk, "quantity": "2", "unit": "CRATE",
            "type": "ISSUE", "department": "RESTAURANT", "recipient": "Bar keeper",
            "idempotency_key": "b1-issue-crate",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        movement = StockMovement.objects.get(source_key__endswith="", type=StockMovement.Type.ISSUE)
        self.assertEqual(movement.quantity_delta, Decimal("-48.0000"))
        self.assertEqual(movement.item.base_unit, "PIECE")
        self.assertEqual(
            StockBalance.objects.get(item=self.crate, location=self.store).quantity_on_hand, Decimal("72.0000")
        )

    def test_issuing_without_a_declared_pack_size_is_refused(self):
        loose = self.item(sku="B1-ISSUE-LOOSE", name="Loose bottles", base_unit="PIECE", units_per_pack=None)
        self.assertEqual(self.client.post("/api/admin/inventory/opening-stock/", {
            "item_id": loose.pk, "location_id": self.store.pk, "quantity": "10", "idempotency_key": "b1-loose-open",
        }, format="json").status_code, 201)
        response = self.client.post("/api/admin/inventory/issues/", {
            "item_id": loose.pk, "location_id": self.store.pk, "quantity": "1", "unit": "CRATE",
            "type": "ISSUE", "department": "RESTAURANT", "idempotency_key": "b1-issue-loose",
        }, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(StockMovement.objects.filter(item=loose, type=StockMovement.Type.ISSUE).exists())

    def test_a_base_unit_issue_keeps_the_existing_contract(self):
        response = self.client.post("/api/admin/inventory/issues/", {
            "item_id": self.crate.pk, "location_id": self.store.pk, "quantity": "24",
            "type": "ISSUE", "department": "RESTAURANT", "idempotency_key": "b1-issue-plain",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(
            StockBalance.objects.get(item=self.crate, location=self.store).quantity_on_hand, Decimal("96.0000")
        )
        ledger = self.client.get("/api/admin/inventory/movements/", {"item_id": self.crate.pk, "type": "ISSUE"}).json()["data"]
        self.assertEqual(ledger[0]["base_unit_label"], "Pieces")


class StockAtHandAdjustmentTests(Batch01Base):
    """Task 2 — administrator-only correction of stock at hand."""

    def setUp(self):
        super().setUp()
        self.stock_item = self.item(sku="B1-ADJ", name="Adjustable", base_unit="PIECE", standard_cost="100.00")
        self.assertEqual(self.client.post("/api/admin/inventory/opening-stock/", {
            "item_id": self.stock_item.pk, "location_id": self.store.pk, "quantity": "10",
            "idempotency_key": "b1-adj-open",
        }, format="json").status_code, 201)
        self.balance = StockBalance.objects.get(item=self.stock_item, location=self.store)

    def _adjust_url(self):
        return f"{BALANCES}{self.balance.pk}/adjust/"

    def test_a_storekeeper_without_the_adjustment_capability_is_forbidden_on_the_api(self):
        self.auth(self.clerk)
        response = self.client.post(self._adjust_url(), {"quantity": "7", "reason": "Broken bottles"}, format="json")
        self.assertEqual(response.status_code, 403, response.content)
        self.balance.refresh_from_db()
        self.assertEqual(self.balance.quantity_on_hand, Decimal("10.0000"))
        self.assertFalse(StockBalanceAdjustment.objects.exists())

    def test_an_unauthenticated_request_is_refused(self):
        self.unauth()
        response = self.client.post(self._adjust_url(), {"quantity": "7", "reason": "Broken bottles"}, format="json")
        self.assertIn(response.status_code, (401, 403), response.content)
        self.assertEqual(StockBalanceAdjustment.objects.count(), 0)

    def test_an_administrator_correction_is_recorded_with_actor_quantities_and_reason(self):
        self.auth(self.admin)
        response = self.client.post(self._adjust_url(), {
            "quantity": "7.5", "reason": "Physical count: three bottles broken", "notes": "Week 44 count",
            "idempotency_key": "b1-adj-1",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        adjustment = StockBalanceAdjustment.objects.get()
        self.assertEqual(adjustment.actor, self.admin)
        self.assertEqual(adjustment.previous_quantity, Decimal("10.0000"))
        self.assertEqual(adjustment.new_quantity, Decimal("7.5000"))
        self.assertEqual(adjustment.quantity_delta, Decimal("-2.5000"))
        self.assertEqual(adjustment.reason, "Physical count: three bottles broken")
        self.assertEqual(adjustment.base_unit, "PIECE")
        self.assertIsNotNone(adjustment.created_at)
        self.assertEqual(adjustment.movement.type, StockMovement.Type.ADJUSTMENT)
        self.assertEqual(adjustment.movement.quantity_delta, Decimal("-2.5000"))
        self.balance.refresh_from_db()
        self.assertEqual(self.balance.quantity_on_hand, Decimal("7.5000"))
        # The audit trail names the administrator and both quantities.
        entry = AuditLog.objects.filter(action="STOCK_BALANCE_ADJUSTED").first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.actor, self.admin)
        self.assertEqual(entry.changes["previous_quantity"], "10.0000")
        self.assertEqual(entry.changes["new_quantity"], "7.5000")
        self.assertEqual(entry.metadata["reason"], "Physical count: three bottles broken")

    def test_the_correction_is_an_append_only_movement_and_history_keeps_its_values(self):
        opening_quantity = StockMovement.objects.get(item=self.stock_item).quantity_delta
        self.auth(self.admin)
        self.assertEqual(self.client.post(self._adjust_url(), {
            "quantity": "4", "reason": "Stock-take shortfall", "idempotency_key": "b1-adj-2",
        }, format="json").status_code, 201)
        movements = StockMovement.objects.filter(item=self.stock_item).order_by("pk")
        self.assertEqual(movements.count(), 2)
        self.assertEqual(movements[0].type, StockMovement.Type.OPENING)
        self.assertEqual(movements[0].quantity_delta, opening_quantity, "history is never recalculated")
        self.assertEqual(movements[1].type, StockMovement.Type.ADJUSTMENT)
        with self.assertRaises(Exception):
            adjustment = StockBalanceAdjustment.objects.get()
            adjustment.reason = "changed my mind"
            adjustment.save()

    def test_the_reason_and_a_real_change_are_required(self):
        self.auth(self.admin)
        self.assertEqual(self.client.post(self._adjust_url(), {"quantity": "7"}, format="json").status_code, 400)
        self.assertEqual(self.client.post(self._adjust_url(), {"quantity": "7", "reason": "x"}, format="json").status_code, 400)
        same = self.client.post(self._adjust_url(), {"quantity": "10", "reason": "No real change"}, format="json")
        self.assertEqual(same.status_code, 400, same.content)
        negative = self.client.post(self._adjust_url(), {"quantity": "-1", "reason": "Negative test"}, format="json")
        self.assertEqual(negative.status_code, 400, negative.content)
        self.assertFalse(StockBalanceAdjustment.objects.exists())
        self.assertEqual(StockMovement.objects.filter(item=self.stock_item).count(), 1)

    def test_a_replayed_request_records_the_correction_once(self):
        self.auth(self.admin)
        payload = {"quantity": "6", "reason": "Recount after breakage", "idempotency_key": "b1-adj-replay"}
        self.assertEqual(self.client.post(self._adjust_url(), payload, format="json").status_code, 201)
        replay = self.client.post(self._adjust_url(), payload, format="json")
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(StockBalanceAdjustment.objects.count(), 1)
        self.assertEqual(StockMovement.objects.filter(item=self.stock_item, type=StockMovement.Type.ADJUSTMENT).count(), 1)
        self.balance.refresh_from_db()
        self.assertEqual(self.balance.quantity_on_hand, Decimal("6.0000"))

    def test_the_correction_history_is_listable_and_scoped(self):
        self.auth(self.admin)
        self.client.post(self._adjust_url(), {"quantity": "9", "reason": "Recount", "idempotency_key": "b1-adj-3"})
        listing = self.client.get(ADJUSTMENTS, {"item_id": self.stock_item.pk})
        self.assertEqual(listing.status_code, 200, listing.content)
        self.assertEqual(listing.json()["data"][0]["reason"], "Recount")
        self.assertEqual(listing.json()["data"][0]["actor_email"], self.admin.email)
        self.assertEqual(listing.json()["data"][0]["base_unit_label"], "Pieces")
        self.assertEqual(self.client.get(ADJUSTMENTS, {"item_id": 999999}).json()["data"], [])

    def test_stock_valuation_follows_the_corrected_quantity(self):
        self.auth(self.admin)
        self.client.post(self._adjust_url(), {"quantity": "8", "reason": "Count correction", "idempotency_key": "b1-adj-4"})
        projection = self.client.get("/api/admin/inventory/profitability/", {"search": "B1-ADJ"})
        self.assertEqual(projection.status_code, 200, projection.content)
        row = projection.json()["data"][0]
        self.assertEqual(row["current_stock"], "8.0000")
        self.assertEqual(row["stock_value"], "800.00")


class SearchableSkuTests(Batch01Base):
    """Task 3 — searchable SKU, no duplicate items, safe replenishment."""

    def test_a_duplicate_sku_is_refused_case_insensitively(self):
        self.assertEqual(self.create_item(sku="B1-DUP", name="Original").status_code, 201)
        duplicate = self.create_item(sku="b1-dup", name="Second record")
        self.assertEqual(duplicate.status_code, 400, duplicate.content)
        self.assertIn("already belongs to", str(duplicate.json()["errors"]))
        self.assertEqual(StockItem.objects.filter(sku__iexact="B1-DUP").count(), 1)
        self.assertEqual(StockItem.objects.count(), 1)

    def test_the_sku_search_finds_existing_items_by_sku_name_category_and_barcode(self):
        self.assertEqual(self.create_item(
            sku="B1-SEARCH", name="Sparkling water", category="Bar drinks", barcode="5901234123457",
        ).status_code, 201)
        self.assertEqual(self.create_item(sku="B1-OTHER", name="Rice", category="Kitchen").status_code, 201)
        for term in ("B1-SEARCH", "sparkling", "bar drinks", "5901234123457"):
            with self.subTest(term=term):
                data = self.client.get(ITEMS, {"search": term}).json()["data"]
                self.assertEqual([row["sku"] for row in data], ["B1-SEARCH"], term)

    def test_an_exact_sku_match_is_returned_first(self):
        self.assertEqual(self.create_item(sku="B1-EXACT", name="Exact item").status_code, 201)
        self.assertEqual(self.create_item(sku="B1-EXACT-DUP-NAME", name="B1-EXACT sibling").status_code, 201)
        data = self.client.get(ITEMS, {"search": "B1-EXACT"}).json()["data"]
        self.assertEqual(data[0]["sku"], "B1-EXACT")

    def test_replenishing_an_existing_sku_never_rewrites_its_master_data(self):
        item = self.item(sku="B1-REPL", name="Replenished water", category="Bar drinks", barcode="5900000000017",
                         base_unit="PIECE", units_per_pack="24", standard_cost="50.00")
        response = self.client.post(f"{ITEMS}{item.pk}/replenish/", {
            "location_id": self.store.pk, "quantity": "2", "unit": "CRATE", "unit_cost": "1200.00",
            "supplier_reference": "INV-778", "notes": "Weekly delivery", "idempotency_key": "b1-repl-1",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()["data"]
        self.assertEqual(body["movement"]["type"], StockMovement.Type.RECEIPT)
        self.assertEqual(body["movement"]["quantity_delta"], "48.0000")
        self.assertEqual(body["movement"]["metadata"]["entered_unit"], "CRATE")
        self.assertEqual(body["balance"]["quantity_on_hand"], "48.0000")
        item.refresh_from_db()
        # Master data is exactly what it was: replenishment adds stock only.
        self.assertEqual(item.name, "Replenished water")
        self.assertEqual(item.category, "Bar drinks")
        self.assertEqual(item.base_unit, "PIECE")
        self.assertEqual(item.barcode, "5900000000017")
        self.assertEqual(item.units_per_pack, Decimal("24.00"))
        self.assertEqual(item.standard_cost, Decimal("50.00"))
        self.assertEqual(StockItem.objects.count(), 1)

    def test_replenishment_is_idempotent(self):
        item = self.item(sku="B1-REPL2", name="Replenished twice")
        payload = {"location_id": self.store.pk, "quantity": "5", "idempotency_key": "b1-repl-2"}
        self.assertEqual(self.client.post(f"{ITEMS}{item.pk}/replenish/", payload, format="json").status_code, 201)
        self.assertEqual(self.client.post(f"{ITEMS}{item.pk}/replenish/", payload, format="json").status_code, 200)
        self.assertEqual(StockMovement.objects.filter(item=item, type=StockMovement.Type.RECEIPT).count(), 1)
        self.assertEqual(StockBalance.objects.get(item=item).quantity_on_hand, Decimal("5.0000"))

    def test_replenishment_validates_quantities_and_units(self):
        item = self.item(sku="B1-REPL3", name="Validated", units_per_pack="24")
        for payload in (
            {"location_id": self.store.pk, "quantity": "0"},
            {"location_id": self.store.pk, "quantity": "-2"},
            {"location_id": 999999, "quantity": "1"},
            {"location_id": self.store.pk, "quantity": "1", "unit": "CRATE", "conversion_factor": "12"},
            {"location_id": self.store.pk, "quantity": "1", "unit": "LITRES"},
        ):
            with self.subTest(payload=payload):
                response = self.client.post(f"{ITEMS}{item.pk}/replenish/", payload, format="json")
                self.assertIn(response.status_code, (400, 404), response.content)
        self.assertFalse(StockMovement.objects.filter(item=item).exists())

    def test_replenishment_is_capability_gated(self):
        item = self.item(sku="B1-REPL4", name="Gated")
        self.auth(self.cashier)
        response = self.client.post(f"{ITEMS}{item.pk}/replenish/", {"location_id": self.store.pk, "quantity": "1"}, format="json")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(StockMovement.objects.filter(item=item).exists())


class StockAtHandSearchTests(Batch01Base):
    """Task 4 — search stock at hand without reloading the page."""

    def setUp(self):
        super().setUp()
        self.water = self.item(sku="B1-S-WATER", name="Sachet water", category="Bar drinks", units_per_pack=None)
        self.rice = self.item(sku="B1-S-RICE", name="Parboiled rice", category="Kitchen", units_per_pack=None)
        self.rice.barcode = "5901234000018"
        self.rice.save(update_fields=["barcode"])
        for item, quantity in ((self.water, "10"), (self.rice, "2")):
            self.assertEqual(self.client.post("/api/admin/inventory/opening-stock/", {
                "item_id": item.pk, "location_id": self.store.pk, "quantity": quantity,
                "idempotency_key": f"b1-search-{item.pk}-{self.store.pk}",
            }, format="json").status_code, 201)
        # A genuinely empty store: a zero balance row is a real projection row
        # (the bar has never held rice), so the search must still find it.
        StockBalance.objects.create(item=self.rice, location=self.bar, quantity_on_hand=0)

    def test_search_matches_name_sku_category_barcode_and_location(self):
        for term, expected in (
            ("sachet", {"B1-S-WATER"}), ("B1-S-RICE", {"B1-S-RICE"}),
            ("kitchen", {"B1-S-RICE"}), ("5901234000018", {"B1-S-RICE"}),
            ("B1-BAR", {"B1-S-RICE"}),
        ):
            with self.subTest(term=term):
                data = self.client.get(BALANCES, {"search": term}).json()["data"]
                self.assertEqual({row["item_sku"] for row in data}, expected)

    def test_search_preserves_filters_and_paginates(self):
        page_one = self.client.get(BALANCES, {"search": "B1-S-", "page_size": 2}).json()
        self.assertEqual(len(page_one["data"]), 2)
        self.assertEqual(page_one["pagination"]["count"], 3)
        self.assertEqual(page_one["pagination"]["total_pages"], 2)
        page_two = self.client.get(BALANCES, {"search": "B1-S-", "page_size": 2, "page": 2}).json()
        self.assertEqual(len(page_two["data"]), 1)
        # The reorder filter still applies to a search.
        needs_reorder = self.client.get(BALANCES, {"search": "B1-S-", "reorder": "true"}).json()["data"]
        self.assertEqual({row["item_sku"] for row in needs_reorder}, {"B1-S-RICE"})
        by_type = self.client.get(BALANCES, {"search": "B1-S-", "item_type": "CONSUMABLE"}).json()["data"]
        self.assertTrue(by_type)
        empty = self.client.get(BALANCES, {"search": "nothing-matches-this"}).json()
        self.assertEqual(empty["data"], [])
        self.assertEqual(empty["pagination"]["count"], 0)

    def test_balances_expose_the_identifiers_the_search_uses(self):
        row = self.client.get(BALANCES, {"search": "B1-S-RICE", "location_id": self.store.pk}).json()["data"][0]
        self.assertEqual(row["item_category"], "Kitchen")
        self.assertEqual(row["item_barcode"], "5901234000018")
        self.assertEqual(row["base_unit_label"], "Pieces")
        self.assertEqual(row["location_name"], "Batch 01 main store")

    def test_stock_at_hand_requires_the_inventory_capability(self):
        self.auth(self.cashier)
        self.assertEqual(self.client.get(BALANCES, {"search": "B1-S-"}).status_code, 403)


class ReorderingTests(Batch01Base):
    """Task 5 — reorder dialog, exact totals, approval workflow, no stock change."""

    def setUp(self):
        super().setUp()
        self.stock_item = self.item(sku="B1-ORD", name="Ordered water", category="Bar drinks",
                                    base_unit="PIECE", units_per_pack="24", standard_cost="50.00")
        supplier = self.client.post("/api/admin/inventory/suppliers/", {"name": "Batch 01 Supplier"}, format="json")
        self.assertEqual(supplier.status_code, 201, supplier.content)
        self.supplier_id = supplier.json()["data"]["id"]
        self.stock_item.preferred_supplier_id = self.supplier_id
        self.stock_item.save(update_fields=["preferred_supplier"])
        self.assertEqual(self.client.post("/api/admin/inventory/opening-stock/", {
            "item_id": self.stock_item.pk, "location_id": self.store.pk, "quantity": "12",
            "idempotency_key": "b1-ord-open",
        }, format="json").status_code, 201)

    def test_the_reorder_context_supplies_the_item_details_staff_must_not_retype(self):
        self.auth(self.procurement)
        response = self.client.get(f"/api/admin/inventory/procurement/items/{self.stock_item.pk}/reorder-context/")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["item"]["sku"], "B1-ORD")
        self.assertEqual(data["item"]["name"], "Ordered water")
        self.assertEqual(data["item"]["category"], "Bar drinks")
        self.assertEqual(data["item"]["base_unit"], "PIECE")
        self.assertEqual(data["item"]["base_unit_label"], "Pieces")
        self.assertEqual(data["item"]["preferred_supplier"], self.supplier_id)
        self.assertEqual(data["item"]["standard_cost"], "50.00")
        self.assertEqual(data["total_on_hand"], "12.0000")
        self.assertEqual([row["location_code"] for row in data["balances"]], ["B1-MAIN"])
        self.assertEqual(data["default_location_id"], self.store.pk)
        self.assertIsNone(data["last_receipt"])
        self.assertEqual(data["open_purchase_order_quantity"], "0.0000")

    def test_the_reorder_context_reports_the_last_actual_purchase_cost(self):
        # A real approved-and-received order establishes the last purchase cost.
        order = self.client.post("/api/admin/inventory/purchase-orders/", {
            "supplier_id": self.supplier_id, "delivery_location_id": self.store.pk,
            "idempotency_key": "b1-ctx-po",
            "lines": [{"item_id": self.stock_item.pk, "quantity_ordered": "24", "unit_cost": "55"}],
        }, format="json")
        reference = order.json()["data"]["reference"]
        self.client.post(f"/api/admin/inventory/purchase-orders/{reference}/submit/", {}, format="json")
        self.auth(self.manager)
        self.client.post(f"/api/admin/inventory/purchase-orders/{reference}/approve/", {}, format="json")
        self.auth(self.clerk)
        self.client.post(f"/api/admin/inventory/purchase-orders/{reference}/ordered/", {}, format="json")
        line_id = self.client.get(f"/api/admin/inventory/purchase-orders/{reference}/").json()["data"]["lines"][0]["id"]
        self.client.post(f"/api/admin/inventory/purchase-orders/{reference}/receipts/", {
            "idempotency_key": "b1-ctx-grn",
            "lines": [{"purchase_order_line_id": line_id, "quantity_received": "24"}],
        }, format="json")
        context = self.client.get(f"/api/admin/inventory/procurement/items/{self.stock_item.pk}/reorder-context/").json()["data"]
        self.assertEqual(context["last_receipt"]["unit_cost"], "55.00")
        self.assertEqual(context["last_receipt"]["supplier_name"], "Batch 01 Supplier")
        self.assertEqual(context["total_on_hand"], "36.0000")

    def test_creating_a_reorder_draft_does_not_increase_stock(self):
        before = StockBalance.objects.get(item=self.stock_item, location=self.store).quantity_on_hand
        draft = self.client.post("/api/admin/inventory/purchase-orders/", {
            "supplier_id": self.supplier_id, "delivery_location_id": self.store.pk,
            "expected_on": "2026-11-15", "notes": "Reorder from the stock-at-hand screen",
            "idempotency_key": "b1-reorder-1",
            "lines": [{"item_id": self.stock_item.pk, "quantity_ordered": "48", "unit_cost": "52.50"}],
        }, format="json")
        self.assertEqual(draft.status_code, 201, draft.content)
        data = draft.json()["data"]
        self.assertEqual(data["status"], PurchaseOrder.Status.DRAFT)
        self.assertEqual(data["total_amount"], "2520.00")
        StockBalance.objects.get(item=self.stock_item, location=self.store)
        self.assertEqual(
            StockBalance.objects.get(item=self.stock_item, location=self.store).quantity_on_hand, before,
            "a reorder request must never increase stock",
        )
        self.assertEqual(StockMovement.objects.filter(item=self.stock_item, type=StockMovement.Type.RECEIPT).count(), 0)
        # The approval workflow is unchanged: the requester still cannot approve.
        self.assertEqual(self.client.post(
            f"/api/admin/inventory/purchase-orders/{data['reference']}/approve/", {}, format="json"
        ).status_code, 403)

    def test_stock_only_increases_after_an_authorised_receipt(self):
        draft = self.client.post("/api/admin/inventory/purchase-orders/", {
            "supplier_id": self.supplier_id, "delivery_location_id": self.store.pk,
            "idempotency_key": "b1-reorder-2",
            "lines": [{"item_id": self.stock_item.pk, "quantity_ordered": "24", "unit_cost": "52"}],
        }, format="json").json()["data"]
        reference = draft["reference"]
        self.assertEqual(self.client.post(f"/api/admin/inventory/purchase-orders/{reference}/submit/", {}, format="json").status_code, 200)
        self.auth(self.manager)
        self.assertEqual(self.client.post(f"/api/admin/inventory/purchase-orders/{reference}/approve/", {}, format="json").status_code, 200)
        self.auth(self.clerk)
        self.assertEqual(self.client.post(f"/api/admin/inventory/purchase-orders/{reference}/ordered/", {}, format="json").status_code, 200)
        self.assertEqual(StockBalance.objects.get(item=self.stock_item, location=self.store).quantity_on_hand, Decimal("12.0000"))
        line_id = self.client.get(f"/api/admin/inventory/purchase-orders/{reference}/").json()["data"]["lines"][0]["id"]
        receipt = self.client.post(f"/api/admin/inventory/purchase-orders/{reference}/receipts/", {
            "idempotency_key": "b1-reorder-grn",
            "lines": [{"purchase_order_line_id": line_id, "quantity_received": "24", "unit_cost": "52"}],
        }, format="json")
        self.assertEqual(receipt.status_code, 201, receipt.content)
        self.assertEqual(StockBalance.objects.get(item=self.stock_item, location=self.store).quantity_on_hand, Decimal("36.0000"))
        order = PurchaseOrder.objects.get(reference=reference)
        self.assertEqual(order.status, PurchaseOrder.Status.RECEIVED)
        self.assertEqual(PurchaseOrderLine.objects.get(purchase_order=order).quantity_received, Decimal("24.0000"))

    def test_the_reorder_context_is_procurement_scoped(self):
        self.auth(self.cashier)
        self.assertEqual(
            self.client.get(f"/api/admin/inventory/procurement/items/{self.stock_item.pk}/reorder-context/").status_code, 403
        )
        self.unauth()
        self.assertIn(
            self.client.get(f"/api/admin/inventory/procurement/items/{self.stock_item.pk}/reorder-context/").status_code,
            (401, 403),
        )

    def test_a_missing_item_is_a_clean_404(self):
        self.assertEqual(
            self.client.get("/api/admin/inventory/procurement/items/999999/reorder-context/").status_code, 404
        )
