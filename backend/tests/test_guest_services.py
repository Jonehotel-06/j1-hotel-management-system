"""Guest-service ownership, state-machine, and team-queue integration tests."""
from datetime import timedelta

from django.utils import timezone

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import FinancialLine, FinancialTransaction
from apps.finance.services import accounting
from apps.finance.services.folio_service import get_or_create_main_folio_for_stay
from apps.finance.services.ledger_service import create_posted_transaction
from apps.guest_services.models import ServiceRequest, ServiceRequestEvent
from apps.guest_services.services.request_service import create_service_request
from apps.portal.models import PortalSession
from apps.portal.services import hash_secret
from apps.stays.models import Stay, StayRoom

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type, make_staff


class GuestServiceTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("guest-service-manager@staff.dev", role=User.Role.MANAGER)
        self.housekeeper = make_staff("guest-service-housekeeper@staff.dev", role=User.Role.HOUSEKEEPING)
        self.maintenance = make_staff("guest-service-maintenance@staff.dev", role=User.Role.MAINTENANCE)
        self.guest = make_guest("guest-service@example.com")
        room_type = make_room_type("Guest service suite", price="10000.00")
        self.room = make_room(room_type, "GS-01")
        booking = make_booking(
            self.guest, room_type, [self.room], check_in=hotel_today(),
            check_out=hotel_today() + timedelta(days=2), total="20000.00",
        )
        self.stay = Stay.objects.create(
            reference=f"STY-{booking.booking_reference}", booking=booking, guest=self.guest,
            status=Stay.Status.IN_HOUSE, expected_arrival=booking.check_in, expected_departure=booking.check_out,
        )
        StayRoom.objects.create(stay=self.stay, room=self.room, actual_check_in_at=timezone.now())

    def _portal_token(self, email=None):
        email = email or self.guest.email
        token = f"guest-services-portal-token-{email}-0123456789-abcdefghijklmnopqrstuvwxyz"
        PortalSession.objects.create(
            email=email,
            session_hash=hash_secret(token),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        return token

    def test_portal_request_is_idempotent_and_hides_internal_staff_comments(self):
        token = self._portal_token()
        payload = {
            "stay_reference": self.stay.reference,
            "category": "HOUSEKEEPING",
            "priority": "NORMAL",
            "summary": "Please bring towels",
            "detail": "Two extra towels, please.",
            "idempotency_key": "guest-towels-1",
        }
        created = self.client.post("/api/portal/requests/", payload, format="json", HTTP_X_PORTAL_SESSION=token)
        self.assertEqual(created.status_code, 201, created.content)
        reference = created.json()["data"]["reference"]
        retry = self.client.post("/api/portal/requests/", payload, format="json", HTTP_X_PORTAL_SESSION=token)
        self.assertEqual(retry.status_code, 200, retry.content)
        self.assertEqual(retry.json()["data"]["reference"], reference)
        service_request = ServiceRequest.objects.get(reference=reference)
        self.assertEqual(service_request.room_id, self.room.pk)
        self.assertEqual(service_request.owner_team, ServiceRequest.OwnerTeam.HOUSEKEEPING)

        self.auth(self.manager)
        routed = self.client.post(f"/api/admin/service-requests/{reference}/housekeeping-task/", {}, format="json")
        self.assertEqual(routed.status_code, 200, routed.content)
        rerouted = self.client.post(f"/api/admin/service-requests/{reference}/housekeeping-task/", {}, format="json")
        self.assertEqual(rerouted.status_code, 200, rerouted.content)
        self.assertFalse(rerouted.json()["data"]["created"])
        service_request.refresh_from_db()
        self.assertEqual(service_request.housekeeping_task.reference, routed.json()["data"]["task_reference"])

        internal = self.client.post(
            f"/api/admin/service-requests/{reference}/comments/",
            {"message": "Use linen-store reserve", "guest_visible": False}, format="json",
        )
        self.assertEqual(internal.status_code, 200, internal.content)
        self.unauth()
        detail = self.client.get(f"/api/portal/requests/{reference}/", HTTP_X_PORTAL_SESSION=token)
        self.assertEqual(detail.status_code, 200, detail.content)
        portal_messages = {event["message"] for event in detail.json()["data"]["events"]}
        self.assertIn("Two extra towels, please.", portal_messages)
        self.assertNotIn("Use linen-store reserve", portal_messages)

        other_token = self._portal_token("other-guest-service@example.com")
        self.assertEqual(
            self.client.get(f"/api/portal/requests/{reference}/", HTTP_X_PORTAL_SESSION=other_token).status_code,
            404,
        )

    def test_team_worker_can_claim_and_progress_own_queue_but_cannot_view_other_team(self):
        house_request, _ = create_service_request(
            guest=self.guest, stay=self.stay, room=self.room,
            category=ServiceRequest.Category.HOUSEKEEPING,
            channel=ServiceRequest.Channel.FRONT_DESK,
            summary="Clean bathroom", actor=self.manager,
        )
        maintenance_request, _ = create_service_request(
            guest=self.guest, stay=self.stay, room=self.room,
            category=ServiceRequest.Category.MAINTENANCE,
            channel=ServiceRequest.Channel.FRONT_DESK,
            summary="Repair lamp", actor=self.manager,
        )
        self.auth(self.housekeeper)
        queue = self.client.get("/api/admin/service-requests/")
        self.assertEqual(queue.status_code, 200, queue.content)
        references = {row["reference"] for row in queue.json()["data"]}
        self.assertIn(house_request.reference, references)
        self.assertNotIn(maintenance_request.reference, references)
        self.assertEqual(
            self.client.get(f"/api/admin/service-requests/{maintenance_request.reference}/").status_code,
            404,
        )
        claimed = self.client.post(f"/api/admin/service-requests/{house_request.reference}/claim/", {}, format="json")
        self.assertEqual(claimed.status_code, 200, claimed.content)
        self.assertEqual(
            self.client.post(
                f"/api/admin/service-requests/{house_request.reference}/status/",
                {"status": "ACKNOWLEDGED", "note": "On my way."}, format="json",
            ).status_code,
            200,
        )
        house_request.refresh_from_db()
        self.assertEqual(house_request.status, ServiceRequest.Status.ACKNOWLEDGED)
        self.assertEqual(house_request.assigned_to_id, self.housekeeper.pk)
        self.assertTrue(ServiceRequestEvent.objects.filter(
            request=house_request, type=ServiceRequestEvent.Type.ACKNOWLEDGED, guest_visible=True
        ).exists())

    def test_portal_folio_reads_are_email_owned_and_statement_is_paginated(self):
        folio, _ = get_or_create_main_folio_for_stay(stay=self.stay, actor=self.manager)
        transaction, _ = create_posted_transaction(
            transaction_type=FinancialTransaction.Type.OTHER_CHARGE,
            source_key="guest-service-portal-folio-charge",
            actor=self.manager,
            lines=[
                {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": FinancialLine.Direction.DEBIT,
                 "amount": "250.00", "folio": folio},
                {"account_code": accounting.OTHER_OPERATING_REVENUE, "direction": FinancialLine.Direction.CREDIT,
                 "amount": "250.00"},
            ],
            postings=[{"line_index": 0, "folio": folio, "kind": "CHARGE", "effect": "DEBIT", "amount": "250.00"}],
        )
        self.assertIsNotNone(transaction.pk)
        token = self._portal_token()
        folios = self.client.get("/api/portal/folios/?page_size=10", HTTP_X_PORTAL_SESSION=token)
        self.assertEqual(folios.status_code, 200, folios.content)
        self.assertEqual(folios.json()["data"][0]["reference"], folio.reference)
        self.assertEqual(folios.json()["data"][0]["balance"], "250.00")
        statement = self.client.get(
            f"/api/portal/folios/{folio.reference}/postings/", HTTP_X_PORTAL_SESSION=token
        )
        self.assertEqual(statement.status_code, 200, statement.content)
        self.assertEqual(statement.json()["data"][0]["amount"], "250.00")
        other_token = self._portal_token("other-folio-guest@example.com")
        self.assertEqual(
            self.client.get(f"/api/portal/folios/{folio.reference}/", HTTP_X_PORTAL_SESSION=other_token).status_code,
            404,
        )

    def test_guest_can_cancel_open_request_but_not_an_in_progress_request(self):
        token = self._portal_token()
        service_request, _ = create_service_request(
            guest=self.guest, stay=self.stay, room=self.room,
            category=ServiceRequest.Category.GENERAL,
            channel=ServiceRequest.Channel.PORTAL,
            summary="Please call me", portal_email=self.guest.email,
        )
        cancelled = self.client.post(
            f"/api/portal/requests/{service_request.reference}/cancel/", {"note": "No longer needed"},
            format="json", HTTP_X_PORTAL_SESSION=token,
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.content)
        service_request.refresh_from_db()
        self.assertEqual(service_request.status, ServiceRequest.Status.CANCELLED)
        in_progress, _ = create_service_request(
            guest=self.guest, stay=self.stay, room=self.room,
            category=ServiceRequest.Category.GENERAL,
            channel=ServiceRequest.Channel.PORTAL,
            summary="Already being handled", portal_email=self.guest.email,
        )
        from apps.guest_services.services.request_service import transition_service_request
        transition_service_request(
            service_request=in_progress, target_status=ServiceRequest.Status.IN_PROGRESS, actor=self.manager
        )
        blocked = self.client.post(
            f"/api/portal/requests/{in_progress.reference}/cancel/", {}, format="json", HTTP_X_PORTAL_SESSION=token,
        )
        self.assertEqual(blocked.status_code, 400, blocked.content)
