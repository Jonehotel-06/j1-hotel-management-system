"""Batch 05 backend/API regressions; no external services required."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
import uuid

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import Capability, RoleCapability, User
from apps.inventory.models import StockBalance, StockConsumptionRequest, StockItem, StockLocation, StockMovement, StockRecipe
from apps.inventory.services.inventory_service import create_stock_recipe, record_stock_movement, retire_stock_recipe
from apps.pos.models import FoodAvailabilityEvent, MenuCategory, MenuItem, PosOrder, PreparedPortionAllocation
from apps.pos.services.food_service import change_food, prepare_nonstock_food_handoff
from apps.pos.services.order_service import create_order, submit_order, transition_order
from apps.pos.services.return_service import record_unopened_return
from .base import BaseAPITestCase
from .factories import make_staff


def fixtures(case):
    case.actor = make_staff("batch05-admin@staff.dev", role=User.Role.ADMIN)
    case.category = MenuCategory.objects.create(name="Prepared dishes", slug="prepared-dishes")
    case.food = MenuItem.objects.create(category=case.category, name="Jollof rice", sku="B05-JOLLOF", base_price="2500")


class PreparedFoodTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        fixtures(self)
        self.auth(self.actor)

    def change(self, action, quantity=0, reason="", key=None):
        return change_food(item_id=self.food.pk, action=action, quantity=quantity, reason=reason,
                           actor=self.actor, idempotency_key=key or str(uuid.uuid4()))

    def portions(self, count=5):
        self.change("ENABLE_PORTIONS")
        self.change("REPLENISH", count)

    def draft(self, count=1, lines=None, mode="RESTAURANT"):
        return create_order(mode=mode, actor=self.actor, lines=lines or [{"menu_item_id": self.food.pk, "quantity": count}])[0]

    def submit(self, order):
        return submit_order(order=order, actor=self.actor)

    def transition(self, order, target):
        return transition_order(order=order, target_status=target, actor=self.actor)

    def balance(self):
        self.food.refresh_from_db()
        return self.food.prepared_portions

    def recipe(self):
        self.location = StockLocation.objects.create(code="B05-KITCHEN", name="Raw ingredient store")
        self.rice = StockItem.objects.create(sku="B05-RAW-RICE", name="Raw rice", base_unit="KG", item_type="RECIPE_INGREDIENT")
        record_stock_movement(item=self.rice, location=self.location, movement_type="OPENING", quantity_delta="10",
                              source_key="batch05-opening", actor=self.actor)
        return create_stock_recipe(menu_item=self.food, location=self.location, tracking_mode="COMPONENTS",
                                   components=[{"item_id": self.rice.pk, "quantity_per_menu_unit": "250", "unit": "g"}],
                                   actor=self.actor, idempotency_key="batch05-recipe")[0]

    def test_ready_food_without_recipe_creates_draft_and_delivers_without_ingredients(self):
        order = self.submit(self.draft())
        self.transition(order, "DELIVERED")
        request = StockConsumptionRequest.objects.get(pos_order=order)
        self.assertEqual(request.status, "SKIPPED")
        self.assertFalse(StockRecipe.objects.exists())
        self.assertFalse(StockMovement.objects.filter(type="POS_CONSUMPTION").exists())

    def test_unavailable_rejects_new_draft_and_preserves_other_orders(self):
        existing = self.submit(self.draft())
        self.change("NOT_AVAILABLE")
        with self.assertRaises(ValidationError):
            self.draft()
        self.assertEqual(PosOrder.objects.get(pk=existing.pk).status, "SUBMITTED")
        self.transition(existing, "DELIVERED")

    def test_draft_submission_rechecks_availability_and_retains_draft(self):
        order = self.draft()
        self.change("NOT_AVAILABLE")
        with self.assertRaises(ValidationError):
            self.submit(order)
        order.refresh_from_db()
        self.assertEqual(order.status, "DRAFT")
        self.assertEqual(order.lines.count(), 1)

    def test_optional_portions_reserve_at_submission_not_at_draft(self):
        self.portions(5)
        order = self.draft(3)
        self.assertEqual(self.balance(), 5)
        self.submit(order)
        self.assertEqual(self.balance(), 2)
        self.submit(order)
        self.assertEqual(self.balance(), 2)
        self.assertEqual(PreparedPortionAllocation.objects.get(order=order).quantity, 3)

    def test_duplicate_lines_aggregate_before_draft_validation(self):
        self.portions(3)
        with self.assertRaises(ValidationError):
            self.draft(lines=[{"menu_item_id": self.food.pk, "quantity": 2}] * 2)
        self.assertFalse(PosOrder.objects.exists())
        self.assertEqual(self.balance(), 3)

    def test_competing_drafts_cannot_both_be_accepted(self):
        self.portions(3)
        first, second = self.draft(2), self.draft(2)
        self.submit(first)
        with self.assertRaises(ValidationError):
            self.submit(second)
        self.assertEqual(self.balance(), 1)
        self.assertEqual(PosOrder.objects.get(pk=second.pk).status, "DRAFT")

    def test_cancel_releases_exactly_once_even_when_marked_unavailable(self):
        self.portions(3)
        order = self.submit(self.draft(2))
        self.change("NOT_AVAILABLE")
        self.transition(order, "CANCELLED")
        self.transition(order, "CANCELLED")
        self.assertEqual(self.balance(), 3)
        self.assertFalse(self.food.is_available)
        self.assertEqual(PreparedPortionAllocation.objects.get(order=order).state, "RELEASED")

    def test_cancel_draft_does_not_increase_portions(self):
        self.portions(3)
        self.transition(self.draft(2), "CANCELLED")
        self.assertEqual(self.balance(), 3)

    def test_delivery_does_not_deduct_portions_twice(self):
        self.portions(5)
        order = self.submit(self.draft(2))
        self.transition(order, "DELIVERED")
        self.transition(order, "DELIVERED")
        self.assertEqual(self.balance(), 3)
        self.assertEqual(PreparedPortionAllocation.objects.get(order=order).state, "CONSUMED")

    def test_wastage_controlled_and_replenishment_idempotent(self):
        self.portions(5)
        self.change("REPLENISH", 2, key="same-preparation")
        self.change("REPLENISH", 2, key="same-preparation")
        self.assertEqual(self.balance(), 7)
        with self.assertRaises(ValidationError):
            self.change("WASTE", 2)
        with self.assertRaises(ValidationError):
            self.change("WASTE", 8, "Spoiled")
        self.change("WASTE", 2, "Spoiled", key="waste-once")
        self.change("WASTE", 2, "Spoiled", key="waste-once")
        self.assertEqual(self.balance(), 5)
        with self.assertRaises(ValidationError):
            self.change("REPLENISH", 3, key="same-preparation")

    def test_wastage_cannot_touch_reserved_portions(self):
        self.portions(5)
        self.submit(self.draft(4))
        with self.assertRaises(ValidationError):
            self.change("WASTE", 2, "Spilled")
        self.assertEqual(self.balance(), 1)

    def test_disable_cannot_discard_balance_or_orphan_reservations(self):
        self.portions(2)
        with self.assertRaises(ValidationError):
            self.change("DISABLE_PORTIONS")
        order = self.submit(self.draft(2))
        with self.assertRaises(ValidationError):
            self.change("DISABLE_PORTIONS")
        self.transition(order, "DELIVERED")
        self.change("DISABLE_PORTIONS")
        self.food.refresh_from_db()
        self.assertFalse(self.food.portion_tracking)

    def test_existing_active_recipe_converts_base_units_and_deducts_once(self):
        recipe = self.recipe()
        self.portions(5)
        order = self.submit(self.draft(2))
        self.transition(order, "DELIVERED")
        self.transition(order, "DELIVERED")
        self.assertEqual(self.balance(), 3)
        self.assertEqual(StockBalance.objects.get(item=self.rice).quantity_on_hand, Decimal("9.5"))
        self.assertEqual(StockMovement.objects.filter(type="POS_CONSUMPTION").count(), 1)
        self.assertTrue(StockRecipe.objects.filter(pk=recipe.pk, is_active=True).exists())

    def test_retired_recipe_preserved_and_never_deducted(self):
        recipe = self.recipe()
        retire_stock_recipe(recipe=recipe, actor=self.actor)
        order = self.submit(self.draft(2))
        self.transition(order, "DELIVERED")
        self.assertEqual(StockBalance.objects.get(item=self.rice).quantity_on_hand, Decimal("10"))
        self.assertTrue(StockRecipe.objects.filter(pk=recipe.pk, is_active=False).exists())
        self.assertFalse(StockMovement.objects.filter(type="POS_CONSUMPTION").exists())

    def test_returned_food_not_resold_or_uncooked_into_ingredients(self):
        self.recipe()
        self.portions(5)
        order = self.submit(self.draft(2))
        self.transition(order, "DELIVERED")
        args = dict(order=order, lines=[{"order_line_id": order.lines.get().pk, "quantity_returned": 1}],
                    actor=self.actor, reason="Returned food", idempotency_key="food-return-once")
        record, created = record_unopened_return(**args)
        self.assertTrue(created)
        self.assertFalse(record_unopened_return(**args)[1])
        self.assertEqual(self.balance(), 3)
        self.assertEqual(StockBalance.objects.get(item=self.rice).quantity_on_hand, Decimal("9.5"))
        self.assertEqual(FoodAvailabilityEvent.objects.filter(action="RETURN_DISCARD").count(), 1)
        self.assertEqual(record.returned_amount, Decimal("2500"))

    def test_simple_food_return_without_stock_mapping_does_not_fail(self):
        order = self.submit(self.draft())
        self.transition(order, "DELIVERED")
        record, _ = record_unopened_return(order=order, actor=self.actor,
            lines=[{"order_line_id": order.lines.get().pk, "quantity_returned": 1}], idempotency_key="simple-return")
        self.assertEqual(record.returned_amount, Decimal("2500"))
        self.assertEqual(FoodAvailabilityEvent.objects.get(action="RETURN_DISCARD").details["quantity_discarded"], 1)

    def test_pos_and_barcode_publish_portion_ceiling(self):
        self.portions(2)
        response = self.client.get("/api/admin/pos/menu/")
        item = response.json()["data"][0]["items"][0]
        self.assertEqual(item["stock"]["available_quantity"], "2")
        self.assertTrue(item["portion_tracking"])
        self.change("NOT_AVAILABLE")
        self.assertFalse(self.client.get("/api/admin/pos/menu/").json()["data"][0]["items"])
        from apps.pos.services.barcode_service import resolve_menu_item
        from rest_framework.exceptions import NotFound
        with self.assertRaises(NotFound):
            resolve_menu_item(self.food.pk)

    def test_controls_api_audit_validation_and_permissions(self):
        path = f"/api/admin/pos/kitchen-food/{self.food.pk}/"
        response = self.client.post(path, {"action": "NOT_AVAILABLE", "idempotency_key": "api-ready"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        event = FoodAvailabilityEvent.objects.get()
        self.assertEqual(event.actor_id, self.actor.pk)
        self.assertIsNotNone(event.created_at)
        self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.post(path, {"action": "WASTE", "idempotency_key": "invalid", "quantity": 1}, format="json").status_code, 400)
        self.unauth()
        self.assertIn(self.client.post(path, {}, format="json").status_code, [401, 403])

    def test_chef_can_manage_food_but_not_bar_and_waiter_cannot_write(self):
        # Explicit grants keep this deterministic after TransactionTestCase flushes.
        for role, code in [(User.Role.CHEF, "kitchen.ticket.manage"), (User.Role.WAITER, "kitchen.queue.view")]:
            cap, _ = Capability.objects.get_or_create(code=code, defaults={"name": code})
            RoleCapability.objects.get_or_create(role=role, capability=cap)
        chef = make_staff("batch05-chef@staff.dev", role=User.Role.CHEF)
        self.auth(chef)
        self.assertEqual(self.client.post(f"/api/admin/pos/kitchen-food/{self.food.pk}/", {"action": "READY", "idempotency_key": "chef"}, format="json").status_code, 200)
        bar = MenuCategory.objects.create(name="Bar", slug="batch05-bar", service_area="BAR")
        drink = MenuItem.objects.create(category=bar, name="Beer", sku="B05-BEER", base_price=500)
        self.assertEqual(self.client.post(f"/api/admin/pos/kitchen-food/{drink.pk}/", {"action": "READY", "idempotency_key": "bar"}, format="json").status_code, 400)
        self.auth(make_staff("batch05-waiter@staff.dev", role=User.Role.WAITER))
        self.assertEqual(self.client.post(f"/api/admin/pos/kitchen-food/{self.food.pk}/", {"action": "READY", "idempotency_key": "waiter"}, format="json").status_code, 403)

    def test_food_listing_has_constant_query_count(self):
        url = "/api/admin/pos/kitchen-food/?page_size=100"
        self.client.get(url)  # capability/cache warmup
        with CaptureQueriesContext(connection) as small:
            self.assertEqual(self.client.get(url).status_code, 200)
        MenuItem.objects.bulk_create([MenuItem(category=self.category, name=f"Dish {n}", sku=f"B05-{n}", base_price=500) for n in range(30)])
        with CaptureQueriesContext(connection) as large:
            self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(len(large), len(small))

    def test_legacy_availability_patch_is_audited(self):
        response = self.client.patch(f"/api/admin/pos/menu/items/{self.food.pk}/", {"is_available": False}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(FoodAvailabilityEvent.objects.get().action, "NOT_AVAILABLE")

    def test_failed_ingredient_recheck_rolls_back_prepared_reservation(self):
        from apps.core.exceptions import PosStockConflictError
        self.recipe()
        self.portions(5)
        order = self.draft(2)
        StockBalance.objects.filter(item=self.rice).update(quantity_on_hand=0)
        with self.assertRaises(PosStockConflictError):
            self.submit(order)
        self.assertEqual(self.balance(), 5)
        self.assertFalse(PreparedPortionAllocation.objects.exists())
        self.assertFalse(FoodAvailabilityEvent.objects.filter(action="RESERVE").exists())
        self.assertEqual(PosOrder.objects.get(pk=order.pk).status, "DRAFT")

    def test_explicit_nonstock_recipe_provenance_is_preserved(self):
        location = StockLocation.objects.create(code="B05-NOSTOCK", name="Kitchen")
        recipe, _ = create_stock_recipe(menu_item=self.food, location=location, tracking_mode="NO_STOCK", components=[],
                                        actor=self.actor, idempotency_key="b05-explicit-no-stock")
        order = self.submit(self.draft())
        self.transition(order, "DELIVERED")
        request = StockConsumptionRequest.objects.get(pos_order=order)
        self.assertEqual(request.status, "SKIPPED")
        self.assertEqual(request.payload["recipe_snapshot"][str(self.food.pk)]["recipe_reference"], recipe.reference)


class PreparedFoodConcurrencyTests(TransactionTestCase):
    """Run on MySQL or file-backed SQLite; shared-memory SQLite cannot wait on writes."""
    def setUp(self):
        fixtures(self)
        self.food.portion_tracking = True
        self.food.prepared_portions = 3
        self.food.save()

    def run_parallel(self, operation):
        if connection.vendor == "sqlite" and "memory" in str(connection.settings_dict["NAME"]):
            self.skipTest("Use file-backed TEST.NAME for SQLite write concurrency; see TEST_REPORT.md.")
        barrier = Barrier(2)
        def worker(value):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return operation(value)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(worker, range(2)))

    def test_simultaneous_acceptance_never_oversells(self):
        orders = [create_order(mode="RESTAURANT", actor=self.actor, lines=[{"menu_item_id": self.food.pk, "quantity": 2}])[0] for _ in range(2)]
        def accept(i):
            try:
                submit_order(order=orders[i], actor=self.actor)
                return "accepted"
            except ValidationError:
                return "rejected"
        self.assertCountEqual(self.run_parallel(accept), ["accepted", "rejected"])
        self.food.refresh_from_db()
        self.assertEqual(self.food.prepared_portions, 1)
        self.assertEqual(PreparedPortionAllocation.objects.count(), 1)

    def test_simultaneous_retry_reserves_once(self):
        order = create_order(mode="RESTAURANT", actor=self.actor, lines=[{"menu_item_id": self.food.pk, "quantity": 2}])[0]
        self.run_parallel(lambda _: submit_order(order=order, actor=self.actor).pk)
        self.food.refresh_from_db()
        self.assertEqual(self.food.prepared_portions, 1)
        self.assertEqual(FoodAvailabilityEvent.objects.filter(action="RESERVE").count(), 1)

    def test_simultaneous_replenishment_retry_adds_once(self):
        self.run_parallel(lambda _: change_food(item_id=self.food.pk, action="REPLENISH", quantity=2,
                                               actor=self.actor, idempotency_key="same-replenishment")[1])
        self.food.refresh_from_db()
        self.assertEqual(self.food.prepared_portions, 5)
        self.assertEqual(FoodAvailabilityEvent.objects.filter(action="REPLENISH").count(), 1)
    def test_simultaneous_cancellation_releases_once(self):
        order = create_order(mode="RESTAURANT", actor=self.actor, lines=[{"menu_item_id": self.food.pk, "quantity": 2}])[0]
        submit_order(order=order, actor=self.actor)
        self.run_parallel(lambda _: transition_order(order=order, target_status="CANCELLED", actor=self.actor).pk)
        self.food.refresh_from_db()
        self.assertEqual(self.food.prepared_portions, 3)
        self.assertEqual(FoodAvailabilityEvent.objects.filter(action="RELEASE").count(), 1)

    def test_simultaneous_wastage_cannot_make_balance_negative(self):
        def waste(i):
            try:
                change_food(item_id=self.food.pk, action="WASTE", quantity=2, reason="Spoiled", actor=self.actor,
                            idempotency_key=f"waste-{i}")
                return "recorded"
            except ValidationError:
                return "rejected"
        self.assertCountEqual(self.run_parallel(waste), ["recorded", "rejected"])
        self.food.refresh_from_db()
        self.assertEqual(self.food.prepared_portions, 1)


class PreparedFoodMigrationTests(TransactionTestCase):
    def test_schema_roundtrip_preserves_recipe_records_and_availability(self):
        from django.core.management import call_command
        fixtures(self)
        location = StockLocation.objects.create(code="B05-MIG", name="Migration kitchen")
        raw = StockItem.objects.create(sku="B05-MIG-RAW", name="Raw rice", base_unit="KG", item_type="RECIPE_INGREDIENT")
        recipe = create_stock_recipe(menu_item=self.food, location=location, tracking_mode="COMPONENTS",
                                     components=[{"item_id": raw.pk, "quantity_per_menu_unit": "0.25"}],
                                     actor=self.actor, idempotency_key="migration-recipe")[0]
        retired = StockRecipe.objects.create(reference="B05-RETIRED", menu_item=self.food, location=location,
                                            version=2, is_active=False, notes="Keep historical data")
        self.food.is_available = False
        self.food.save()
        recipes_before = list(StockRecipe.objects.order_by("pk").values())
        lines_before = list(recipe.lines.values())
        try:
            call_command("migrate", "pos", "0008", verbosity=0, interactive=False)
            self.assertEqual(list(StockRecipe.objects.order_by("pk").values()), recipes_before)
        finally:
            call_command("migrate", "pos", "0009", verbosity=0, interactive=False)
        self.food.refresh_from_db()
        self.assertFalse(self.food.is_available)
        self.assertFalse(self.food.portion_tracking)
        self.assertEqual(self.food.prepared_portions, 0)
        self.assertEqual(list(StockRecipe.objects.order_by("pk").values()), recipes_before)
        self.assertEqual(list(recipe.lines.values()), lines_before)
