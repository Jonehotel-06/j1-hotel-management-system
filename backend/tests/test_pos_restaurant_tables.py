"""Registered dining tables, session idempotency, and POS close safeguards."""
from apps.accounts.models import User
from apps.pos.models import (
    MenuCategory,
    MenuItem,
    PosOrder,
    RestaurantTableSession,
    RestaurantTableSessionEvent,
)

from .base import BaseAPITestCase
from .factories import make_staff


class PosRestaurantTableApiTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("restaurant-tables.manager@staff.dev", role=User.Role.RESTAURANT_MANAGER)
        self.waiter = make_staff("restaurant-tables.waiter@staff.dev", role=User.Role.WAITER)
        self.category = MenuCategory.objects.create(
            name="Table service mains", slug="table-service-mains", service_area=MenuCategory.ServiceArea.RESTAURANT,
        )
        self.item = MenuItem.objects.create(
            category=self.category, name="Table service meal", sku="TABLE-SERVICE-MEAL", base_price="3500.00",
        )

    def _register_table(self, *, code="T-01", seats=2):
        self.auth(self.manager)
        response = self.client.post("/api/admin/pos/restaurant-tables/", {
            "code": code, "name": "Window table", "section": "Main dining", "seats": seats,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["data"]

    def _open_session(self, table, *, actor=None, key="table-session-open-01", covers=2):
        self.auth(actor or self.waiter)
        return self.client.post("/api/admin/pos/restaurant-table-sessions/", {
            "table_id": table["id"], "covers": covers, "notes": "Dinner service", "idempotency_key": key,
        }, format="json")

    def _create_order(self, session_reference, *, key="table-pos-order-001", mode=PosOrder.Mode.RESTAURANT):
        return self.client.post("/api/admin/pos/orders/", {
            "mode": mode,
            "table_session_reference": session_reference,
            "lines": [{"menu_item_id": self.item.pk, "quantity": 1}],
            "idempotency_key": key,
        }, format="json")

    def test_register_read_scope_and_manager_only_write(self):
        table = self._register_table(code=" t-01 ")
        self.assertEqual(table["code"], "T-01")

        self.auth(self.waiter)
        listed = self.client.get("/api/admin/pos/restaurant-tables/", {"active": "true", "search": "window"})
        self.assertEqual(listed.status_code, 200, listed.content)
        self.assertEqual([row["id"] for row in listed.json()["data"]], [table["id"]])
        denied_create = self.client.post("/api/admin/pos/restaurant-tables/", {
            "code": "T-02", "seats": 2,
        }, format="json")
        self.assertEqual(denied_create.status_code, 403, denied_create.content)

        self.auth(self.manager)
        invalid_code = self.client.post("/api/admin/pos/restaurant-tables/", {
            "code": "T/02", "seats": 2,
        }, format="json")
        self.assertEqual(invalid_code.status_code, 400, invalid_code.content)
        duplicate_case = self.client.post("/api/admin/pos/restaurant-tables/", {
            "code": "t-01", "seats": 2,
        }, format="json")
        self.assertEqual(duplicate_case.status_code, 400, duplicate_case.content)

    def test_table_session_idempotency_capacity_and_one_open_session_per_table(self):
        table = self._register_table()
        opened = self._open_session(table)
        self.assertEqual(opened.status_code, 201, opened.content)
        session_ref = opened.json()["data"]["reference"]

        replay = self._open_session(table)
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(replay.json()["data"]["reference"], session_ref)
        changed_retry = self._open_session(table, covers=1)
        self.assertEqual(changed_retry.status_code, 400, changed_retry.content)

        duplicate = self._open_session(table, key="table-session-open-02")
        self.assertEqual(duplicate.status_code, 409, duplicate.content)
        self.assertEqual(duplicate.json()["code"], "TABLE_SESSION_CONFLICT")

        too_many = self._open_session(table, key="table-session-open-03", covers=3)
        self.assertEqual(too_many.status_code, 400, too_many.content)

        self.auth(self.manager)
        edit_while_open = self.client.patch(
            f"/api/admin/pos/restaurant-tables/{table['id']}/", {"name": "Renamed table"}, format="json",
        )
        self.assertEqual(edit_while_open.status_code, 409, edit_while_open.content)
        active = self.client.get("/api/admin/pos/restaurant-table-sessions/", {"status": "OPEN"})
        self.assertEqual(active.status_code, 200, active.content)
        self.assertEqual([row["reference"] for row in active.json()["data"]], [session_ref])

    def test_table_identity_freezes_after_first_session_and_manager_controls_activation(self):
        table = self._register_table()
        opened = self._open_session(table)
        session_ref = opened.json()["data"]["reference"]

        self.auth(self.manager)
        deactivate_while_open = self.client.patch(
            f"/api/admin/pos/restaurant-tables/{table['id']}/", {"is_active": False}, format="json",
        )
        self.assertEqual(deactivate_while_open.status_code, 409, deactivate_while_open.content)

        self.auth(self.waiter)
        closed = self.client.post(
            f"/api/admin/pos/restaurant-table-sessions/{session_ref}/close/", {}, format="json",
        )
        self.assertEqual(closed.status_code, 200, closed.content)

        self.auth(self.manager)
        rename_after_history = self.client.patch(
            f"/api/admin/pos/restaurant-tables/{table['id']}/", {"code": "T-01-RENUMBERED"}, format="json",
        )
        self.assertEqual(rename_after_history.status_code, 409, rename_after_history.content)
        deactivated = self.client.patch(
            f"/api/admin/pos/restaurant-tables/{table['id']}/", {"is_active": False}, format="json",
        )
        self.assertEqual(deactivated.status_code, 200, deactivated.content)

        self.auth(self.waiter)
        inactive_open = self.client.post("/api/admin/pos/restaurant-table-sessions/", {
            "table_id": table["id"], "covers": 1, "idempotency_key": "inactive-table-open-01",
        }, format="json")
        self.assertEqual(inactive_open.status_code, 409, inactive_open.content)

        self.auth(self.manager)
        reactivated = self.client.patch(
            f"/api/admin/pos/restaurant-tables/{table['id']}/", {"is_active": True}, format="json",
        )
        self.assertEqual(reactivated.status_code, 200, reactivated.content)
        self.auth(self.waiter)
        reopened = self.client.post("/api/admin/pos/restaurant-table-sessions/", {
            "table_id": table["id"], "covers": 1, "idempotency_key": "active-table-open-01",
        }, format="json")
        self.assertEqual(reopened.status_code, 201, reopened.content)

    def test_table_order_link_close_guard_and_closed_session_rejection(self):
        table = self._register_table()
        opened = self._open_session(table)
        session_ref = opened.json()["data"]["reference"]

        self.auth(self.waiter)
        order_response = self._create_order(session_ref)
        self.assertEqual(order_response.status_code, 201, order_response.content)
        order = order_response.json()["data"]
        self.assertEqual(order["table_number"], table["code"])
        self.assertEqual(order["table_session_reference"], session_ref)
        self.assertEqual(order["table_session_code"], table["code"])
        self.assertEqual(order["status"], PosOrder.Status.DRAFT)
        self.assertEqual(
            list(RestaurantTableSessionEvent.objects.filter(table_session__reference=session_ref).values_list("type", flat=True)),
            [RestaurantTableSessionEvent.Type.OPENED, RestaurantTableSessionEvent.Type.ORDER_ADDED],
        )

        invalid_mode = self._create_order(session_ref, key="table-bar-order-001", mode=PosOrder.Mode.BAR)
        self.assertEqual(invalid_mode.status_code, 400, invalid_mode.content)
        filtered = self.client.get("/api/admin/pos/orders/", {"table_session": session_ref})
        self.assertEqual(filtered.status_code, 200, filtered.content)
        self.assertEqual([row["reference"] for row in filtered.json()["data"]], [order["reference"]])

        close_while_draft = self.client.post(
            f"/api/admin/pos/restaurant-table-sessions/{session_ref}/close/", {}, format="json",
        )
        self.assertEqual(close_while_draft.status_code, 409, close_while_draft.content)

        cancelled = self.client.post(
            f"/api/admin/pos/orders/{order['reference']}/status/", {"status": "CANCELLED"}, format="json",
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.content)
        self.assertEqual(cancelled.json()["data"]["status"], PosOrder.Status.CANCELLED)

        closed = self.client.post(
            f"/api/admin/pos/restaurant-table-sessions/{session_ref}/close/",
            {"close_note": "Guest departed"}, format="json",
        )
        self.assertEqual(closed.status_code, 200, closed.content)
        self.assertEqual(closed.json()["data"]["status"], RestaurantTableSession.Status.CLOSED)
        self.assertEqual(closed.json()["data"]["order_count"], 1)
        self.assertEqual(closed.json()["data"]["active_order_count"], 0)

        repeated_close = self.client.post(
            f"/api/admin/pos/restaurant-table-sessions/{session_ref}/close/", {}, format="json",
        )
        self.assertEqual(repeated_close.status_code, 200, repeated_close.content)
        self.assertEqual(
            RestaurantTableSessionEvent.objects.filter(
                table_session__reference=session_ref, type=RestaurantTableSessionEvent.Type.CLOSED,
            ).count(),
            1,
        )
        closed_order = self._create_order(session_ref, key="table-order-on-closed-session")
        self.assertEqual(closed_order.status_code, 409, closed_order.content)

        reopened = self._open_session(table, key="table-session-open-04", covers=1)
        self.assertEqual(reopened.status_code, 201, reopened.content)
        self.assertNotEqual(reopened.json()["data"]["reference"], session_ref)

    def test_unpaid_delivered_orders_block_close_until_ledger_tender_is_captured(self):
        table = self._register_table()
        session = self._open_session(table).json()["data"]
        self.auth(self.waiter)
        order_response = self._create_order(session["reference"], key="table-pos-unpaid-001")
        self.assertEqual(order_response.status_code, 201, order_response.content)
        order = order_response.json()["data"]
        self.assertEqual(order["total_amount"], "3500.00")

        self.assertEqual(self.client.post(f"/api/admin/pos/orders/{order['reference']}/submit/", {}, format="json").status_code, 200)
        for state in ("PREPARING", "READY", "DELIVERED"):
            transition = self.client.post(
                f"/api/admin/pos/orders/{order['reference']}/status/", {"status": state}, format="json",
            )
            self.assertEqual(transition.status_code, 200, transition.content)

        close_unpaid = self.client.post(
            f"/api/admin/pos/restaurant-table-sessions/{session['reference']}/close/", {}, format="json",
        )
        self.assertEqual(close_unpaid.status_code, 409, close_unpaid.content)
        self.assertEqual(close_unpaid.json()["code"], "TABLE_SESSION_CONFLICT")

        tender = self.client.post(
            f"/api/admin/pos/orders/{order['reference']}/tenders/",
            {"method": "CARD", "amount": "3500.00"}, format="json",
        )
        self.assertEqual(tender.status_code, 201, tender.content)
        self.assertEqual(tender.json()["data"]["settlement_status"], PosOrder.SettlementStatus.PAID)
        closed = self.client.post(
            f"/api/admin/pos/restaurant-table-sessions/{session['reference']}/close/", {}, format="json",
        )
        self.assertEqual(closed.status_code, 200, closed.content)
        self.assertEqual(closed.json()["data"]["unpaid_order_count"], 0)
