# tests/test_inventory_item_management.py
"""Item classification, explicit direct sales, honest units, and store issues.

This module proves the inventory brief's organisational rules, item by item:

=======================  =====================================================
directly sellable         deducted by an EXPLICIT direct-sale configuration
products                  (POS menu item + store + units per menu unit) or by a
                          real recipe — never because a SKU or a name happens
                          to match a menu item;
recipe ingredients        deducted through their own BOM line, in the unit the
                          kitchen wrote down, converted exactly (250 g of rice
                          is 0.2500 KG, never a whole bag);
operational consumables   leave through an internal issue to a department,
                          recorded on the immutable ledger with department,
                          recipient, issuing user, reason and date;
reusable assets           the same issue path, but custody must be named and
                          the issue ceiling is enforced when they come back.
========================================================================

Coverage map against the ten required areas (plus the configuration rules):

1. direct POS bar deduction .................. DirectSaleDeductionTests
2. recipe-only ingredient deduction .......... RecipeUnitDeductionTests
3. unit conversion accuracy .................. UnitConversionTests
4. internal issue reduces source + auditable . InternalIssueTests
5. internal issues create no POS revenue ..... InternalIssueTests
6. cancelled/failed/retried never double-shot  IdempotencyTests
7. concurrency cannot go negative ............ ConcurrencyTests
8. transfers preserve the total ............... TransferIntegrityTests
9. weighted-average cost/valuation intact .... ValuationIntegrityTests
10. existing recipes/history/workflows intact  ExistingBehaviourTests
    (plus) no automatic recipe creation ....... NoAutomaticRecipeTests
    (plus) the read-only configuration audit .. ExistingBehaviourTests
    (plus) the deployment backfill ............ ItemTypeBackfillTests
"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from importlib import import_module

from django.apps import apps as global_apps

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection, transaction
from io import StringIO

from apps.accounts.models import User
from apps.finance.models import Expense, FinancialLine, FinancialTransaction, FolioPosting
from apps.inventory.models import (
    StockBalance,
    StockDepartment,
    StockItem,
    StockLocation,
    StockMovement,
    StockRecipe,
)
from apps.inventory.services.inventory_service import (
    approve_purchase_order,
    convert_stock_quantity,
    create_purchase_order,
    create_stock_recipe,
    issue_stock,
    mark_purchase_order_ordered,
    process_pending_pos_consumption,
    receive_goods,
    record_opening_stock,
    record_stock_return,
    submit_purchase_order,
    transfer_stock,
)
from apps.inventory.services.valuation_service import product_profitability_projection
from apps.pos.models import MenuCategory, MenuItem, PosOrder, PosTender
from apps.pos.services.barcode_service import stock_summary_for_menu_item
from apps.pos.services.order_service import create_order, submit_order, transition_order

from .base import BaseAPITestCase
from .factories import make_staff
from .test_inventory_sales_depletion import SalesDepletionTestCase


class InventoryManagementTestCase(SalesDepletionTestCase):
    """The depletion fixture (supplier, stores, recipes) plus the four item types."""

    def setUp(self):
        super().setUp()
        self.kitchen = StockLocation.objects.create(code="ITM-KITCHEN", name="Kitchen store")
        self.laundry = StockLocation.objects.create(code="ITM-LAUNDRY", name="Laundry store")

        # recipe ingredient: a 5 kg bag of rice deducted in grams
        self.rice = StockItem.objects.create(
            sku="ITM-RICE", name="Parboiled rice", category="Foodstuff", base_unit="KG",
            item_type=StockItem.ItemType.RECIPE_INGREDIENT, standard_cost=Decimal("2200.00"),
        )
        self.rice_menu = MenuItem.objects.create(
            category=self.restaurant_category, name="Plate of rice", sku="ITM-MENU-RICE",
            base_price=Decimal("2500.00"),
        )
        create_stock_recipe(
            menu_item=self.rice_menu, location=self.store, tracking_mode="COMPONENTS",
            components=[{"item_id": self.rice.pk, "quantity_per_menu_unit": "250", "unit": "g"}],
            actor=self.clerk, idempotency_key="itm-rice-recipe",
        )

        # operational consumable: nothing about it appears on a POS menu
        self.detergent = StockItem.objects.create(
            sku="ITM-DETERGENT", name="Laundry detergent (1 kg)", category="Housekeeping",
            base_unit="PACK", item_type=StockItem.ItemType.CONSUMABLE, standard_cost=Decimal("1800.00"),
        )
        # reusable asset: leaves the store as custody, comes back
        self.towel = StockItem.objects.create(
            sku="ITM-TOWEL", name="Bath towel", category="Housekeeping", base_unit="PIECE",
            item_type=StockItem.ItemType.REUSABLE_ASSET, standard_cost=Decimal("3500.00"),
        )
        # directly sellable product sold by the crate (24 bottles per crate)
        self.crate_menu = MenuItem.objects.create(
            category=self.bar_category, name="Crate of table water (24)", sku="ITM-MENU-CRATE",
            base_price=Decimal("6000.00"),
        )

    # ------------------------------------------------------------------ helpers
    def balance(self, item, location):
        row = StockBalance.objects.filter(item=item, location=location).first()
        return Decimal(row.quantity_on_hand) if row else Decimal("0.0000")

    def total(self, item):
        return sum(
            (Decimal(row.quantity_on_hand) for row in StockBalance.objects.filter(item=item)),
            Decimal("0.0000"),
        )

    def stock_in(self, item, location, quantity, *, unit_cost="100.00", key="itm-opening"):
        """Opening balance (not a purchase — see ``receive_item`` for cost basis)."""
        return record_opening_stock(
            item=item, location=location, quantity=quantity, actor=self.clerk,
            source_key=f"{key}:{item.pk}:{location.pk}", unit_cost=unit_cost, notes="Test stock in",
        )

    def receive_item(self, item, location, quantity, *, unit_cost="100.00", key="itm-receipt"):
        """The real procurement path, so the receipt ledger carries the cost."""
        purchase_order, _ = create_purchase_order(
            supplier=self.supplier, delivery_location=location, requested_by=self.clerk,
            lines=[{"item_id": item.pk, "quantity_ordered": str(quantity), "unit_cost": str(unit_cost)}],
            idempotency_key=f"{key}-po",
        )
        submit_purchase_order(purchase_order=purchase_order, actor=self.clerk)
        approve_purchase_order(purchase_order=purchase_order, actor=self.manager)
        ordered = mark_purchase_order_ordered(purchase_order=purchase_order, actor=self.clerk)
        line = ordered.lines.get()
        return receive_goods(
            purchase_order=ordered, actor=self.clerk, source_key=f"{key}-grn",
            receipt_lines=[{
                "purchase_order_line_id": line.pk, "quantity_received": str(quantity), "unit_cost": str(unit_cost),
            }],
        )[0]

    def configure_direct_sale(self, item, *, menu_item, location, quantity="1.0000"):
        StockItem.objects.filter(pk=item.pk).update(
            item_type=StockItem.ItemType.SELLABLE_PRODUCT,
            direct_sale_menu_item=menu_item,
            direct_sale_location=location,
            direct_sale_quantity=Decimal(quantity),
        )
        item.refresh_from_db()
        return item


class NoAutomaticRecipeTests(InventoryManagementTestCase):
    """Requirement 5: a SKU/name match must never fabricate a recipe."""

    def test_a_matching_sku_creates_no_recipe_and_deducts_nothing(self):
        menu_item = MenuItem.objects.create(
            category=self.bar_category, name="Malt drink 33cl", sku="ITM-MALT", base_price=Decimal("900.00"),
        )
        # Same SKU, no recipe, no direct-sale configuration.
        malt = StockItem.objects.create(sku="ITM-MALT", name="Malt drink 33cl", base_unit="BOTTLE")
        self.stock_in(malt, self.bar, 12)
        recipes_before = StockRecipe.objects.count()
        movements_before = StockMovement.objects.count()

        order = self.sell(mode=PosOrder.Mode.BAR, menu_item=menu_item, quantity=2, table_number="T-9")

        self.assertEqual(StockRecipe.objects.count(), recipes_before, "no recipe may be invented from a SKU match")
        self.assertEqual(
            StockMovement.objects.filter(item=malt).count(),
            1,
            "the only movement for this item must be its opening stock",
        )
        self.assertEqual(self.balance(malt, self.bar), Decimal("12.0000"))

        request = PosOrder.objects.get(pk=order.pk).stock_consumption_request
        # Nothing deducted *and* nothing configured is a gap for a human, so the
        # hand-off is parked for review — never a silent "not tracked".
        self.assertEqual(request.status, "FAILED")
        self.assertIn("ITM-MALT", request.resolution_note)
        self.assertIn("No stock deduction is configured", request.resolution_note)
        self.assertIn("no recipe was created", request.resolution_note)
        self.assertEqual(request.resolution_payload.get("uncontrolled"), ["ITM-MALT"])
        self.assertEqual(request.resolution_payload.get("movement_count"), 0)
        self.assertEqual(movements_before + 0, StockMovement.objects.count())

        # The order itself is untouched: the guest was charged and the sale is
        # delivered. Configuring the product afterwards resolves the parked
        # hand-off through the existing catch-up sweep, and deducts once.
        self.configure_direct_sale(malt, menu_item=menu_item, location=self.bar)
        self.assertEqual(process_pending_pos_consumption(), {"examined": 1, "posted": 1, "still_open": 0})
        request.refresh_from_db()
        self.assertEqual(request.status, "PROCESSED")
        self.assertEqual(self.balance(malt, self.bar), Decimal("10.0000"))
        self.assertEqual(
            StockMovement.objects.filter(item=malt, type=StockMovement.Type.POS_CONSUMPTION).count(), 1,
        )
        # Re-running the sweep after the fact deducts nothing a second time.
        self.assertEqual(process_pending_pos_consumption(), {"examined": 0, "posted": 0, "still_open": 0})
        self.assertEqual(self.balance(malt, self.bar), Decimal("10.0000"))
        self.assertEqual(StockRecipe.objects.count(), recipes_before, "still no invented recipe")

    def test_an_invalid_recipe_still_blocks_the_whole_order(self):
        """No guessing, but a broken configuration is a hard stop with evidence.

        Since order entry validates stock, the hard stop now happens at
        CREATION with a clear product-level refusal. The delivery-time
        evidence path is still covered: a recipe broken AFTER a valid draft
        was created parks the hand-off instead of deducting anything.
        """
        from apps.core.exceptions import PosStockConflictError

        menu_item = MenuItem.objects.create(
            category=self.bar_category, name="Cocktail special", sku="ITM-COCKTAIL", base_price=Decimal("3000.00"),
        )
        recipe, _ = create_stock_recipe(
            menu_item=menu_item, location=self.bar, tracking_mode="COMPONENTS",
            components=[{"item_id": self.bottle.pk, "quantity_per_menu_unit": "1"}],
            actor=self.clerk, idempotency_key="itm-cocktail-recipe",
        )
        # The BOM line is removed out-of-band: the configuration is now invalid.
        recipe.lines.all().delete()
        self.stock_in(self.bottle, self.bar, 5)
        before = self.balance(self.bottle, self.bar)

        # Order entry refuses the sale outright: no order, no lines, no deduction.
        with self.assertRaises(PosStockConflictError) as raised:
            self.sell(mode=PosOrder.Mode.BAR, menu_item=menu_item, table_number="T-10")
        self.assertIn("Stock location unavailable for Cocktail special", str(raised.exception))
        self.assertIn("has no components", str(raised.exception))
        self.assertFalse(PosOrder.objects.filter(lines__item_sku="ITM-COCKTAIL").exists())
        self.assertEqual(self.balance(self.bottle, self.bar), before, "a broken recipe must not deduct anything")

        # A recipe broken after the draft was submitted still parks at
        # delivery, with evidence, and never deducts anything.
        recipe.lines.create(item=self.bottle, quantity_per_menu_unit=Decimal("1"))
        order = create_order(
            mode=PosOrder.Mode.BAR, actor=self.cashier, table_number="T-10b",
            lines=[{"menu_item_id": menu_item.pk, "quantity": 1}],
        )[0]
        submit_order(order=order, actor=self.cashier)
        recipe.lines.all().delete()
        transition_order(order=order, target_status=PosOrder.Status.PREPARING, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.READY, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.cashier)

        request = PosOrder.objects.get(pk=order.pk).stock_consumption_request
        self.assertEqual(request.status, "FAILED")
        self.assertIn("invalid recipe", request.resolution_note)
        self.assertEqual(self.balance(self.bottle, self.bar), before, "a broken recipe must not deduct anything")


class DirectSaleDeductionTests(InventoryManagementTestCase):
    """Requirement 3: the exact quantity sold, from the configured store, once."""

    def test_a_configured_crate_deducts_the_twenty_four_bottles_it_contains(self):
        self.configure_direct_sale(
            self.bottle, menu_item=self.crate_menu, location=self.bar, quantity="24.0000"
        )
        self.stock_in(self.bottle, self.bar, 30)
        self.assertEqual(self.balance(self.bottle, self.bar), Decimal("30.0000"))

        self.sell(mode=PosOrder.Mode.BAR, menu_item=self.crate_menu, quantity=1, table_number="T-1")

        self.assertEqual(self.balance(self.bottle, self.bar), Decimal("6.0000"))
        movement = StockMovement.objects.filter(item=self.bottle, type=StockMovement.Type.POS_CONSUMPTION).first()
        self.assertIsNotNone(movement)
        self.assertEqual(movement.quantity_delta, Decimal("-24.0000"))
        resolved = movement.metadata.get("resolved_recipes") or []
        self.assertTrue(any(row.get("direct_sale") for row in resolved), "the evidence must name the direct sale")
        self.assertFalse(StockRecipe.objects.filter(menu_item=self.crate_menu).exists())

    def test_the_sale_deducts_only_from_the_configured_store(self):
        self.configure_direct_sale(self.bottle, menu_item=self.crate_menu, location=self.kitchen)
        self.stock_in(self.bottle, self.kitchen, 5)
        self.stock_in(self.bottle, self.bar, 7)

        self.sell(mode=PosOrder.Mode.BAR, menu_item=self.crate_menu, quantity=2, table_number="T-2")

        self.assertEqual(self.balance(self.bottle, self.kitchen), Decimal("3.0000"))
        self.assertEqual(self.balance(self.bottle, self.bar), Decimal("7.0000"), "the bar store was not configured")
        self.assertEqual(self.total(self.bottle), Decimal("10.0000"))

    def test_a_configured_product_reports_its_saleable_stock_to_the_pos(self):
        self.configure_direct_sale(self.bottle, menu_item=self.crate_menu, location=self.bar, quantity="24.0000")
        self.stock_in(self.bottle, self.bar, 50)

        summary = stock_summary_for_menu_item(self.crate_menu)
        self.assertTrue(summary["tracked"])
        self.assertEqual(summary["location_code"], self.bar.code)
        self.assertEqual(summary["saleable_quantity"], "2", "50 bottles is two whole crates")

    def test_a_configuration_must_be_complete_and_unique_per_menu_item(self):
        self.auth(self.clerk)
        response = self.client.patch(
            f"/api/admin/inventory/items/{self.bottle.pk}/",
            {"item_type": "SELLABLE_PRODUCT", "direct_sale_menu_item": self.crate_menu.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("direct_sale_location", (response.json().get("errors") or {}))

        other = StockItem.objects.create(sku="ITM-WATER-2", name="Table water (second record)", base_unit="BOTTLE")
        self.configure_direct_sale(self.bottle, menu_item=self.crate_menu, location=self.bar)
        response = self.client.patch(
            f"/api/admin/inventory/items/{other.pk}/",
            {"direct_sale_menu_item": self.crate_menu.pk, "direct_sale_location": self.bar.pk,
             "direct_sale_quantity": "1.0000"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("already deducts", str(response.json()))

        # Clearing the first claim releases the menu item for a correctly
        # configured one; nothing else about the first item is touched.
        response = self.client.patch(
            f"/api/admin/inventory/items/{self.bottle.pk}/", {"direct_sale_menu_item": None}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.json())
        self.bottle.refresh_from_db()
        self.assertFalse(self.bottle.is_direct_sale_configured)
        self.assertIsNone(self.bottle.direct_sale_location_id, "clearing the product clears the stale store too")

        response = self.client.patch(
            f"/api/admin/inventory/items/{other.pk}/",
            {"item_type": "SELLABLE_PRODUCT", "direct_sale_menu_item": self.crate_menu.pk,
             "direct_sale_location": self.laundry.pk, "direct_sale_quantity": "24.0000"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.json())
        other.refresh_from_db()
        self.assertTrue(other.is_direct_sale_configured)
        self.assertEqual(other.direct_sale_location_id, self.laundry.pk)


class RecipeUnitDeductionTests(InventoryManagementTestCase):
    """Requirement 2: a plate of rice deducts the configured grams of rice."""

    def test_only_the_recipe_quantity_is_deducted_in_the_base_unit(self):
        self.stock_in(self.rice, self.store, 5)
        self.sell(mode=PosOrder.Mode.RESTAURANT, menu_item=self.rice_menu, quantity=3)

        self.assertEqual(self.balance(self.rice, self.store), Decimal("4.2500"), "3 plates × 250 g = 0.75 KG")
        movement = StockMovement.objects.filter(item=self.rice, type=StockMovement.Type.POS_CONSUMPTION).first()
        self.assertEqual(movement.quantity_delta, Decimal("-0.7500"))
        self.assertEqual(movement.location_id, self.store.pk)

    def test_the_typed_unit_is_kept_beside_the_converted_deduction(self):
        recipe = StockRecipe.objects.get(menu_item=self.rice_menu)
        line = recipe.lines.get(item=self.rice)
        self.assertEqual(line.quantity_per_menu_unit, Decimal("0.2500"))
        self.assertEqual(line.entered_quantity, Decimal("250.0000"))
        self.assertEqual(line.entered_unit, "g")
        self.assertFalse(
            StockRecipe.objects.filter(menu_item=self.rice_menu, version__gt=recipe.version).exists(),
            "configuring one recipe must not silently create another version",
        )


class UnitConversionTests(InventoryManagementTestCase):
    """Requirement 2 + 3: exact conversions, and a loud refusal when impossible."""

    def test_mass_and_volume_convert_exactly(self):
        cases = [
            (("500", "g", "KG"), Decimal("0.5000")),
            (("0.25", "kg", "G"), Decimal("250.0000")),
            (("2", "kg", "KG"), Decimal("2.0000")),
            (("1500", "ml", "L"), Decimal("1.5000")),
            (("0.75", "l", "ML"), Decimal("750.0000")),
            (("25", "cl", "L"), Decimal("0.2500")),
            (("25", "cl", "ML"), Decimal("250.0000")),
            (("3", "shot", "ML"), Decimal("150.0000")),
            (("2", "shot", "L"), Decimal("0.1000")),
            (("3", "dozen", "PC"), Decimal("36.0000")),
            (("1", "unit", "PIECE"), Decimal("1.0000")),
        ]
        for (quantity, from_unit, to_unit), expected in cases:
            with self.subTest(quantity=quantity, from_unit=from_unit, to_unit=to_unit):
                self.assertEqual(
                    convert_stock_quantity(quantity, from_unit=from_unit, to_unit=to_unit), expected
                )

    def test_a_container_converts_only_with_the_item_pack_size(self):
        item = StockItem.objects.create(
            sku="ITM-CRATE-PACK", name="Crate packed water", base_unit="BOTTLE", units_per_pack=Decimal("24.00"),
        )
        self.assertEqual(
            convert_stock_quantity("2", from_unit="crate", to_unit="BOTTLE", item=item), Decimal("48.0000")
        )
        self.assertEqual(
            convert_stock_quantity("48", from_unit="BOTTLE", to_unit="carton", item=item), Decimal("2.0000")
        )
        no_pack_size = StockItem.objects.create(sku="ITM-NOPACK", name="Loose water", base_unit="BOTTLE")
        with self.assertRaises(ValidationError) as caught:
            convert_stock_quantity("1", from_unit="crate", to_unit="BOTTLE", item=no_pack_size)
        self.assertIn("cannot be converted", str(caught.exception))

    def test_an_unknown_unit_pair_is_refused_instead_of_passed_through(self):
        with self.assertRaises(ValidationError) as caught:
            convert_stock_quantity("5", from_unit="kg", to_unit="L")
        self.assertIn("cannot be converted", str(caught.exception))

        with self.assertRaises(ValidationError):
            create_stock_recipe(
                menu_item=self.crate_menu, location=self.bar, tracking_mode="COMPONENTS",
                components=[{"item_id": self.bottle.pk, "quantity_per_menu_unit": "5", "unit": "kg"}],
                actor=self.clerk, idempotency_key="itm-bad-unit-recipe",
            )
        self.assertFalse(StockRecipe.objects.filter(menu_item=self.crate_menu).exists())


class InternalIssueTests(InventoryManagementTestCase):
    """Requirement 4: authorised, auditable, non-revenue internal issuance."""

    def issue(self, item, location, quantity, *, key, department="LAUNDRY", recipient="Mrs Bola (laundry)"):
        return issue_stock(
            item=item, location=location, quantity=quantity, actor=self.clerk, source_key=key,
            department=department, recipient=recipient, notes="Monthly departmental issue",
        )

    def test_an_issue_reduces_only_the_source_store_and_carries_its_evidence(self):
        self.stock_in(self.detergent, self.store, 10)
        self.auth(self.clerk)
        response = self.client.post(
            "/api/admin/inventory/issues/",
            {
                "item_id": self.detergent.pk, "location_id": self.store.pk, "quantity": "4.0000",
                "type": "ISSUE", "department": "LAUNDRY", "recipient": "Mrs Bola",
                "notes": "Monthly detergent issue", "idempotency_key": "itm-issue-detergent-1",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.json())
        payload = response.json()["data"]
        self.assertEqual(payload["department"], "LAUNDRY")
        self.assertEqual(payload["department_label"], "Laundry")
        self.assertEqual(payload["recipient"], "Mrs Bola")
        self.assertEqual(payload["actor_email"], self.clerk.email)
        self.assertEqual(payload["notes"], "Monthly detergent issue")
        self.assertTrue(payload["occurred_at"])
        self.assertEqual(self.balance(self.detergent, self.store), Decimal("6.0000"))

        listed = self.client.get(
            "/api/admin/inventory/movements/",
            {"item_id": self.detergent.pk, "department": "LAUNDRY"},
        ).json()["data"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["reference"], payload["reference"])

        other = self.client.get(
            "/api/admin/inventory/movements/", {"item_id": self.detergent.pk, "department": "KITCHEN"}
        ).json()["data"]
        self.assertEqual(other, [])

    def test_an_issue_creates_no_pos_revenue_and_no_duplicate_expense(self):
        self.stock_in(self.detergent, self.store, 10)
        counts_before = {
            "transactions": FinancialTransaction.objects.count(),
            "lines": FinancialLine.objects.count(),
            "postings": FolioPosting.objects.count(),
            "expenses": Expense.objects.count(),
            "pos_orders": PosOrder.objects.count(),
            "tenders": PosTender.objects.count(),
        }

        self.issue(self.detergent, self.store, Decimal("4.0000"), key="itm-issue-no-revenue")

        self.assertEqual(FinancialTransaction.objects.count(), counts_before["transactions"])
        self.assertEqual(FinancialLine.objects.count(), counts_before["lines"])
        self.assertEqual(FolioPosting.objects.count(), counts_before["postings"])
        self.assertEqual(Expense.objects.count(), counts_before["expenses"])
        self.assertEqual(PosOrder.objects.count(), counts_before["pos_orders"])
        self.assertEqual(PosTender.objects.count(), counts_before["tenders"])

    def test_a_reusable_asset_needs_a_named_custodian_and_a_write_off_reason(self):
        self.stock_in(self.towel, self.store, 20)
        with self.assertRaises(ValidationError) as caught:
            issue_stock(
                item=self.towel, location=self.store, quantity=Decimal("2.0000"), actor=self.clerk,
                source_key="itm-towel-nocustodian", department="HOUSEKEEPING", recipient="",
            )
        self.assertIn("reusable asset", str(caught.exception))

        movement, created = issue_stock(
            item=self.towel, location=self.store, quantity=Decimal("2.0000"), actor=self.clerk,
            source_key="itm-towel-custody", department="HOUSEKEEPING", recipient="Grace (housekeeping)",
        )
        self.assertTrue(created)
        self.assertEqual(movement.recipient, "Grace (housekeeping)")
        self.assertEqual(self.balance(self.towel, self.store), Decimal("18.0000"))

        with self.assertRaises(ValidationError) as caught:
            issue_stock(
                item=self.towel, location=self.store, quantity=Decimal("1.0000"), actor=self.clerk,
                source_key="itm-towel-writeoff", movement_type=StockMovement.Type.WRITE_OFF,
                department="HOUSEKEEPING", recipient="Grace (housekeeping)", notes="",
            )
        self.assertIn("reason", str(caught.exception))

    def test_a_return_is_capped_by_the_issue_it_cites(self):
        self.stock_in(self.towel, self.store, 10)
        issue, _ = issue_stock(
            item=self.towel, location=self.store, quantity=Decimal("5.0000"), actor=self.clerk,
            source_key="itm-towel-issue-5", department="HOUSEKEEPING", recipient="Grace (housekeeping)",
        )
        self.assertEqual(self.balance(self.towel, self.store), Decimal("5.0000"))

        returned, created = record_stock_return(
            item=self.towel, location=self.store, quantity=Decimal("3.0000"), actor=self.clerk,
            source_key="itm-towel-return-3", source_reference=issue.reference,
            department="HOUSEKEEPING", recipient="Grace (housekeeping)",
        )
        self.assertTrue(created)
        self.assertEqual(returned.metadata["returned_against_issue"], issue.reference)
        self.assertEqual(self.balance(self.towel, self.store), Decimal("8.0000"))

        with self.assertRaises(ValidationError) as caught:
            record_stock_return(
                item=self.towel, location=self.store, quantity=Decimal("3.0000"), actor=self.clerk,
                source_key="itm-towel-return-6", source_reference=issue.reference,
            )
        self.assertIn("at most 2.0000 can be returned", str(caught.exception))
        self.assertEqual(self.balance(self.towel, self.store), Decimal("8.0000"))

        remaining, _ = record_stock_return(
            item=self.towel, location=self.store, quantity=Decimal("2.0000"), actor=self.clerk,
            source_key="itm-towel-return-2", source_reference=issue.reference,
        )
        self.assertEqual(remaining.quantity_delta, Decimal("2.0000"))
        self.assertEqual(self.balance(self.towel, self.store), Decimal("10.0000"))

    def test_an_unknown_department_is_refused(self):
        with self.assertRaises(ValidationError) as caught:
            self.issue(self.detergent, self.store, Decimal("1.0000"), key="itm-issue-bad-dept",
                       department="SPA", recipient="Someone")
        self.assertIn("not a known department", str(caught.exception))
        self.assertIn(StockDepartment.KITCHEN.value, str(caught.exception))
        self.assertEqual(StockMovement.objects.filter(item=self.detergent).count(), 0)


class IdempotencyTests(InventoryManagementTestCase):
    """Requirement 6: a retry, a failure or a cancellation never double-deducts."""

    def test_retrying_an_issue_returns_the_same_movement(self):
        self.stock_in(self.detergent, self.store, 10)
        first, created = issue_stock(
            item=self.detergent, location=self.store, quantity=Decimal("3.0000"), actor=self.clerk,
            source_key="itm-issue-retry", department="LAUNDRY", recipient="Mrs Bola",
        )
        second, created_again = issue_stock(
            item=self.detergent, location=self.store, quantity=Decimal("3.0000"), actor=self.clerk,
            source_key="itm-issue-retry", department="LAUNDRY", recipient="Mrs Bola",
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(self.balance(self.detergent, self.store), Decimal("7.0000"))

    def test_a_retried_delivery_or_catch_up_sweep_deducts_once(self):
        self.configure_direct_sale(self.bottle, menu_item=self.crate_menu, location=self.bar, quantity="2.0000")
        self.stock_in(self.bottle, self.bar, 10)

        order = self.sell(mode=PosOrder.Mode.BAR, menu_item=self.crate_menu, quantity=1, table_number="T-3")
        request = PosOrder.objects.get(pk=order.pk).stock_consumption_request
        self.assertEqual(request.status, "PROCESSED")
        self.assertEqual(self.balance(self.bottle, self.bar), Decimal("8.0000"))

        # A repeated delivery transition and the catch-up sweep must converge.
        transition_order(
            order=PosOrder.objects.get(pk=order.pk), target_status=PosOrder.Status.DELIVERED, actor=self.cashier
        )
        process_pending_pos_consumption(actor=self.clerk)
        self.assertEqual(self.balance(self.bottle, self.bar), Decimal("8.0000"))
        self.assertEqual(
            StockMovement.objects.filter(item=self.bottle, type=StockMovement.Type.POS_CONSUMPTION).count(), 1
        )

    def test_an_unconfigured_product_does_not_block_the_configured_lines(self):
        """A mixed order deducts what is configured and reports what is not."""
        self.configure_direct_sale(self.bottle, menu_item=self.crate_menu, location=self.bar, quantity="2.0000")
        self.stock_in(self.bottle, self.bar, 10)
        self.stock_in(self.rice, self.store, 5)

        unknown = MenuItem.objects.create(
            category=self.bar_category, name="Snack pack", sku="ITM-SNACK", base_price=Decimal("300.00"),
        )
        order, _ = create_order(
            mode=PosOrder.Mode.BAR, actor=self.cashier, table_number="T-4", guest_name="Mixed order",
            lines=[
                {"menu_item_id": self.crate_menu.pk, "quantity": 1},
                {"menu_item_id": unknown.pk, "quantity": 2},
            ],
            idempotency_key=None,
        )
        submit_order(order=order, actor=self.cashier)
        for status_value in (PosOrder.Status.PREPARING, PosOrder.Status.READY, PosOrder.Status.DELIVERED):
            transition_order(order=order, target_status=status_value, actor=self.cashier)

        self.assertEqual(self.balance(self.bottle, self.bar), Decimal("8.0000"))
        request = PosOrder.objects.get(pk=order.pk).stock_consumption_request
        self.assertEqual(request.status, "PROCESSED")
        self.assertIn("Not deducted", request.resolution_note)
        self.assertIn("ITM-SNACK", request.resolution_note)


class ConcurrencyTests(InventoryManagementTestCase):
    """Requirement 6: concurrent callers must never oversell the same stock."""

    def test_concurrent_issues_cannot_drive_a_balance_negative(self):
        self.stock_in(self.detergent, self.store, 6)

        def worker(index):
            try:
                with transaction.atomic():
                    issue_stock(
                        item=self.detergent, location=self.store, quantity=Decimal("3.0000"),
                        actor=self.clerk, source_key=f"itm-concurrent-{index}",
                        department="LAUNDRY", recipient="Mrs Bola",
                    )
                return True
            except Exception:  # noqa: BLE001 - SQLite writer contention is expected and safe
                return False
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(worker, range(6)))

        posted = sum(1 for ok in results if ok)
        remaining = self.balance(self.detergent, self.store)
        self.assertGreaterEqual(remaining, Decimal("0.0000"), "stock must never go negative")
        self.assertEqual(
            posted * Decimal("3.0000") + remaining, Decimal("6.0000"),
            "every posted issue must have actually reduced the balance",
        )
        self.assertLessEqual(posted, 2)
        self.assertEqual(StockMovement.objects.filter(item=self.detergent).count(), 1 + posted)


class TransferIntegrityTests(InventoryManagementTestCase):
    """Requirement 6: a transfer moves stock without creating or losing any."""

    def test_a_transfer_preserves_the_total_and_is_idempotent(self):
        self.stock_in(self.detergent, self.store, 20)
        (outbound, inbound), created = transfer_stock(
            item=self.detergent, from_location=self.store, to_location=self.kitchen,
            quantity=Decimal("6.0000"), actor=self.clerk, transfer_key="itm-transfer-1",
        )
        self.assertTrue(created)
        self.assertEqual(self.balance(self.detergent, self.store), Decimal("14.0000"))
        self.assertEqual(self.balance(self.detergent, self.kitchen), Decimal("6.0000"))
        self.assertEqual(self.total(self.detergent), Decimal("20.0000"))
        self.assertEqual(outbound.type, StockMovement.Type.TRANSFER_OUT)
        self.assertEqual(inbound.type, StockMovement.Type.TRANSFER_IN)

        (same_out, same_in), created_again = transfer_stock(
            item=self.detergent, from_location=self.store, to_location=self.kitchen,
            quantity=Decimal("6.0000"), actor=self.clerk, transfer_key="itm-transfer-1",
        )
        self.assertFalse(created_again)
        self.assertEqual(same_out.pk, outbound.pk)
        self.assertEqual(same_in.pk, inbound.pk)
        self.assertEqual(self.total(self.detergent), Decimal("20.0000"))

    def test_a_transfer_beyond_the_source_balance_is_refused(self):
        self.stock_in(self.detergent, self.store, 2)
        with self.assertRaises(ValidationError):
            transfer_stock(
                item=self.detergent, from_location=self.store, to_location=self.kitchen,
                quantity=Decimal("5.0000"), actor=self.clerk, transfer_key="itm-transfer-over",
            )
        self.assertEqual(self.balance(self.detergent, self.store), Decimal("2.0000"))
        self.assertEqual(self.balance(self.detergent, self.kitchen), Decimal("0.0000"))


class ValuationIntegrityTests(InventoryManagementTestCase):
    """Requirement 6: weighted-average cost and valuation rules are unchanged."""

    def test_weighted_average_cost_survives_receipts_issues_and_transfers(self):
        item = StockItem.objects.create(
            sku="ITM-VALU", name="Valuation test item", base_unit="UNIT",
            item_type=StockItem.ItemType.CONSUMABLE, standard_cost=Decimal("150.00"),
        )
        self.receive_item(item, self.store, 10, unit_cost="100.00", key="itm-valu-a")
        self.receive_item(item, self.store, 10, unit_cost="200.00", key="itm-valu-b")

        rows = product_profitability_projection(item_ids=[item.pk], location_id=self.store.pk)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["purchase_unit_cost"], Decimal("150.00"))
        self.assertEqual(rows[0]["cost_basis"], "WEIGHTED_AVERAGE_RECEIPT")
        self.assertEqual(rows[0]["current_stock"], Decimal("20.0000"))
        self.assertEqual(rows[0]["stock_value"], Decimal("3000.00"))

        issue_stock(
            item=item, location=self.store, quantity=Decimal("5.0000"), actor=self.clerk,
            source_key="itm-valu-issue", department="KITCHEN", recipient="Store keeper",
        )
        transfer_stock(
            item=item, from_location=self.store, to_location=self.kitchen, quantity=Decimal("5.0000"),
            actor=self.clerk, transfer_key="itm-valu-transfer",
        )

        rows = product_profitability_projection(item_ids=[item.pk], location_id=self.store.pk)
        self.assertEqual(rows[0]["purchase_unit_cost"], Decimal("150.00"), "issuing stock cannot change the cost basis")
        self.assertEqual(rows[0]["current_stock"], Decimal("10.0000"))
        self.assertEqual(rows[0]["stock_value"], Decimal("1500.00"))

        # Valuation stays per item AND location: a transfer alone creates no
        # receipt basis at the destination, so the documented fallback (the
        # item's standard cost) applies there — nothing is averaged across
        # locations behind the operator's back.
        kitchen_rows = product_profitability_projection(item_ids=[item.pk], location_id=self.kitchen.pk)
        self.assertEqual(kitchen_rows[0]["current_stock"], Decimal("5.0000"))
        self.assertEqual(kitchen_rows[0]["purchase_unit_cost"], Decimal("150.00"))
        self.assertEqual(kitchen_rows[0]["cost_basis"], "STANDARD_COST")
        self.assertEqual(kitchen_rows[0]["stock_value"], Decimal("750.00"))

    def test_a_direct_sale_configuration_prices_the_projection_per_base_unit(self):
        item = StockItem.objects.create(
            sku="ITM-CRATE-VALU", name="Water crate (valuation)", base_unit="BOTTLE",
            item_type=StockItem.ItemType.SELLABLE_PRODUCT, standard_cost=Decimal("120.00"),
        )
        self.receive_item(item, self.bar, 48, unit_cost="120.00", key="itm-crate-valu")
        self.configure_direct_sale(item, menu_item=self.crate_menu, location=self.bar, quantity="24.0000")

        rows = product_profitability_projection(item_ids=[item.pk], location_id=self.bar.pk)
        self.assertEqual(rows[0]["price_basis"], "MENU_ITEM_MATCH")
        # 6000 per crate ÷ 24 bottles = 250 per bottle of implied selling price.
        self.assertEqual(rows[0]["selling_price"], Decimal("250.00"))
        self.assertEqual(rows[0]["recipe_count"], 0)


class ExistingBehaviourTests(InventoryManagementTestCase):
    """Requirement 10: existing recipes, POS flows and history keep working."""

    def test_existing_recipes_still_deduct_and_their_history_is_unchanged(self):
        before = StockMovement.objects.count()
        self.stock_in(self.bottle, self.bar, 10)
        self.sell(mode=PosOrder.Mode.BAR, quantity=1, table_number="T-5")

        self.assertEqual(self.balance(self.bottle, self.bar), Decimal("9.0000"))
        self.assertEqual(StockMovement.objects.count(), before + 2)
        request = PosOrder.objects.latest("pk").stock_consumption_request
        self.assertEqual(request.status, "PROCESSED")
        recipe = StockRecipe.objects.get(menu_item=self.bar_water)
        self.assertTrue(recipe.is_active, "an existing recipe is never retired by an unrelated change")

    def test_barcode_lookup_still_resolves_the_sku_and_the_order_total_is_unchanged(self):
        from apps.pos.services.barcode_service import resolve_barcode

        # The SKU is auto-adopted as the item's Code 128 barcode on creation, so
        # the same label resolves either way — scanning behaviour is untouched.
        item, matched_by = resolve_barcode(self.bar_water.sku, service_areas=[MenuCategory.ServiceArea.BAR])
        self.assertEqual(item.pk, self.bar_water.pk)
        self.assertIn(matched_by, ("sku", "barcode"))

        # Stocked store, so order entry's stock validation accepts the sale.
        self.stock_in(self.bottle, self.bar, 10)
        order = self.sell(mode=PosOrder.Mode.BAR, quantity=2, table_number="T-6")
        self.assertEqual(order.total_amount, Decimal("600.00"))

    def test_the_configuration_audit_reports_without_changing_anything(self):
        self.configure_direct_sale(self.bottle, menu_item=self.crate_menu, location=self.bar, quantity="24.0000")
        self.stock_in(self.bottle, self.bar, 30)
        self.stock_in(self.detergent, self.store, 10)
        issue_stock(
            item=self.detergent, location=self.store, quantity=Decimal("2.0000"), actor=self.clerk,
            source_key="itm-audit-issue", department="LAUNDRY", recipient="Mrs Bola",
            notes="Monthly issue",
        )
        # A second record with the same name: a candidate, never merged.
        StockItem.objects.create(sku="ITM-DETERGENT-2", name="Laundry detergent (1 kg)", base_unit="PACK")
        snapshot = {
            "balances": list(StockBalance.objects.values_list("item_id", "location_id", "quantity_on_hand")),
            "movements": StockMovement.objects.count(),
            "recipes": StockRecipe.objects.count(),
            "items": StockItem.objects.count(),
        }

        out = StringIO()
        call_command("inventory_configuration_audit", stdout=out)
        text = out.getvalue()

        self.assertIn("Direct-sale configuration", text)
        self.assertIn("DEP-WATER", text)
        self.assertIn("ITM-MENU-CRATE", text)
        self.assertIn("Internal issues", text)
        self.assertIn("LAUNDRY", text)
        self.assertIn("Possible duplicate records", text)
        self.assertIn("Laundry detergent (1 kg)", text)
        self.assertIn("nothing was changed", text)

        self.assertEqual(
            list(StockBalance.objects.values_list("item_id", "location_id", "quantity_on_hand")),
            snapshot["balances"],
        )
        self.assertEqual(StockMovement.objects.count(), snapshot["movements"])
        self.assertEqual(StockRecipe.objects.count(), snapshot["recipes"])
        self.assertEqual(StockItem.objects.count(), snapshot["items"])


class ItemTypeApiTests(InventoryManagementTestCase):
    """Requirement 1: the classification is stored, exposed and filterable."""

    def test_the_four_types_are_recorded_and_default_to_consumable(self):
        untyped = StockItem.objects.create(sku="ITM-UNTYPED", name="Unclassified thing", base_unit="UNIT")
        self.assertEqual(untyped.item_type, StockItem.ItemType.CONSUMABLE)
        self.assertEqual({row for row in StockItem.ItemType.values}, {
            "SELLABLE_PRODUCT", "RECIPE_INGREDIENT", "CONSUMABLE", "REUSABLE_ASSET",
        })

        self.auth(self.manager)
        payload = self.client.get(f"/api/admin/inventory/items/?search=ITM-TOWEL").json()["data"]
        row = next(entry for entry in payload if entry["sku"] == "ITM-TOWEL")
        self.assertEqual(row["item_type"], "REUSABLE_ASSET")
        self.assertEqual(row["item_type_label"], "Reusable asset")
        self.assertFalse(row["direct_sale_configured"])
        self.assertEqual(row["direct_sale_quantity"], "1.0000")

        self.stock_in(self.rice, self.store, 3)
        balances = self.client.get(
            "/api/admin/inventory/balances/", {"item_type": "RECIPE_INGREDIENT"}
        ).json()["data"]
        self.assertTrue(balances)
        self.assertTrue(all(entry["item_type"] == "RECIPE_INGREDIENT" for entry in balances))


class StorekeeperPermissionTests(BaseAPITestCase):
    """Requirement 6: server-side permissions, not just hidden buttons."""

    def test_a_role_without_inventory_capability_cannot_issue_stock(self):
        location = StockLocation.objects.create(code="PERM-STORE", name="Permission store")
        item = StockItem.objects.create(sku="PERM-ITEM", name="Permission item", base_unit="UNIT")
        housekeeper = make_staff("perm-housekeeper@staff.dev", role=User.Role.HOUSEKEEPING)
        self.auth(housekeeper)

        response = self.client.post(
            "/api/admin/inventory/issues/",
            {
                "item_id": item.pk, "location_id": location.pk, "quantity": "1.0000",
                "type": "ISSUE", "idempotency_key": "perm-issue-1",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(StockMovement.objects.filter(item=item).count(), 0)


class ItemTypeBackfillTests(InventoryManagementTestCase):
    """The deployment step classifies only what the data already proves.

    ``0006`` adds ``item_type`` with a safe default; ``0007`` may then label the
    items the recipe book proves a use for. It must never guess from a name,
    never overwrite a classification somebody made, and never touch a balance,
    a movement, a recipe or a purchase.
    """

    backfill = import_module("apps.inventory.migrations.0007_classify_existing_stock_items")

    def _prove(self):
        """Build one 1:1 product, one measured ingredient and one untouched item."""
        bottle = StockItem.objects.create(
            sku="BF-BOTTLE", name="Backfill bottle", base_unit="BOTTLE",
            item_type=StockItem.ItemType.CONSUMABLE,
        )
        sold_as_is = MenuItem.objects.create(
            category=self.bar_category, name="Backfill water", sku="BF-MENU-1",
            base_price=Decimal("200.00"),
        )
        create_stock_recipe(
            menu_item=sold_as_is, location=self.store, tracking_mode="COMPONENTS",
            components=[{"item_id": bottle.pk, "quantity_per_menu_unit": "1"}],
            actor=self.clerk, idempotency_key="bf-one-for-one",
        )

        measured = StockItem.objects.create(
            sku="BF-TOMATOES", name="Backfill tomatoes", base_unit="KG",
            item_type=StockItem.ItemType.CONSUMABLE,
        )
        stew = MenuItem.objects.create(
            category=self.restaurant_category, name="Backfill stew", sku="BF-MENU-2",
            base_price=Decimal("1500.00"),
        )
        create_stock_recipe(
            menu_item=stew, location=self.store, tracking_mode="COMPONENTS",
            components=[
                {"item_id": measured.pk, "quantity_per_menu_unit": "0.5", "unit": "kg"},
                {"item_id": self.rice.pk, "quantity_per_menu_unit": "200", "unit": "g"},
            ],
            actor=self.clerk, idempotency_key="bf-measured",
        )
        untouched = StockItem.objects.create(
            sku="BF-BLEACH", name="Backfill bleach", base_unit="BOTTLE",
            item_type=StockItem.ItemType.CONSUMABLE,
        )
        return bottle, measured, untouched

    def test_only_recipe_evidence_is_classified_and_nothing_else_moves(self):
        bottle, measured, untouched = self._prove()
        # A classification an operator already made is respected, even where the
        # recipe book would suggest something else.
        StockItem.objects.filter(pk=self.rice.pk).update(item_type=StockItem.ItemType.REUSABLE_ASSET)

        before = {
            "balances": list(StockBalance.objects.values_list("item_id", "location_id", "quantity_on_hand")),
            "movements": StockMovement.objects.count(),
            "recipes": StockRecipe.objects.count(),
            "lines": StockRecipe.objects.filter(is_active=True).count(),
        }

        self.backfill.classify_stock_items(global_apps, None)

        bottle.refresh_from_db(); measured.refresh_from_db(); untouched.refresh_from_db()
        self.rice.refresh_from_db()
        self.assertEqual(bottle.item_type, StockItem.ItemType.SELLABLE_PRODUCT)
        self.assertEqual(measured.item_type, StockItem.ItemType.RECIPE_INGREDIENT)
        self.assertEqual(untouched.item_type, StockItem.ItemType.CONSUMABLE, "no evidence, no label")
        self.assertEqual(self.rice.item_type, StockItem.ItemType.REUSABLE_ASSET, "never overwritten")

        self.assertEqual(list(StockBalance.objects.values_list("item_id", "location_id", "quantity_on_hand")), before["balances"])
        self.assertEqual(StockMovement.objects.count(), before["movements"])
        self.assertEqual(StockRecipe.objects.count(), before["recipes"])

    def test_the_backfill_is_reversible_and_reverts_only_its_own_rows(self):
        bottle, measured, _untouched = self._prove()
        self.backfill.classify_stock_items(global_apps, None)
        bottle.refresh_from_db(); measured.refresh_from_db()
        self.assertEqual(bottle.item_type, StockItem.ItemType.SELLABLE_PRODUCT)

        # An operator re-classified the bottle after the migration: the reverse
        # pass leaves that decision alone.
        StockItem.objects.filter(pk=bottle.pk).update(item_type=StockItem.ItemType.CONSUMABLE)

        self.backfill.revert_classification(global_apps, None)

        bottle.refresh_from_db(); measured.refresh_from_db()
        self.assertEqual(bottle.item_type, StockItem.ItemType.CONSUMABLE)
        self.assertEqual(measured.item_type, StockItem.ItemType.CONSUMABLE)
        self.assertEqual(
            StockItem.objects.filter(sku__startswith="BF-").count(), 3,
            "reversing a classification never deletes an item",
        )

    def test_running_the_backfill_twice_changes_nothing(self):
        bottle, measured, _untouched = self._prove()
        self.backfill.classify_stock_items(global_apps, None)
        snapshot = dict(StockItem.objects.values_list("pk", "item_type"))
        self.backfill.classify_stock_items(global_apps, None)
        self.assertEqual(dict(StockItem.objects.values_list("pk", "item_type")), snapshot)
        bottle.refresh_from_db(); measured.refresh_from_db()
        self.assertEqual(bottle.item_type, StockItem.ItemType.SELLABLE_PRODUCT)
        self.assertEqual(measured.item_type, StockItem.ItemType.RECIPE_INGREDIENT)


class PortableConstraintTests(InventoryManagementTestCase):
    """The one-item-per-menu-item guard must exist on MySQL/MariaDB too.

    The original constraint was CONDITIONAL (``condition=Q(...isnull=False)``).
    MySQL and MariaDB have no partial indexes, so Django created the table
    WITHOUT the constraint and only printed ``models.W036`` — the double
    deduction guard that requirement 5 depends on was missing on exactly the
    servers the hotel runs (the reported ``python manage.py migrate`` warning).

    A plain unique constraint has identical meaning for a nullable column
    (NULLs compare as distinct everywhere), so it is used instead.
    """

    def test_the_constraint_is_portable_and_really_enforced(self):
        from django.db.models import UniqueConstraint

        (constraint,) = [
            row for row in StockItem._meta.constraints
            if row.name == "one_direct_sale_stock_item_per_menu_item"
        ]
        self.assertIsInstance(constraint, UniqueConstraint)
        # The condition is what MySQL/MariaDB silently drop (models.W036).
        self.assertIsNone(
            getattr(constraint, "condition", None),
            "a conditional unique constraint would not be created on MariaDB",
        )
        self.assertNotIn("condition", getattr(constraint, "kwargs", {}))

        # It is not merely declared: the database refuses the duplicate.
        from django.db import IntegrityError

        first = StockItem.objects.get(sku="ITM-RICE")
        StockItem.objects.filter(pk=first.pk).update(direct_sale_menu_item=self.crate_menu)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                StockItem.objects.create(
                    sku="ITM-RICE-DUPLICATE", name="Second claim on the same menu item",
                    base_unit="KG", direct_sale_menu_item=self.crate_menu,
                )

        # …and unconfigured items (NULL menu item) stay unconstrained, which is
        # what the condition used to express.
        unconfigured_before = StockItem.objects.filter(direct_sale_menu_item__isnull=True).count()
        StockItem.objects.create(sku="ITM-NULL-1", name="Unconfigured A", base_unit="KG")
        StockItem.objects.create(sku="ITM-NULL-2", name="Unconfigured B", base_unit="KG")
        self.assertEqual(
            StockItem.objects.filter(direct_sale_menu_item__isnull=True).count(),
            unconfigured_before + 2,
            "many items may sit without a direct-sale claim",
        )
