"""Portal menu -> one server-priced, stock-controlled POS/folio workflow."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier, Event
from unittest import mock

from django.core.cache import cache
from django.db import OperationalError, close_old_connections, connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Capability, RoleCapability, User
from apps.audit.models import AuditLog
from apps.bookings.models import Booking, RoomOccupant
from apps.core.utils import hotel_today
from apps.finance.models import FinancialTransaction, FolioPosting
from apps.guest_services.models import ServiceRequest, ServiceRequestEvent
from apps.inventory.models import StockBalance, StockConsumptionRequest, StockItem, StockLocation, StockMovement, StockRecipe
from apps.inventory.services.inventory_service import create_stock_recipe, open_pos_order_demand, record_stock_movement
from apps.inventory.services.valuation_service import menu_item_cost_basis
from apps.portal.models import PortalSession
from apps.portal.services import hash_secret
from apps.pos.models import KitchenTicket, MenuCategory, MenuItem, PosOrder, PosOrderEvent
from apps.pos.services.order_service import bill_unbilled_order_charge
from apps.stays.models import Stay, StayRoom

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type, make_staff
from .portal_stay_helpers import make_in_house_stay


def portal_token(email, suffix="one"):
    raw = f"portal-order-{suffix}-{email}-abcdefghijklmnopqrstuvwxyz-0123456789"
    PortalSession.objects.create(email=email, session_hash=hash_secret(raw), expires_at=timezone.now() + timedelta(hours=2))
    return raw


def add_balance(item, location, amount, key, actor):
    record_stock_movement(item=item, location=location, movement_type=StockMovement.Type.OPENING,
                          quantity_delta=amount, source_key=key, actor=actor)


class GuestPortalRoomOrderTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # TransactionTestCase flushes migration-seeded grants under --keepdb.
        # Grant the exact capabilities this staff workflow exercises.
        for code in ("pos.order.manage", "guest_request.manage"):
            capability, _ = Capability.objects.get_or_create(
                code=code, defaults={"name": code, "category": "Tests", "is_active": True},
            )
            RoleCapability.objects.get_or_create(role=User.Role.WAITER, capability=capability)
        self.waiter = make_staff("portal.order.waiter@staff.dev", role=User.Role.WAITER)
        self.guest = make_guest("portal.order.guest@example.com")
        self.room_type = make_room_type("Portal order suite")
        self.room = make_room(self.room_type, "OP-101")
        self.booking = make_booking(self.guest, self.room_type, [self.room], check_in=hotel_today(),
                                    check_out=hotel_today() + timedelta(days=2), status=Booking.Status.CHECKED_IN)
        self.stay = make_in_house_stay(self.booking)
        self.token = portal_token(self.guest.email)
        self.kitchen = StockLocation.objects.create(code="PO-KITCHEN", name="Order Kitchen")
        self.bar = StockLocation.objects.create(code="PO-BAR", name="Order Bar")
        self.restaurant = MenuCategory.objects.create(name="Portal dishes", slug="portal-dishes", service_area="RESTAURANT")
        self.drinks = MenuCategory.objects.create(name="Portal drinks", slug="portal-drinks", service_area="BAR")
        self.rice_dish = MenuItem.objects.create(category=self.restaurant, name="Rice bowl", sku="PO-RICE-DISH", base_price="3500.00")
        self.drink = MenuItem.objects.create(category=self.drinks, name="Malt", sku="PO-MENU-MALT", base_price="800.00")
        self.rice = StockItem.objects.create(sku="PO-RICE", name="Rice", base_unit="KG", item_type="RECIPE_INGREDIENT")
        self.oil = StockItem.objects.create(sku="PO-OIL", name="Oil", base_unit="L", item_type="RECIPE_INGREDIENT")
        self.malt = StockItem.objects.create(sku="PO-MALT", name="Malt bottles", base_unit="BOTTLE",
                                             item_type="SELLABLE_PRODUCT", direct_sale_menu_item=self.drink,
                                             direct_sale_quantity="1", direct_sale_location=self.bar)
        create_stock_recipe(menu_item=self.rice_dish, location=self.kitchen, tracking_mode="COMPONENTS",
                            components=[{"item_id": self.rice.pk, "quantity_per_menu_unit": "250", "unit": "g"},
                                        {"item_id": self.oil.pk, "quantity_per_menu_unit": "100", "unit": "ml"}],
                            actor=self.waiter, idempotency_key="portal-rice-recipe")
        add_balance(self.rice, self.kitchen, "2", "portal-rice-opening", self.waiter)
        add_balance(self.oil, self.kitchen, "1", "portal-oil-opening", self.waiter)
        add_balance(self.malt, self.bar, "5", "portal-malt-opening", self.waiter)

    def line(self, item=None, quantity=1, price=None):
        item = item or self.drink
        return {"menu_item_id": item.pk, "quantity": quantity, "expected_unit_price": price or str(item.base_price)}

    def payload(self, lines=None, key="portal-order-key-01", room=None, **extra):
        return {"stay_reference": self.stay.reference, "room_id": (room or self.room).pk,
                "lines": lines or [self.line()], "payment_method": "FOLIO", "idempotency_key": key, **extra}

    def post_order(self, data=None, token=None):
        return self.client.post("/api/portal/requests/", data or self.payload(), format="json",
                                HTTP_X_PORTAL_SESSION=token or self.token)

    def assert_no_sale(self):
        self.assertEqual(ServiceRequest.objects.count(), 0)
        self.assertEqual(PosOrder.objects.count(), 0)
        self.assertFalse(FinancialTransaction.objects.filter(type=FinancialTransaction.Type.POS_CHARGE).exists())
        self.assertFalse(StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION).exists())

    def create_order(self, data=None, token=None):
        response = self.post_order(data, token)
        self.assertEqual(response.status_code, 201, response.content)
        return ServiceRequest.objects.get(reference=response.json()["data"]["reference"]), response

    def test_room_order_is_linked_and_visible_to_waiter_without_rekeying(self):
        req, response = self.create_order(self.payload(lines=[self.line(self.rice_dish, 2), self.line(self.drink, 2)], notes="No pepper"))
        data = response.json()["data"]
        order = req.pos_order
        self.assertEqual((req.guest_id, req.stay_id, req.room_id), (self.guest.pk, self.stay.pk, self.room.pk))
        self.assertEqual((order.mode, order.status), ("ROOM_SERVICE", "DRAFT"))
        self.assertEqual(order.folio.stay_id, self.stay.pk)
        self.assertEqual(order.delivery_location, "Room OP-101")
        self.assertEqual(order.total_amount, Decimal("8600.00"))
        self.assertEqual([(x.item_name, x.quantity) for x in order.lines.all()], [("Rice bowl", 2), ("Malt", 2)])
        self.assertEqual(data["pos_order"]["status"], "DRAFT")
        self.assertEqual(data["pos_order"]["payment_method"], "FOLIO")
        self.assertFalse(data["pos_order"]["charge_posted"])
        self.assertIsNone(order.charge_transaction_id)
        self.assertFalse(StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION).exists())
        self.assertTrue(ServiceRequestEvent.objects.filter(request=req, type="POS_ORDER_LINKED", guest_visible=True).exists())
        self.assertTrue(AuditLog.objects.filter(action="PORTAL_ROOM_SERVICE_ORDER_CREATED", object_id=str(req.pk)).exists())
        self.auth(self.waiter)
        listing = self.client.get("/api/admin/pos/orders/?mode=ROOM_SERVICE")
        self.assertEqual(listing.status_code, 200, listing.content)
        self.assertIn(order.reference, [x["reference"] for x in listing.json()["data"]])
        detail = self.client.get(f"/api/admin/pos/orders/{order.reference}/")
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(detail.json()["data"]["service_request_reference"], req.reference)
        self.assertEqual([x["item_name"] for x in detail.json()["data"]["lines"]], ["Rice bowl", "Malt"])
        queue = self.client.get("/api/admin/service-requests/")
        self.assertIn(req.reference, [x["reference"] for x in queue.json()["data"]])

    def test_delivery_posts_one_folio_charge_and_exact_converted_movements(self):
        req, _ = self.create_order(self.payload(lines=[self.line(self.rice_dish, 2), self.line(self.drink, 2)]))
        order = req.pos_order
        self.auth(self.waiter)
        submit = self.client.post(f"/api/admin/pos/orders/{order.reference}/submit/", {}, format="json")
        self.assertEqual(submit.status_code, 200, submit.content)
        req.refresh_from_db()
        self.assertEqual(req.status, "ACKNOWLEDGED")
        self.assertEqual(KitchenTicket.objects.filter(order=order).count(), 1)
        deliver = self.client.post(f"/api/admin/pos/orders/{order.reference}/status/", {"status": "DELIVERED"}, format="json")
        self.assertEqual(deliver.status_code, 200, deliver.content)
        repeat = self.client.post(f"/api/admin/pos/orders/{order.reference}/status/", {"status": "DELIVERED"}, format="json")
        self.assertEqual(repeat.status_code, 200, repeat.content)
        order.refresh_from_db(); req.refresh_from_db()
        self.assertEqual(req.status, "RESOLVED")
        self.assertIsNotNone(order.charge_transaction_id)
        self.assertEqual(FinancialTransaction.objects.filter(type="POS_CHARGE", source_reference=order.reference).count(), 1)
        self.assertEqual(FolioPosting.objects.filter(folio=order.folio, source_reference=order.reference, kind="CHARGE").count(), 1)
        self.assertEqual(PosOrderEvent.objects.filter(order=order, type="CHARGED").count(), 1)
        self.assertEqual(order.stock_consumption_request.status, StockConsumptionRequest.Status.PROCESSED)
        self.assertEqual(StockBalance.objects.get(item=self.rice, location=self.kitchen).quantity_on_hand, Decimal("1.5000"))
        self.assertEqual(StockBalance.objects.get(item=self.oil, location=self.kitchen).quantity_on_hand, Decimal("0.8000"))
        self.assertEqual(StockBalance.objects.get(item=self.malt, location=self.bar).quantity_on_hand, Decimal("3.0000"))
        self.assertEqual(StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION).count(), 3)
        self.unauth()
        shown = self.client.get(f"/api/portal/requests/{req.reference}/", HTTP_X_PORTAL_SESSION=self.token)
        self.assertEqual(shown.json()["data"]["pos_order"]["status"], "DELIVERED")
        self.assertTrue(shown.json()["data"]["pos_order"]["charge_posted"])

    def test_same_key_replay_is_idempotent_and_changed_payload_is_rejected(self):
        payload = self.payload()
        req, _ = self.create_order(payload)
        replay = self.post_order(payload)
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(replay.json()["data"]["reference"], req.reference)
        changed = self.post_order(self.payload(lines=[self.line(quantity=2)]))
        self.assertEqual(changed.status_code, 400, changed.content)
        self.assertEqual(PosOrder.objects.count(), 1)
        self.assertEqual(ServiceRequest.objects.count(), 1)
        self.assertEqual(ServiceRequestEvent.objects.filter(request=req, type="POS_ORDER_LINKED").count(), 1)

    def test_identical_active_order_with_new_key_is_rejected_not_duplicated(self):
        req, _ = self.create_order()
        duplicate = self.post_order(self.payload(key="different-key"))
        self.assertEqual(duplicate.status_code, 400, duplicate.content)
        self.assertIn("identical", str(duplicate.content).lower())
        self.assertEqual(PosOrder.objects.count(), 1)
        cancelled = self.client.post(f"/api/portal/requests/{req.reference}/cancel/", {}, format="json", HTTP_X_PORTAL_SESSION=self.token)
        self.assertEqual(cancelled.status_code, 200, cancelled.content)
        again = self.post_order(self.payload(key="different-key"))
        self.assertEqual(again.status_code, 201, again.content)
        self.assertEqual(PosOrder.objects.count(), 2)  # historical cancelled order remains

    def test_direct_sale_quantity_checked_at_configured_location_with_no_charge_or_partial_order(self):
        add_balance(self.malt, self.kitchen, "100", "wrong-location", self.waiter)
        response = self.post_order(self.payload(lines=[self.line(quantity=6)]))
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["code"], "POS_STOCK_CONFLICT")
        self.assertIn("Malt", response.json()["message"])
        self.assertNotIn("PO-MALT", str(response.content))
        self.assert_no_sale()
        self.assertEqual(StockBalance.objects.get(item=self.malt, location=self.bar).quantity_on_hand, Decimal("5"))

    def test_recipe_shortage_fails_all_lines_and_identifies_dish(self):
        response = self.post_order(self.payload(lines=[self.line(self.rice_dish, 9), self.line(self.drink, 1)]))
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn("Rice bowl", response.json()["message"])
        self.assertNotIn("PO-RICE", str(response.content))
        self.assert_no_sale()
        self.assertEqual(StockBalance.objects.get(item=self.rice, location=self.kitchen).quantity_on_hand, Decimal("2"))

    def test_ingredient_shortage_even_when_other_component_is_sufficient(self):
        # The oil supports 10 portions but rice supports only 8. Make rice ample
        # and request 11 so oil becomes the limiting converted component.
        add_balance(self.rice, self.kitchen, "5", "rice-extra", self.waiter)
        response = self.post_order(self.payload(lines=[self.line(self.rice_dish, 11)]))
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn("Rice bowl", response.json()["message"])
        self.assert_no_sale()

    def test_missing_mapping_sku_or_name_match_does_not_allow_order(self):
        unconfigured = MenuItem.objects.create(category=self.drinks, name=self.malt.name, sku=self.malt.sku, base_price="900.00")
        result = self.post_order(self.payload(lines=[self.line(unconfigured)]))
        self.assertEqual(result.status_code, 409, result.content)
        self.assertIn(unconfigured.name, result.json()["message"])
        self.assert_no_sale()
        self.assertEqual(StockItem.objects.filter(sku=self.malt.sku).count(), 1)

    def test_ambiguous_active_recipes_are_unavailable_in_catalog_and_at_order(self):
        # Imported data can bypass the recipe service's one-active-version rule.
        StockRecipe.objects.create(reference="AMBIGUOUS-RECIPE", menu_item=self.rice_dish,
                                   location=self.kitchen, version=999, is_active=True)
        response = self.client.get("/api/portal/catalog/menu/", HTTP_X_PORTAL_SESSION=self.token)
        menu = {x["id"]: x for cat in response.json()["data"]["categories"] for x in cat["items"]}
        self.assertFalse(menu[self.rice_dish.pk]["is_available"])
        result = self.post_order(self.payload(lines=[self.line(self.rice_dish)]))
        self.assertEqual(result.status_code, 409, result.content)
        self.assert_no_sale()

    def test_recipe_and_direct_sale_conflict_is_not_guessed(self):
        create_stock_recipe(menu_item=self.drink, location=self.bar, tracking_mode="COMPONENTS",
                            components=[{"item_id": self.malt.pk, "quantity_per_menu_unit": "1"}],
                            actor=self.waiter, idempotency_key="conflicting-direct-recipe")
        result = self.post_order()
        self.assertEqual(result.status_code, 409, result.content)
        self.assert_no_sale()

    def test_explicit_no_stock_food_can_be_ordered_without_ingredients(self):
        dish = MenuItem.objects.create(category=self.restaurant, name="Untracked meal", sku="PO-NOSTOCK", base_price="500.00")
        create_stock_recipe(menu_item=dish, location=self.kitchen, tracking_mode="NO_STOCK", components=[],
                            actor=self.waiter, idempotency_key="portal-no-stock-recipe")
        result = self.post_order(self.payload(lines=[self.line(dish)]))
        self.assertEqual(result.status_code, 201, result.content)
        self.assertEqual(PosOrder.objects.get().status, PosOrder.Status.DRAFT)
        self.assertFalse(StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION).exists())

    def test_price_change_and_guest_supplied_financial_fields_are_not_trusted(self):
        bad_price = self.post_order(self.payload(lines=[self.line(price="0.01")]))
        self.assertEqual(bad_price.status_code, 400, bad_price.content)
        self.assertIn("price", str(bad_price.content).lower())
        for field, value in [("total_amount", "0.01"), ("guest_id", 999), ("payment_status", "PAID"), ("stay_id", 999)]:
            with self.subTest(field=field):
                result = self.post_order(self.payload(**{field: value}))
                self.assertEqual(result.status_code, 400, result.content)
        modifier = self.line(); modifier["modifier_ids"] = [1]
        self.assertEqual(self.post_order(self.payload(lines=[modifier])).status_code, 400)
        self.assert_no_sale()

    def test_bad_quantity_disabled_item_and_missing_key_are_rejected(self):
        for quantity in (0, -1, 101):
            response = self.post_order(self.payload(lines=[self.line(quantity=quantity)]))
            self.assertEqual(response.status_code, 400, response.content)
        payload = self.payload(); payload.pop("idempotency_key")
        self.assertEqual(self.post_order(payload).status_code, 400)
        self.drink.is_available = False; self.drink.save(update_fields=["is_available"])
        self.assertEqual(self.post_order().status_code, 400)
        self.assert_no_sale()

    def test_menu_exposes_accurate_non_reserving_quantity_and_hides_unconfigured(self):
        response = self.client.get("/api/portal/catalog/menu/", HTTP_X_PORTAL_SESSION=self.token)
        self.assertEqual(response.status_code, 200, response.content)
        items = {x["id"]: x for cat in response.json()["data"]["categories"] for x in cat["items"]}
        self.assertEqual(items[self.rice_dish.pk]["available_quantity"], 8)
        self.assertEqual(items[self.drink.pk]["available_quantity"], 5)
        self.assertTrue(items[self.drink.pk]["is_available"])
        self.create_order(self.payload(lines=[self.line(quantity=3)]))
        again = self.client.get("/api/portal/catalog/menu/", HTTP_X_PORTAL_SESSION=self.token)
        items = {x["id"]: x for cat in again.json()["data"]["categories"] for x in cat["items"]}
        self.assertEqual(items[self.drink.pk]["available_quantity"], 2)
        self.assertNotIn("PO-MALT", str(again.content))

    def test_guest_cancellation_cancels_draft_and_releases_commitment(self):
        req, _ = self.create_order(self.payload(lines=[self.line(quantity=5)]))
        self.assertEqual(open_pos_order_demand({(self.malt.pk, self.bar.pk)})[(self.malt.pk, self.bar.pk)], Decimal("5"))
        cancelled = self.client.post(f"/api/portal/requests/{req.reference}/cancel/", {"note": "Changed mind"}, format="json", HTTP_X_PORTAL_SESSION=self.token)
        self.assertEqual(cancelled.status_code, 200, cancelled.content)
        req.refresh_from_db(); order = req.pos_order; order.refresh_from_db()
        self.assertEqual((req.status, order.status), ("CANCELLED", "CANCELLED"))
        self.assertEqual(open_pos_order_demand({(self.malt.pk, self.bar.pk)}), {})
        self.assertFalse(order.charge_transaction_id)
        self.assertEqual(PosOrderEvent.objects.filter(order=order, type="CANCELLED").count(), 1)
        self.assertEqual(ServiceRequestEvent.objects.filter(request=req, type="CANCELLED").count(), 1)

    def test_guest_can_cancel_submitted_but_not_preparing_order(self):
        req, _ = self.create_order()
        order = req.pos_order
        self.auth(self.waiter)
        self.assertEqual(self.client.post(f"/api/admin/pos/orders/{order.reference}/submit/", {}, format="json").status_code, 200)
        self.unauth()
        cancel = self.client.post(f"/api/portal/requests/{req.reference}/cancel/", {}, format="json", HTTP_X_PORTAL_SESSION=self.token)
        self.assertEqual(cancel.status_code, 200, cancel.content)
        order.refresh_from_db(); self.assertEqual(order.status, "CANCELLED")
        self.assertEqual(order.kitchen_ticket.status, "CANCELLED")

        req2, _ = self.create_order(self.payload(key="new-after-cancel"))
        order2 = req2.pos_order
        self.auth(self.waiter)
        self.client.post(f"/api/admin/pos/orders/{order2.reference}/submit/", {}, format="json")
        self.client.post(f"/api/admin/pos/orders/{order2.reference}/status/", {"status": "PREPARING"}, format="json")
        self.unauth()
        refused = self.client.post(f"/api/portal/requests/{req2.reference}/cancel/", {}, format="json", HTTP_X_PORTAL_SESSION=self.token)
        self.assertEqual(refused.status_code, 400, refused.content)
        order2.refresh_from_db(); self.assertEqual(order2.status, "PREPARING")

    def test_staff_pos_cancellation_synchronizes_request(self):
        req, _ = self.create_order(); order = req.pos_order
        self.auth(self.waiter)
        cancelled = self.client.post(f"/api/admin/pos/orders/{order.reference}/status/", {"status": "CANCELLED"}, format="json")
        self.assertEqual(cancelled.status_code, 200, cancelled.content)
        req.refresh_from_db(); self.assertEqual(req.status, "CANCELLED")
        self.assertFalse(order.charge_transaction_id)

    def test_checkout_billing_cannot_charge_undelivered_guest_order(self):
        from django.core.exceptions import ValidationError
        req, _ = self.create_order(); order = req.pos_order
        self.auth(self.waiter)
        self.assertEqual(self.client.post(f"/api/admin/pos/orders/{order.reference}/submit/", {}, format="json").status_code, 200)
        with self.assertRaises(ValidationError):
            bill_unbilled_order_charge(order=order, actor=self.waiter)
        order.refresh_from_db(); self.assertIsNone(order.charge_transaction_id)
        self.assertFalse(FinancialTransaction.objects.filter(type="POS_CHARGE").exists())

    def test_pending_order_does_not_inflate_or_block_other_settlement(self):
        from apps.payments.models import Payment

        req, _ = self.create_order()
        order = req.pos_order
        self.auth(self.waiter)
        submitted = self.client.post(f"/api/admin/pos/orders/{order.reference}/submit/", {}, format="json")
        self.assertEqual(submitted.status_code, 200, submitted.content)

        # The pending request blocks clearance but is neither a debt nor a
        # BILL_AND_SETTLE action until delivery. Other real debts remain payable.
        admin = make_staff("portal-order-admin@staff.dev", role=User.Role.ADMIN)
        self.auth(admin)
        url = f"/api/admin/bookings/{self.booking.booking_reference}"
        clearance = self.client.get(f"{url}/folio-clearance/")
        self.assertEqual(clearance.status_code, 200, clearance.content)
        before = clearance.json()["data"]
        self.assertEqual(before["amount_to_settle"], "15000.00")
        self.assertFalse(before["unpaid_requests"])
        self.assertEqual(len(before["open_requests"]), 1)
        self.assertFalse(before["gate_pass_allowed"])
        settled = self.client.post(f"{url}/settle-balance/", {
            "amount": "5000.00", "provider": "BANK_TRANSFER",
        }, format="json")
        self.assertEqual(settled.status_code, 201, settled.content)
        self.assertEqual(Payment.objects.filter(booking=self.booking).count(), 1)
        self.assertEqual(settled.json()["data"]["settlement"]["billed_requests"], [])
        self.assertEqual(settled.json()["data"]["folio"]["amount_to_settle"], "10000.00")
        self.assertFalse(settled.json()["data"]["folio"]["gate_pass_allowed"])
        order.refresh_from_db()
        self.assertIsNone(order.charge_transaction_id)
        self.assertEqual(order.status, PosOrder.Status.SUBMITTED)

    def test_stock_removed_after_draft_blocks_submit_and_delivery_without_charge(self):
        req, _ = self.create_order(self.payload(lines=[self.line(quantity=4)])); order = req.pos_order
        record_stock_movement(item=self.malt, location=self.bar, movement_type="ISSUE", quantity_delta="-3",
                              source_key="unexpected-depletion", actor=self.waiter)
        self.auth(self.waiter)
        response = self.client.post(f"/api/admin/pos/orders/{order.reference}/submit/", {}, format="json")
        self.assertEqual(response.status_code, 409, response.content)
        order.refresh_from_db(); self.assertEqual(order.status, "DRAFT")
        self.assertIsNone(order.charge_transaction_id)
        self.assertFalse(StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION).exists())

    def test_depletion_after_submission_blocks_delivery_atomically(self):
        req, _ = self.create_order(self.payload(lines=[self.line(quantity=4)])); order = req.pos_order
        self.auth(self.waiter)
        self.assertEqual(self.client.post(f"/api/admin/pos/orders/{order.reference}/submit/", {}, format="json").status_code, 200)
        record_stock_movement(item=self.malt, location=self.bar, movement_type="ISSUE", quantity_delta="-3",
                              source_key="depletion-after-submit", actor=self.waiter)
        response = self.client.post(f"/api/admin/pos/orders/{order.reference}/status/", {"status": "DELIVERED"}, format="json")
        self.assertEqual(response.status_code, 409, response.content)
        order.refresh_from_db(); req.refresh_from_db()
        self.assertEqual(order.status, "SUBMITTED")
        self.assertEqual(req.status, "ACKNOWLEDGED")
        self.assertIsNone(order.charge_transaction_id)
        self.assertFalse(FinancialTransaction.objects.filter(type="POS_CHARGE").exists())
        self.assertFalse(StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION).exists())
        self.assertEqual(StockBalance.objects.get(item=self.malt, location=self.bar).quantity_on_hand, Decimal("2"))

    def test_legacy_pos_link_is_not_misrepresented_as_a_portal_folio_order(self):
        from apps.guest_services.serializers import PortalServiceRequestListSerializer

        req, _ = self.create_order()
        order = req.pos_order
        order.metadata = {}  # A legacy/non-portal POS link lacks our marker.
        order.save(update_fields=["metadata"])
        req.refresh_from_db()
        self.assertIsNone(PortalServiceRequestListSerializer(req).data["pos_order"])

    def test_missing_menu_fk_in_open_order_does_not_silently_drop_demand(self):
        from django.core.exceptions import ValidationError
        from apps.pos.models import PosOrderLine

        req, _ = self.create_order()
        order = req.pos_order
        # Simulate an imported/deleted menu whose historical line has SET_NULL.
        PosOrderLine.objects.filter(order=order).update(menu_item=None)
        with self.assertRaisesRegex(ValidationError, "invalid stock evidence"):
            open_pos_order_demand({(self.malt.pk, self.bar.pk)})
        self.auth(self.waiter)
        response = self.client.post(f"/api/admin/pos/orders/{order.reference}/submit/", {}, format="json")
        self.assertEqual(response.status_code, 409, response.content)
        order.refresh_from_db()
        self.assertEqual(order.status, PosOrder.Status.DRAFT)
        self.assertIsNone(order.charge_transaction_id)

    def test_recipe_change_preserves_old_commitment_and_blocks_delivery(self):
        req, _ = self.create_order(self.payload(lines=[self.line(self.rice_dish, 4)])); order = req.pos_order
        before = open_pos_order_demand({(self.rice.pk, self.kitchen.pk)})
        self.assertEqual(before[(self.rice.pk, self.kitchen.pk)], Decimal("1.0000"))
        create_stock_recipe(menu_item=self.rice_dish, location=self.kitchen, tracking_mode="COMPONENTS",
                            components=[{"item_id": self.rice.pk, "quantity_per_menu_unit": "0.5"}],
                            actor=self.waiter, idempotency_key="replacement-recipe")
        after = open_pos_order_demand({(self.rice.pk, self.kitchen.pk)})
        self.assertEqual(after[(self.rice.pk, self.kitchen.pk)], Decimal("1.0000"))
        self.auth(self.waiter)
        response = self.client.post(f"/api/admin/pos/orders/{order.reference}/submit/", {}, format="json")
        self.assertEqual(response.status_code, 409, response.content)
        order.refresh_from_db(); self.assertEqual(order.status, "DRAFT")
        self.assertIsNone(order.charge_transaction_id)

    def test_cross_guest_room_and_checkout_are_denied(self):
        other_guest = make_guest("other.order@example.com")
        other_room = make_room(self.room_type, "OP-102")
        other_booking = make_booking(other_guest, self.room_type, [other_room], check_in=hotel_today(),
                                     check_out=hotel_today() + timedelta(days=2), status=Booking.Status.CHECKED_IN)
        other_stay = make_in_house_stay(other_booking)
        other_token = portal_token(other_guest.email, "other")
        cross_stay = self.post_order(token=other_token)
        self.assertEqual(cross_stay.status_code, 404, cross_stay.content)
        wrong_room = self.post_order(self.payload(room=other_room))
        self.assertEqual(wrong_room.status_code, 400, wrong_room.content)
        unauth = self.client.post("/api/portal/requests/", self.payload(), format="json")
        self.assertEqual(unauth.status_code, 401, unauth.content)
        # Keep a different active stay for the same email so authentication
        # remains valid, but the checked-out stay must still be denied.
        extra_room = make_room(self.room_type, "OP-103")
        extra_booking = make_booking(self.guest, self.room_type, [extra_room], check_in=hotel_today(),
                                     check_out=hotel_today() + timedelta(days=2), status=Booking.Status.CHECKED_IN)
        make_in_house_stay(extra_booking)
        self.booking.status = Booking.Status.CHECKED_OUT; self.booking.save(update_fields=["status"])
        self.stay.status = Stay.Status.CHECKED_OUT; self.stay.save(update_fields=["status"])
        StayRoom.objects.filter(stay=self.stay).update(released_at=timezone.now())
        refused = self.post_order()
        self.assertEqual(refused.status_code, 404, refused.content)
        self.assert_no_sale()
        self.assertNotEqual(other_stay.pk, self.stay.pk)

    def test_order_cannot_progress_after_checkout_but_staff_can_cancel_it(self):
        from apps.bookings.services.booking_service import check_out_booking

        req, _ = self.create_order()
        order = req.pos_order
        check_out_booking(self.booking, staff_user=self.waiter, allow_balance_due=True)
        self.auth(self.waiter)
        blocked = self.client.post(f"/api/admin/pos/orders/{order.reference}/submit/", {}, format="json")
        self.assertEqual(blocked.status_code, 400, blocked.content)
        cancelled = self.client.post(f"/api/admin/pos/orders/{order.reference}/status/", {"status": "CANCELLED"}, format="json")
        self.assertEqual(cancelled.status_code, 200, cancelled.content)
        order.refresh_from_db(); req.refresh_from_db()
        self.assertEqual(order.status, PosOrder.Status.CANCELLED)
        self.assertEqual(req.status, ServiceRequest.Status.CANCELLED)
        self.assertIsNone(order.charge_transaction_id)
        self.assertFalse(open_pos_order_demand({(self.malt.pk, self.bar.pk)}))

    def test_occupant_can_order_and_cancel_only_their_active_room(self):
        occupant_email = "occupant.order@example.com"
        RoomOccupant.objects.create(booking_room=self.stay.stay_rooms.get().booking_room, email=occupant_email, full_name="Room Guest")
        token = portal_token(occupant_email, "occupant")
        req, _ = self.create_order(self.payload(key="occupant-order"), token)
        self.assertEqual(req.occupant.email, occupant_email)
        cancel = self.client.post(f"/api/portal/requests/{req.reference}/cancel/", {}, format="json", HTTP_X_PORTAL_SESSION=token)
        self.assertEqual(cancel.status_code, 200, cancel.content)
        req.refresh_from_db(); self.assertEqual(req.status, "CANCELLED")

    def test_atomic_rollback_and_safe_unexpected_error(self):
        with mock.patch("apps.guest_services.services.portal_order_service.create_order", side_effect=RuntimeError("PRIVATE_INTERNAL_FAILURE")):
            response = self.post_order()
        self.assertEqual(response.status_code, 500, response.content)
        self.assertNotIn("PRIVATE_INTERNAL_FAILURE", str(response.content))
        self.assert_no_sale()
        retry = self.post_order()
        self.assertEqual(retry.status_code, 201, retry.content)

    def test_unsafe_isolation_fails_closed(self):
        from django.core.exceptions import ValidationError
        with mock.patch("apps.guest_services.services.portal_order_service._require_safe_isolation",
                        side_effect=ValidationError("Ordering is temporarily unavailable. Please contact the hotel.")):
            response = self.post_order()
        self.assertEqual(response.status_code, 400, response.content)
        self.assert_no_sale()

    def test_cost_guard_prefers_direct_link_and_avoids_ambiguous_names(self):
        self.malt.standard_cost = Decimal("100.00"); self.malt.direct_sale_quantity = Decimal("2")
        self.malt.save(update_fields=["standard_cost", "direct_sale_quantity"])
        self.assertEqual(menu_item_cost_basis(self.drink)["cost"], Decimal("200.00"))
        StockItem.objects.create(sku="PO-DUPLICATE-A", name="Duplicate name", standard_cost="200")
        StockItem.objects.create(sku="PO-DUPLICATE-B", name="Duplicate name", standard_cost="500")
        probe = MenuItem(name="Duplicate name", sku="PO-NO-MATCH", base_price="1")
        self.assertIsNone(menu_item_cost_basis(probe))
        # An inactive explicit mapping must not fall back to a lookalike SKU.
        self.malt.is_active = False; self.malt.save(update_fields=["is_active"])
        StockItem.objects.create(sku=self.drink.sku, name="Lookalike", standard_cost="1")
        self.assertIsNone(menu_item_cost_basis(self.drink))


    def test_batch05_recipe_free_food_catalog_order_and_delivery(self):
        from apps.pos.services.order_service import submit_order, transition_order
        dish = MenuItem.objects.create(category=self.restaurant, name="Pepper soup", sku="B05-PORTAL-SOUP", base_price="3000")
        result = self.client.get("/api/portal/catalog/menu/", HTTP_X_PORTAL_SESSION=self.token)
        menu = {x["id"]: x for cat in result.json()["data"]["categories"] for x in cat["items"]}
        self.assertTrue(menu[dish.pk]["is_available"])
        self.assertFalse(menu[dish.pk]["quantity_limited"])
        req, _ = self.create_order(self.payload(lines=[self.line(dish)]))
        order = req.pos_order
        submit_order(order=order, actor=self.waiter)
        dish.is_available = False
        dish.save()
        transition_order(order=order, actor=self.waiter, target_status="DELIVERED")
        self.assertEqual(StockConsumptionRequest.objects.get(pos_order=order).status, "SKIPPED")
        self.assertFalse(StockRecipe.objects.filter(menu_item=dish).exists())
        self.assertFalse(StockMovement.objects.filter(type="POS_CONSUMPTION").exists())

    def test_batch05_portions_published_and_room_order_backend_limits(self):
        dish = MenuItem.objects.create(category=self.restaurant, name="Prepared rice", sku="B05-PORTAL-RICE", base_price="3000", portion_tracking=True, prepared_portions=2)
        result = self.client.get("/api/portal/catalog/menu/", HTTP_X_PORTAL_SESSION=self.token)
        menu = {x["id"]: x for cat in result.json()["data"]["categories"] for x in cat["items"]}
        self.assertEqual(menu[dish.pk]["available_quantity"], 2)
        self.assertTrue(menu[dish.pk]["portion_tracking"])
        rejected = self.post_order(self.payload(lines=[self.line(dish, 3)]))
        self.assertEqual(rejected.status_code, 400, rejected.content)
        self.assert_no_sale()
        req, _ = self.create_order(self.payload(lines=[self.line(dish, 2)]))
        from apps.pos.services.order_service import submit_order
        submit_order(order=req.pos_order, actor=self.waiter)
        dish.refresh_from_db()
        self.assertEqual(dish.prepared_portions, 0)
        rejected = self.post_order(self.payload(lines=[self.line(dish)], key="b05-new-draft"))
        self.assertEqual(rejected.status_code, 400, rejected.content)

    def test_batch05_guest_cancel_releases_submitted_portions(self):
        from apps.pos.services.order_service import submit_order
        from apps.guest_services.services.request_service import cancel_service_request_by_guest
        dish = MenuItem.objects.create(category=self.restaurant, name="White rice", sku="B05-PORTAL-WHITE", base_price="1000", portion_tracking=True, prepared_portions=5)
        req, _ = self.create_order(self.payload(lines=[self.line(dish, 2)]))
        submit_order(order=req.pos_order, actor=self.waiter)
        cancel_service_request_by_guest(service_request=req, portal_email=self.guest.email)
        cancel_service_request_by_guest(service_request=req, portal_email=self.guest.email)
        dish.refresh_from_db()
        self.assertEqual(dish.prepared_portions, 5)
        self.assertEqual(PosOrder.objects.get(pk=req.pos_order.pk).status, "CANCELLED")

    def test_batch05_mixed_recipe_and_simple_food_delivers(self):
        from apps.pos.services.order_service import submit_order, transition_order
        dish = MenuItem.objects.create(category=self.restaurant, name="Prepared soup", sku="B05-PORTAL-MIX", base_price="1000")
        req, _ = self.create_order(self.payload(lines=[self.line(dish), self.line(self.rice_dish)]))
        submit_order(order=req.pos_order, actor=self.waiter)
        transition_order(order=req.pos_order, actor=self.waiter, target_status="DELIVERED")
        self.assertEqual(StockBalance.objects.get(item=self.rice, location=self.kitchen).quantity_on_hand, Decimal("1.75"))
        self.assertEqual(StockConsumptionRequest.objects.get(pos_order=req.pos_order).status, "PROCESSED")

    def test_batch05_not_available_cannot_enter_guest_draft(self):
        dish = MenuItem.objects.create(category=self.restaurant, name="Finished soup", sku="B05-PORTAL-OFF", base_price="1000", is_available=False)
        result = self.post_order(self.payload(lines=[self.line(dish)]))
        self.assertEqual(result.status_code, 400, result.content)
        self.assert_no_sale()


class ConcurrentPortalOrderTests(TransactionTestCase):
    """Real independent connections compete for the same physical stock."""
    reset_sequences = True

    def setUp(self):
        cache.clear()
        self.staff = make_staff("concurrent.order@staff.dev", role=User.Role.WAITER)
        self.location = StockLocation.objects.create(code="CO-STORE", name="Concurrent Store")
        self.category = MenuCategory.objects.create(name="Concurrent menu", slug="concurrent-menu", service_area="BAR")
        self.a = MenuItem.objects.create(category=self.category, name="Bottle A", sku="CO-A", base_price="100.00")
        self.b = MenuItem.objects.create(category=self.category, name="Bottle B", sku="CO-B", base_price="100.00")
        self.ingredient = StockItem.objects.create(sku="CO-SHARED", name="Shared bottles", base_unit="BOTTLE")
        for i, item in enumerate((self.a, self.b)):
            create_stock_recipe(menu_item=item, location=self.location, tracking_mode="COMPONENTS",
                                components=[{"item_id": self.ingredient.pk, "quantity_per_menu_unit": "1"}],
                                actor=self.staff, idempotency_key=f"co-recipe-{i}")
        add_balance(self.ingredient, self.location, "5", "co-opening", self.staff)
        room_type = make_room_type("Concurrent order rooms")
        self.contexts = []
        for i in range(2):
            guest = make_guest(f"concurrent-{i}@example.com")
            room = make_room(room_type, f"CO-{i}")
            booking = make_booking(guest, room_type, [room], check_in=hotel_today(),
                                   check_out=hotel_today() + timedelta(days=2), status=Booking.Status.CHECKED_IN)
            stay = make_in_house_stay(booking)
            token = portal_token(guest.email, f"concurrent-{i}")
            self.contexts.append((stay.reference, room.pk, token))

    def tearDown(self):
        cache.clear()
        super().tearDown()

    def test_two_guests_cannot_oversell_shared_ingredient(self):
        gate = Barrier(2)
        def worker(index):
            close_old_connections()
            try:
                stay, room, token = self.contexts[index]
                item = self.a if index == 0 else self.b
                payload = {"stay_reference": stay, "room_id": room, "idempotency_key": f"co-order-{index}",
                           "lines": [{"menu_item_id": item.pk, "quantity": 3, "expected_unit_price": "100.00"}]}
                client = APIClient()
                gate.wait(timeout=10)
                result = client.post("/api/portal/requests/", payload, format="json", HTTP_X_PORTAL_SESSION=token)
                return result.status_code
            except OperationalError:
                return 503  # SQLite shared DB may fail closed on a write lock
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(worker, range(2)))
        self.assertEqual(statuses.count(201), 1, statuses)
        if connection.vendor == "mysql":
            self.assertEqual(sorted(statuses), [201, 409], statuses)
        else:
            self.assertIn(next(x for x in statuses if x != 201), (409, 500, 503), statuses)
        self.assertEqual(PosOrder.objects.count(), 1)
        self.assertEqual(ServiceRequest.objects.count(), 1)
        self.assertEqual(open_pos_order_demand({(self.ingredient.pk, self.location.pk)})[(self.ingredient.pk, self.location.pk)], Decimal("3"))
        self.assertFalse(FinancialTransaction.objects.filter(type="POS_CHARGE").exists())
        self.assertEqual(StockBalance.objects.get(item=self.ingredient, location=self.location).quantity_on_hand, Decimal("5"))

    def test_guest_order_and_staff_pos_order_cannot_oversell_shared_stock(self):
        if connection.vendor != "mysql":
            self.skipTest("Cross-channel row-lock concurrency is exercised on MariaDB/MySQL.")
        from apps.core.exceptions import PosStockConflictError
        from apps.pos.services.order_service import create_order

        gate = Barrier(2)
        stay, room, token = self.contexts[0]
        payload = {"stay_reference": stay, "room_id": room, "idempotency_key": "cross-channel-race",
                   "lines": [{"menu_item_id": self.a.pk, "quantity": 3, "expected_unit_price": "100.00"}]}
        def worker(channel):
            close_old_connections()
            try:
                gate.wait(timeout=10)
                if channel == "portal":
                    result = APIClient().post("/api/portal/requests/", payload, format="json", HTTP_X_PORTAL_SESSION=token)
                    return result.status_code
                try:
                    create_order(mode=PosOrder.Mode.BAR, actor=self.staff, table_number="RACE-1",
                                 lines=[{"menu_item_id": self.b.pk, "quantity": 3}])
                    return 201
                except PosStockConflictError:
                    return 409
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(worker, ("portal", "staff")))
        self.assertEqual(sorted(statuses), [201, 409], statuses)
        self.assertEqual(PosOrder.objects.count(), 1)
        demand = open_pos_order_demand({(self.ingredient.pk, self.location.pk)})
        self.assertEqual(demand[(self.ingredient.pk, self.location.pk)], Decimal("3"))
        self.assertFalse(FinancialTransaction.objects.filter(type="POS_CHARGE").exists())
        self.assertEqual(StockBalance.objects.get(item=self.ingredient, location=self.location).quantity_on_hand, Decimal("5"))

    def test_repeatable_read_session_refuses_portal_orders(self):
        if connection.vendor != "mysql":
            self.skipTest("MySQL/MariaDB session isolation is exercised on its real database.")
        stay, room, token = self.contexts[0]
        payload = {"stay_reference": stay, "room_id": room, "idempotency_key": "unsafe-isolation",
                   "lines": [{"menu_item_id": self.a.pk, "quantity": 1, "expected_unit_price": "100.00"}]}
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            response = APIClient().post("/api/portal/requests/", payload, format="json", HTTP_X_PORTAL_SESSION=token)
            self.assertEqual(response.status_code, 400, response.content)
            self.assertIn("temporarily unavailable", str(response.content).lower())
            self.assertFalse(PosOrder.objects.exists())
            self.assertFalse(ServiceRequest.objects.exists())
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")

    def test_concurrent_same_key_creates_one_linked_order(self):
        if connection.vendor != "mysql":
            self.skipTest("Independent row-lock concurrency is exercised on MariaDB/MySQL.")
        gate = Barrier(2)
        stay, room, token = self.contexts[0]
        payload = {"stay_reference": stay, "room_id": room, "idempotency_key": "same-concurrent-key",
                   "lines": [{"menu_item_id": self.a.pk, "quantity": 2, "expected_unit_price": "100.00"}]}
        def worker(_):
            close_old_connections()
            try:
                client = APIClient(); gate.wait(timeout=10)
                result = client.post("/api/portal/requests/", payload, format="json", HTTP_X_PORTAL_SESSION=token)
                return result.status_code, result.json().get("data", {}).get("reference")
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, range(2)))
        self.assertEqual(sorted(status for status, _ in results), [200, 201], results)
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(ServiceRequest.objects.count(), 1)
        self.assertEqual(PosOrder.objects.count(), 1)
        self.assertEqual(ServiceRequestEvent.objects.filter(type="POS_ORDER_LINKED").count(), 1)

    def test_checkout_holds_booking_lock_before_rooms_and_rejects_racing_order(self):
        if connection.vendor != "mysql":
            self.skipTest("Independent row-lock concurrency is exercised on MariaDB/MySQL.")
        from apps.bookings.services import booking_service
        booking_id = Stay.objects.get(reference=self.contexts[0][0]).booking_id
        locked, release, started = Event(), Event(), Event()
        original_release = booking_service._release_rooms_for_checkout
        def paused_release(booking):
            locked.set()
            if not release.wait(15):
                raise AssertionError("Checkout synchronization timed out")
            return original_release(booking)
        def checkout():
            close_old_connections()
            try:
                booking = Booking.objects.get(pk=booking_id)
                return booking_service.check_out_booking(booking, staff_user=self.staff, allow_balance_due=True).status
            finally:
                close_old_connections()
        def order():
            close_old_connections()
            try:
                stay, room, token = self.contexts[0]
                data = {"stay_reference": stay, "room_id": room, "idempotency_key": "checkout-race",
                        "lines": [{"menu_item_id": self.a.pk, "quantity": 1, "expected_unit_price": "100.00"}]}
                started.set()
                return APIClient().post("/api/portal/requests/", data, format="json", HTTP_X_PORTAL_SESSION=token).status_code
            finally:
                close_old_connections()
        with mock.patch.object(booking_service, "_release_rooms_for_checkout", side_effect=paused_release):
            with ThreadPoolExecutor(max_workers=2) as pool:
                checkout_future = pool.submit(checkout)
                self.assertTrue(locked.wait(10), "Checkout did not acquire booking lock")
                order_future = pool.submit(order)
                self.assertTrue(started.wait(10), "Order did not start")
                release.set()
                checkout_status, order_status = checkout_future.result(timeout=30), order_future.result(timeout=30)
        self.assertEqual(checkout_status, Booking.Status.CHECKED_OUT)
        self.assertIn(order_status, (401, 404), order_status)
        self.assertFalse(PosOrder.objects.exists())
        self.assertFalse(ServiceRequest.objects.exists())
        self.assertFalse(FinancialTransaction.objects.filter(type="POS_CHARGE").exists())

    def test_cancel_and_delivery_race_never_both_win(self):
        if connection.vendor != "mysql":
            self.skipTest("Independent row-lock concurrency is exercised on MariaDB/MySQL.")
        from django.core.exceptions import ValidationError
        from apps.guest_services.services.request_service import cancel_service_request_by_guest
        from apps.pos.services.order_service import submit_order, transition_order
        stay, room, token = self.contexts[0]
        data = {"stay_reference": stay, "room_id": room, "idempotency_key": "cancel-race",
                "lines": [{"menu_item_id": self.a.pk, "quantity": 2, "expected_unit_price": "100.00"}]}
        response = APIClient().post("/api/portal/requests/", data, format="json", HTTP_X_PORTAL_SESSION=token)
        self.assertEqual(response.status_code, 201, response.content)
        req = ServiceRequest.objects.get(reference=response.json()["data"]["reference"])
        order = req.pos_order
        submit_order(order=order, actor=self.staff)
        gate = Barrier(2)
        def worker(action):
            close_old_connections()
            try:
                gate.wait(timeout=10)
                if action == "deliver":
                    transition_order(order=order, target_status="DELIVERED", actor=self.staff)
                else:
                    cancel_service_request_by_guest(service_request=req, portal_email=req.portal_email)
                return action + ":ok"
            except ValidationError:
                return action + ":rejected"
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, ("deliver", "cancel")))
        self.assertEqual(sum(result.endswith(":ok") for result in results), 1, results)
        self.assertEqual(sum(result.endswith(":rejected") for result in results), 1, results)
        order.refresh_from_db(); req.refresh_from_db()
        if order.status == "DELIVERED":
            self.assertEqual(req.status, "RESOLVED")
            self.assertEqual(FinancialTransaction.objects.filter(type="POS_CHARGE").count(), 1)
            self.assertEqual(StockMovement.objects.filter(type="POS_CONSUMPTION").count(), 1)
            self.assertEqual(StockBalance.objects.get(item=self.ingredient, location=self.location).quantity_on_hand, Decimal("3"))
        else:
            self.assertEqual((order.status, req.status), ("CANCELLED", "CANCELLED"))
            self.assertFalse(FinancialTransaction.objects.filter(type="POS_CHARGE").exists())
            self.assertFalse(StockMovement.objects.filter(type="POS_CONSUMPTION").exists())
