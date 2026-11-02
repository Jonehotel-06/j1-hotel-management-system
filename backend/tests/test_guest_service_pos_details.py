"""Guest-service staff views expose the linked POS order's full service evidence."""

from apps.accounts.models import User
from apps.guest_services.models import ServiceQRLink, ServiceRequest
from apps.guest_services.services.request_service import create_service_request
from apps.pos.models import MenuCategory, MenuItem, RestaurantTable
from apps.pos.services.order_service import (
    capture_direct_tender,
    create_order,
    submit_order,
    transition_order,
)
from apps.pos.services.table_service import open_table_session

from .base import BaseAPITestCase
from .factories import make_staff


class GuestServicePosDetailTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_staff("pos-detail.admin@staff.dev", role=User.Role.ADMIN)
        self.table = RestaurantTable.objects.create(
            code="T-POS-DETAIL", name="Detail table", section="Main", seats=2,
        )
        self.session, _ = open_table_session(
            actor=self.admin, table_id=self.table.pk, covers=2,
            notes="", idempotency_key="pos-detail-session",
        )
        self.category = MenuCategory.objects.create(
            name="POS detail food", slug="pos-detail-food",
            service_area=MenuCategory.ServiceArea.RESTAURANT,
        )
        self.item = MenuItem.objects.create(
            category=self.category, name="Jollof rice", sku="POS-DETAIL-JOLLOF",
            base_price="2500.00",
        )
        self.qr_link = ServiceQRLink.objects.create(
            reference="QR-POS-DETAIL",
            target_key=f"table:{self.table.code.casefold()}",
            target_type=ServiceQRLink.TargetType.TABLE,
            table_number=self.table.code,
            label="Table T-POS-DETAIL",
            token_hash="b" * 64,
        )
        self.request, created = create_service_request(
            guest=None,
            category=ServiceRequest.Category.FOOD_BEVERAGE,
            channel=ServiceRequest.Channel.QR,
            summary="Guest ordered a dish",
            detail="Jollof rice for two",
            table_number=self.table.code,
            qr_link=self.qr_link,
            idempotency_key="pos-detail-service-request",
        )
        self.assertTrue(created)

    def test_detail_and_queue_show_dishes_served_by_staff_and_payment_evidence(self):
        order, created = create_order(
            mode="RESTAURANT",
            lines=[{"menu_item_id": self.item.pk, "quantity": 2}],
            actor=self.admin,
            table_session_reference=self.session.reference,
            service_request=self.request,
            idempotency_key="pos-detail-order",
        )
        self.assertTrue(created)
        submit_order(order=order, actor=self.admin)
        delivered = transition_order(order=order, target_status="DELIVERED", actor=self.admin)
        capture_direct_tender(
            order=delivered, method="CARD", amount=str(delivered.net_total),
            actor=self.admin, external_reference="CARD-AUTH-001",
            idempotency_key="pos-detail-tender",
        )

        self.auth(self.admin)
        detail = self.client.get(f"/api/admin/service-requests/{self.request.reference}/")
        self.assertEqual(detail.status_code, 200, detail.content)
        pos = detail.json()["data"]["pos_order_details"]
        self.assertEqual(pos["reference"], order.reference)
        self.assertEqual(len(pos["lines"]), 1)
        self.assertEqual(pos["lines"][0]["item_sku"], "POS-DETAIL-JOLLOF")
        self.assertEqual(pos["lines"][0]["quantity"], 2)
        self.assertEqual(pos["served_by_email"], self.admin.email)
        self.assertTrue(pos["charge_transaction_reference"])
        self.assertEqual(pos["settlement_status"], "PAID")
        self.assertEqual(pos["amount_captured"], str(order.total_amount))
        self.assertEqual(len(pos["tenders"]), 1)
        self.assertEqual(pos["tenders"][0]["method"], "CARD")
        self.assertEqual(pos["tenders"][0]["external_reference"], "CARD-AUTH-001")

        queue = self.client.get("/api/admin/service-requests/")
        self.assertEqual(queue.status_code, 200, queue.content)
        queue_payload = queue.json()["data"]
        rows = queue_payload["results"] if isinstance(queue_payload, dict) and "results" in queue_payload else queue_payload
        row = next(row for row in rows if row["reference"] == self.request.reference)
        self.assertEqual(row["pos_order_details"]["reference"], order.reference)
        self.assertEqual(row["pos_order_details"]["lines"][0]["quantity"], 2)

    def test_unlinked_request_has_no_invented_pos_evidence(self):
        self.auth(self.admin)
        detail = self.client.get(f"/api/admin/service-requests/{self.request.reference}/")
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertIsNone(detail.json()["data"]["pos_order_details"])
