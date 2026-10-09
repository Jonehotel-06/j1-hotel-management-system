"""Staff-reviewed handoff from a table QR request into a server-priced POS draft."""

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.guest_services.models import ServiceQRLink, ServiceRequest, ServiceRequestEvent
from apps.guest_services.services.request_service import create_service_request
from apps.pos.models import MenuCategory, MenuItem, PosOrder, PosOrderEvent, RestaurantTable
from apps.pos.services.table_service import open_table_session

from .base import BaseAPITestCase
from .factories import make_staff


class ServiceRequestPosHandoffTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.waiter = make_staff("qr-handoff.waiter@staff.dev", role=User.Role.WAITER)
        self.restaurant_manager = make_staff("qr-handoff.manager@staff.dev", role=User.Role.RESTAURANT_MANAGER)
        self.bartender = make_staff("qr-handoff.bartender@staff.dev", role=User.Role.BARTENDER)
        self.table = RestaurantTable.objects.create(code="T-QR-01", name="Patio one", section="Patio", seats=4)
        self.session, _ = open_table_session(
            actor=self.waiter,
            table_id=self.table.pk,
            covers=2,
            notes="",
            idempotency_key="qr-handoff-session-001",
        )
        self.restaurant_category = MenuCategory.objects.create(
            name="QR handoff restaurant", slug="qr-handoff-restaurant", service_area=MenuCategory.ServiceArea.RESTAURANT,
        )
        self.restaurant_item = MenuItem.objects.create(
            category=self.restaurant_category, name="Grilled fish", sku="QR-HANDOFF-FISH", base_price="4800.00",
        )
        self.bar_category = MenuCategory.objects.create(
            name="QR handoff bar", slug="qr-handoff-bar", service_area=MenuCategory.ServiceArea.BAR,
        )
        self.bar_item = MenuItem.objects.create(
            category=self.bar_category, name="Fresh juice", sku="QR-HANDOFF-JUICE", base_price="1200.00",
        )

    def _qr_request(self, *, table_label, key, link=None):
        if link is None:
            link = ServiceQRLink.objects.create(
                reference=f"QR-{key[-8:].upper()}",
                target_key=f"table:{table_label.casefold()}",
                target_type=ServiceQRLink.TargetType.TABLE,
                table_number=table_label,
                label=f"Table {table_label}",
                token_hash=(key.encode().hex() + "0" * 64)[:64],
            )
        request, created = create_service_request(
            guest=None,
            category=ServiceRequest.Category.FOOD_BEVERAGE,
            channel=ServiceRequest.Channel.QR,
            summary="Please send a server",
            detail="The guest is ready to order.",
            table_number=table_label,
            qr_link=link,
            idempotency_key=f"qr-service-request-{key}",
        )
        self.assertTrue(created)
        return request

    def test_waiter_creates_server_priced_restaurant_draft_linked_to_matching_open_session(self):
        service_request = self._qr_request(table_label=self.table.code, key="restaurant-0001")
        self.auth(self.waiter)
        payload = {
            "mode": "RESTAURANT",
            "lines": [{"menu_item_id": self.restaurant_item.pk, "quantity": 1}],
            "table_session_reference": self.session.reference,
            "service_request_reference": service_request.reference,
            "idempotency_key": "qr-handoff-order-restaurant-01",
            "subtotal": "0.01",
            "total_amount": "0.01",
        }
        response = self.client.post("/api/admin/pos/orders/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()["data"]
        self.assertEqual(data["service_request_reference"], service_request.reference)
        self.assertEqual(data["table_session_reference"], self.session.reference)
        self.assertEqual(data["table_number"], self.table.code)
        self.assertEqual(data["status"], PosOrder.Status.DRAFT)
        self.assertEqual(data["subtotal"], "4800.00")

        order = PosOrder.objects.get(reference=data["reference"])
        self.assertIsNone(order.charge_transaction_id)
        self.assertFalse(PosOrderEvent.objects.filter(order=order, type=PosOrderEvent.Type.CHARGED).exists())
        self.assertEqual(order.service_request_id, service_request.pk)
        self.assertEqual(order.table_session_id, self.session.pk)
        self.assertEqual(order.lines.count(), 1)
        linked_events = ServiceRequestEvent.objects.filter(
            request=service_request, type=ServiceRequestEvent.Type.POS_ORDER_LINKED,
        )
        self.assertEqual(linked_events.count(), 1)
        self.assertEqual(linked_events.get().details["pos_order_reference"], order.reference)
        self.assertTrue(AuditLog.objects.filter(action="SERVICE_REQUEST_POS_DRAFT_CREATED", object_id=str(service_request.pk)).exists())
        detail = self.client.get(f"/api/admin/service-requests/{service_request.reference}/")
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(detail.json()["data"]["pos_order_reference"], order.reference)
        self.assertIn(
            ServiceRequestEvent.Type.POS_ORDER_LINKED,
            [event["type"] for event in detail.json()["data"]["events"]],
        )
        service_request.refresh_from_db()
        self.assertEqual(service_request.status, ServiceRequest.Status.OPEN)

        replay = self.client.post("/api/admin/pos/orders/", payload, format="json")
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(replay.json()["data"]["reference"], order.reference)
        self.assertEqual(PosOrder.objects.filter(service_request=service_request).count(), 1)

    def test_restaurant_handoff_rejects_another_table_session_and_a_second_order(self):
        service_request = self._qr_request(table_label=self.table.code, key="restaurant-0002")
        other_table = RestaurantTable.objects.create(code="T-QR-02", name="Patio two", seats=4)
        other_session, _ = open_table_session(
            actor=self.waiter, table_id=other_table.pk, covers=1, idempotency_key="qr-handoff-session-002",
        )
        self.auth(self.waiter)
        payload = {
            "mode": "RESTAURANT",
            "lines": [{"menu_item_id": self.restaurant_item.pk, "quantity": 1}],
            "table_session_reference": other_session.reference,
            "service_request_reference": service_request.reference,
            "idempotency_key": "qr-handoff-order-wrong-table",
        }
        mismatch = self.client.post("/api/admin/pos/orders/", payload, format="json")
        self.assertEqual(mismatch.status_code, 400, mismatch.content)
        self.assertEqual(PosOrder.objects.count(), 0)

        valid_payload = {**payload, "table_session_reference": self.session.reference, "idempotency_key": "qr-handoff-order-valid-table"}
        first = self.client.post("/api/admin/pos/orders/", valid_payload, format="json")
        self.assertEqual(first.status_code, 201, first.content)
        duplicate = self.client.post(
            "/api/admin/pos/orders/",
            {**valid_payload, "idempotency_key": "qr-handoff-order-second-key"},
            format="json",
        )
        self.assertEqual(duplicate.status_code, 200, duplicate.content)
        self.assertEqual(duplicate.json()["data"]["reference"], first.json()["data"]["reference"])
        self.assertEqual(PosOrder.objects.filter(service_request=service_request).count(), 1)

        other_request = self._qr_request(
            table_label=self.table.code,
            key="restaurant-0004",
            link=service_request.qr_link,
        )
        reused_key = self.client.post(
            "/api/admin/pos/orders/",
            {**valid_payload, "service_request_reference": other_request.reference},
            format="json",
        )
        self.assertEqual(reused_key.status_code, 400, reused_key.content)
        self.assertFalse(PosOrder.objects.filter(service_request=other_request).exists())

    def test_restaurant_manager_without_guest_request_capability_cannot_link_a_request(self):
        service_request = self._qr_request(table_label=self.table.code, key="restaurant-0003")
        self.auth(self.restaurant_manager)
        response = self.client.post("/api/admin/pos/orders/", {
            "mode": "RESTAURANT",
            "lines": [{"menu_item_id": self.restaurant_item.pk, "quantity": 1}],
            "table_session_reference": self.session.reference,
            "service_request_reference": service_request.reference,
            "idempotency_key": "qr-handoff-order-unauthorized",
        }, format="json")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(PosOrder.objects.filter(service_request=service_request).exists())

    def test_bartender_creates_free_text_bar_draft_from_table_qr_without_restaurant_session(self):
        service_request = self._qr_request(table_label="Bar 7", key="bar-0001")
        self.auth(self.bartender)
        response = self.client.post("/api/admin/pos/orders/", {
            "mode": "BAR",
            "table_number": "different text is ignored",
            "lines": [{"menu_item_id": self.bar_item.pk, "quantity": 2}],
            "service_request_reference": service_request.reference,
            "idempotency_key": "qr-handoff-order-bar-0001",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()["data"]
        self.assertEqual(data["mode"], PosOrder.Mode.BAR)
        self.assertEqual(data["table_number"], "Bar 7")
        self.assertIsNone(data["table_session_reference"])
        self.assertEqual(data["subtotal"], "2400.00")
        self.assertEqual(data["service_request_reference"], service_request.reference)
