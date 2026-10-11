# tests/test_pass_regressions.py
"""Regression coverage for the behaviours the audit found already correct.

D6 (kitchen ticket list), D11 (workstations/terminals), D13 (notification
count/list) and D15 (enquiry lifecycle) were PASSes or documentation-only
findings. The audit's recommendation for each is the same: keep an automated
test so a future change cannot quietly re-break them.
"""
from datetime import timedelta
from decimal import Decimal

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.enquiries.models import Enquiry
from apps.notifications.models import Notification
from apps.pos.services.order_service import create_order, submit_order, transition_order
from apps.pos.models import KitchenTicket, MenuCategory, MenuItem, PosOrder
from apps.rooms.models import Room

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room_type, make_staff


class KitchenTicketRegressionTests(BaseAPITestCase):
    """D6: the kitchen queue lists the tickets that are actually in production."""

    def setUp(self):
        super().setUp()
        self.manager = make_staff("d6.manager@staff.dev", role=User.Role.MANAGER)
        self.category = MenuCategory.objects.create(name="D6 mains")
        self.item = MenuItem.objects.create(
            category=self.category, name="Jollof rice", sku="D6-JOLLOF",
            base_price=Decimal("2500.00"),
        )
        # Built through the real service so the order, its line and its kitchen
        # ticket are exactly what production creates.
        self.order, _ = create_order(
            mode=PosOrder.Mode.RESTAURANT,
            lines=[{"menu_item_id": self.item.pk, "quantity": 2}],
            actor=self.manager,
        )
        submit_order(order=self.order, actor=self.manager)
        self.order.refresh_from_db()
        self.ticket = KitchenTicket.objects.get(order=self.order)
        transition_order(order=self.order, target_status="PREPARING", actor=self.manager)

        # A finished ticket must not appear in the production queue.
        finished, _ = create_order(
            mode=PosOrder.Mode.RESTAURANT,
            lines=[{"menu_item_id": self.item.pk, "quantity": 1}],
            actor=self.manager,
        )
        submit_order(order=finished, actor=self.manager)
        transition_order(order=finished, target_status="PREPARING", actor=self.manager)
        transition_order(order=finished, target_status="READY", actor=self.manager)
        transition_order(order=finished, target_status="DELIVERED", actor=self.manager)

    def test_kitchen_queue_lists_the_ticket_in_production(self):
        self.auth(self.manager)
        response = self.client.get("/api/admin/pos/kitchen-tickets/")
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["data"]
        self.assertEqual([row["order_reference"] for row in rows], [self.order.reference],
                         "the queue must show open tickets only")

        row = rows[0]
        self.assertEqual(row["status"], "PREPARING")
        self.assertEqual(row["station"], "KITCHEN")
        # The ticket must carry what the kitchen line needs to cook it.
        self.assertEqual(
            row["order_lines"],
            [{"item_name": "Jollof rice", "quantity": 2, "notes": "", "modifiers": []}],
        )


class CashSessionRouteRegressionTests(BaseAPITestCase):
    """D12: the legacy bare POS collection stays absent; actions and the finance
    listing keep working. This documents the decision rather than hiding it."""

    def setUp(self):
        super().setUp()
        self.manager = make_staff("d12.manager@staff.dev", role=User.Role.MANAGER)

    def test_legacy_bare_pos_cash_session_collection_is_not_routed(self):
        self.auth(self.manager)
        response = self.client.get("/api/admin/pos/cash-sessions/")
        self.assertEqual(
            response.status_code, 404,
            "the POS namespace intentionally exposes only open/close; sessions are listed by finance",
        )

    def test_cash_sessions_are_listed_by_finance_and_opened_by_pos(self):
        self.auth(self.manager)
        listing = self.client.get("/api/admin/finance/cash-sessions/")
        self.assertEqual(listing.status_code, 200, listing.content)

        opened = self.client.post("/api/admin/pos/cash-sessions/open/", {}, format="json")
        self.assertIn(opened.status_code, (200, 201), opened.content)


class WorkstationTerminalRegressionTests(BaseAPITestCase):
    """D11: `/api/admin/users/terminals/` is the collection the terminals page reads."""

    def test_terminals_collection_serves_the_dashboard_page(self):
        manager = make_staff("d11.manager@staff.dev", role=User.Role.MANAGER)
        self.auth(manager)
        response = self.client.get("/api/admin/users/terminals/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("data", response.json())


class NotificationFeedRegressionTests(BaseAPITestCase):
    """D13: the unread count and the list are consistent in one response."""

    def test_unread_count_matches_the_rows_returned(self):
        staff = make_staff("d13.staff@staff.dev", role=User.Role.RECEPTIONIST)
        for index in range(2):
            Notification.objects.create(
                recipient=staff, type=Notification.Type.SYSTEM,
                title=f"Unread {index}", message="Body",
            )
        Notification.objects.create(
            recipient=staff, type=Notification.Type.SYSTEM, title="Read", message="Body",
            is_read=True,
        )

        self.auth(staff)
        response = self.client.get("/api/notifications/")
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()["data"]
        rows = payload["notifications"]
        self.assertEqual(payload["unread_count"], 2)
        self.assertEqual(len([row for row in rows if not row["is_read"]]), 2)

        unread_only = self.client.get("/api/notifications/?unread=1")
        self.assertEqual(unread_only.json()["data"]["unread_count"], 2)
        self.assertEqual(len(unread_only.json()["data"]["notifications"]), 2)


class EnquiryLifecycleRegressionTests(BaseAPITestCase):
    """D15: PATCH is the enquiry lifecycle; the unused action routes stay absent."""

    def setUp(self):
        super().setUp()
        self.manager = make_staff("d15.manager@staff.dev", role=User.Role.MANAGER)
        self.enquiry = Enquiry.objects.create(
            name="Test Enquirer", email="d15.enquirer@example.com", phone="08030000000",
            subject="Late check-in", message="We will arrive after 10pm.",
            enquiry_type=Enquiry.EnquiryType.GENERAL,
        )

    def test_patch_moves_the_enquiry_through_its_lifecycle(self):
        self.auth(self.manager)
        for status in ("IN_PROGRESS", "RESOLVED", "CLOSED"):
            response = self.client.patch(
                f"/api/admin/enquiries/{self.enquiry.pk}/",
                {"status": status, "internal_notes": f"Moved to {status}."},
                format="json",
            )
            self.assertEqual(response.status_code, 200, response.content)
            self.enquiry.refresh_from_db()
            self.assertEqual(self.enquiry.status, status, "the lifecycle change must persist")
            self.assertEqual(self.enquiry.internal_notes, f"Moved to {status}.")

    def test_unimplemented_action_routes_stay_unrouted(self):
        self.auth(self.manager)
        for action in ("acknowledge", "resolve", "reply"):
            response = self.client.post(
                f"/api/admin/enquiries/{self.enquiry.pk}/{action}/", {}, format="json"
            )
            self.assertEqual(
                response.status_code, 404,
                f"enquiry {action} is not part of the contract; the UI uses PATCH",
            )
