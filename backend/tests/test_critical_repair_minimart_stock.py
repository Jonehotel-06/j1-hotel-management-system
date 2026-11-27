# tests/test_critical_repair_minimart_stock.py
"""Mini Mart stock-quantity editing repair (2026-11-26).

Reported fault: the Edit button on a Mini Mart product did not allow the stock
quantity to be changed; the only way was the Inventory screen.

Root cause: editing disabled the opening-quantity field, and the write
serializer rejects ``opening_quantity`` on an update ("use inventory
replenishment"). There was no controlled correction path at all from Mini Mart.

The repair adds one endpoint that posts the counted difference through the
EXISTING ``adjust_stock_balance`` service, so the change is a traceable
``ADJUSTMENT`` movement with a mandatory reason — never a direct balance write.
"""
from decimal import Decimal

from apps.accounts.models import User
from apps.inventory.models import (
    StockBalance, StockBalanceAdjustment, StockItem, StockLocation, StockMovement,
)
from apps.inventory.services.inventory_service import record_stock_movement
from apps.guest_services.models import MiniMartProduct

from .base import BaseAPITestCase
from .factories import make_staff

URL = "/api/admin/inventory/minimart/products/{pk}/stock-adjustment/"


class MiniMartStockAdjustmentTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.keeper = make_staff("repair-mart-keeper@staff.dev", role=User.Role.INVENTORY_CLERK)
        self.receptionist = make_staff("repair-mart-desk@staff.dev", role=User.Role.RECEPTIONIST)
        self.store = StockLocation.objects.create(code="REP-MART", name="Reception Store MART")
        self.item = StockItem.objects.create(
            sku="REP-MART-WATER", name="Bottled water", base_unit="PC",
            item_type=StockItem.ItemType.SELLABLE_PRODUCT, standard_cost=Decimal("100.00"),
        )
        self.product = MiniMartProduct.objects.create(
            item=self.item, location=self.store, description="", selling_price=Decimal("200.00"),
        )
        record_stock_movement(
            item=self.item, location=self.store, movement_type=StockMovement.Type.OPENING,
            quantity_delta="12", source_key="rep-mart-opening", actor=self.keeper,
        )

    def balance(self):
        return StockBalance.objects.get(item=self.item, location=self.store)

    def test_authorized_staff_correct_the_counted_quantity(self):
        self.auth(self.keeper)
        response = self.client.post(
            URL.format(pk=self.product.pk),
            {"counted_quantity": "9", "reason": "Three bottles broken in the store room"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.balance().quantity_on_hand, Decimal("9.0000"))
        adjustment = StockBalanceAdjustment.objects.get()
        self.assertEqual(adjustment.previous_quantity, Decimal("12.0000"))
        self.assertEqual(adjustment.new_quantity, Decimal("9.0000"))
        self.assertEqual(adjustment.quantity_delta, Decimal("-3.0000"))
        self.assertIn("broken", adjustment.reason)
        self.assertEqual(adjustment.actor_id, self.keeper.pk)
        # A real ledger movement, not a rewritten balance row.
        movement = StockMovement.objects.get(type=StockMovement.Type.ADJUSTMENT)
        self.assertEqual(movement.quantity_delta, Decimal("-3.0000"))
        self.assertEqual(movement.location_id, self.store.pk)
        # The response itself reports the new quantity for the Mini Mart list.
        self.assertEqual(Decimal(response.json()["data"]["product"]["stock_quantity"]), Decimal("9.0000"))

    def test_unauthorized_staff_cannot_change_stock(self):
        self.auth(self.receptionist)
        response = self.client.post(
            URL.format(pk=self.product.pk),
            {"counted_quantity": "1", "reason": "Unapproved correction attempt"},
            format="json",
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(self.balance().quantity_on_hand, Decimal("12.0000"))
        self.assertFalse(StockBalanceAdjustment.objects.exists())

    def test_a_reason_is_required(self):
        self.auth(self.keeper)
        response = self.client.post(
            URL.format(pk=self.product.pk), {"counted_quantity": "9"}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(self.balance().quantity_on_hand, Decimal("12.0000"))

    def test_negative_quantities_are_refused(self):
        self.auth(self.keeper)
        response = self.client.post(
            URL.format(pk=self.product.pk),
            {"counted_quantity": "-1", "reason": "Impossible negative count"},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(self.balance().quantity_on_hand, Decimal("12.0000"))

    def test_retried_correction_is_recorded_once(self):
        self.auth(self.keeper)
        payload = {"counted_quantity": "7", "reason": "Stock count correction", "idempotency_key": "rep-mart-retry"}
        first = self.client.post(URL.format(pk=self.product.pk), payload, format="json")
        self.assertEqual(first.status_code, 200, first.content)
        second = self.client.post(URL.format(pk=self.product.pk), payload, format="json")
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(StockBalanceAdjustment.objects.count(), 1)
        self.assertEqual(self.balance().quantity_on_hand, Decimal("7.0000"))

    def test_the_product_payload_exposes_the_ledger_balance_id(self):
        self.auth(self.keeper)
        response = self.client.get("/api/admin/inventory/minimart/products/")
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()["data"]
        row = payload["results"][0] if isinstance(payload, dict) else payload[0]
        self.assertEqual(row["balance_id"], self.balance().pk)
        self.assertEqual(Decimal(row["stock_quantity"]), Decimal("12.0000"))
