"""Room/table QR issuance, privacy, revocation, and queue-routing coverage."""
from datetime import timedelta
from urllib.parse import urlsplit

from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.core.utils import hotel_today
from apps.guest_services.models import ServiceQRLink, ServiceRequest
from apps.guest_services.services.qr_service import service_qr_link_for_token
from apps.stays.models import Stay, StayRoom
from tests.base import BaseAPITestCase
from tests.factories import make_booking, make_guest, make_room, make_room_type, make_staff


class ServiceQRApiTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("service-qr.manager@staff.dev", role=User.Role.MANAGER)
        self.housekeeper = make_staff("service-qr.housekeeper@staff.dev", role=User.Role.HOUSEKEEPING)
        self.waiter = make_staff("service-qr.waiter@staff.dev", role=User.Role.WAITER)
        self.guest = make_guest("service-qr.guest@example.com")
        self.room_type = make_room_type("Service QR suite", price="10000.00")
        self.room = make_room(self.room_type, "QR-01")
        booking = make_booking(
            self.guest,
            self.room_type,
            [self.room],
            check_in=hotel_today(),
            check_out=hotel_today() + timedelta(days=2),
            total="20000.00",
        )
        self.stay = Stay.objects.create(
            reference=f"STY-{booking.booking_reference}",
            booking=booking,
            guest=self.guest,
            status=Stay.Status.IN_HOUSE,
            expected_arrival=booking.check_in,
            expected_departure=booking.check_out,
        )
        StayRoom.objects.create(stay=self.stay, room=self.room, actual_check_in_at=timezone.now())

    def _issue(self, target_type="ROOM", **extra):
        payload = {"target_type": target_type, **extra}
        self.auth(self.manager)
        response = self.client.post("/api/admin/service-qr-links/", payload, format="json")
        self.assertIn(response.status_code, (200, 201), response.content)
        self.assertIn("no-store", response["Cache-Control"])
        data = response.json()["data"]
        token = urlsplit(data["service_url"]).fragment.removeprefix("token=")
        self.assertTrue(token)
        return data, token

    def _public_headers(self, token):
        self.client.force_authenticate(user=None)
        self.client.credentials(HTTP_X_SERVICE_QR_TOKEN=token)

    def test_manager_issues_printable_room_qr_without_persisting_raw_bearer(self):
        data, token = self._issue(room_id=self.room.pk)
        self.assertEqual(data["target_type"], ServiceQRLink.TargetType.ROOM)
        self.assertIn("#token=", data["service_url"])
        self.assertIn("<svg", data["qr_svg"])
        self.assertIn("xmlns=\"http://www.w3.org/2000/svg\"", data["qr_svg"])
        self.assertNotIn("http://", data["qr_svg"].replace("http://www.w3.org/2000/svg", ""))

        link = ServiceQRLink.objects.get(reference=data["reference"])
        self.assertNotEqual(link.token_hash, token)
        self.assertEqual(link.token_hash, __import__("hashlib").sha256(token.encode()).hexdigest())
        self.assertEqual(link.room_id, self.room.pk)
        self.assertTrue(AuditLog.objects.filter(action="SERVICE_QR_CREATED", object_id=str(link.pk)).exists())

        listed = self.client.get("/api/admin/service-qr-links/")
        self.assertEqual(listed.status_code, 200, listed.content)
        row = listed.json()["data"][0]
        self.assertNotIn("token_hash", row)
        self.assertNotIn("service_url", row)
        self.assertNotIn("qr_svg", row)
        self.assertIsNone(service_qr_link_for_token("x" * 40))
        self.assertEqual(service_qr_link_for_token(token).pk, link.pk)

    def test_room_qr_intake_is_idempotent_and_routes_into_the_existing_housekeeping_queue(self):
        _, token = self._issue(room_id=self.room.pk)
        self._public_headers(token)
        context = self.client.get("/api/service-qr/context/")
        self.assertEqual(context.status_code, 200, context.content)
        self.assertEqual(context.json()["data"]["location"], self.room.room_number)
        self.assertNotIn("guest", context.json()["data"])
        self.assertIn("no-store", context["Cache-Control"])

        payload = {
            "category": "HOUSEKEEPING",
            "summary": "Please bring two extra towels",
            "detail": "Thank you.",
            "idempotency_key": "qr-room-towels-request-0001",
        }
        first = self.client.post("/api/service-qr/requests/", payload, format="json")
        self.assertEqual(first.status_code, 201, first.content)
        second = self.client.post("/api/service-qr/requests/", payload, format="json")
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(first.json()["data"]["reference"], second.json()["data"]["reference"])

        service_request = ServiceRequest.objects.get(reference=first.json()["data"]["reference"])
        self.assertEqual(service_request.guest_id, self.guest.pk)
        self.assertEqual(service_request.stay_id, self.stay.pk)
        self.assertEqual(service_request.room_id, self.room.pk)
        self.assertEqual(service_request.channel, ServiceRequest.Channel.QR)
        self.assertEqual(service_request.owner_team, ServiceRequest.OwnerTeam.HOUSEKEEPING)
        self.assertEqual(ServiceRequest.objects.filter(qr_link__isnull=False).count(), 1)
        self.assertTrue(AuditLog.objects.filter(action="SERVICE_QR_REQUEST_CREATED", object_id=str(service_request.pk)).exists())

        self.auth(self.housekeeper)
        queue = self.client.get("/api/admin/service-requests/")
        self.assertEqual(queue.status_code, 200, queue.content)
        self.assertIn(service_request.reference, {row["reference"] for row in queue.json()["data"]})

    def test_table_qr_has_no_synthetic_guest_and_routes_only_to_food_and_beverage(self):
        _, token = self._issue("TABLE", table_number="Lounge 4")
        self._public_headers(token)
        payload = {"summary": "Please send a server", "idempotency_key": "qr-table-server-request-0001"}
        response = self.client.post("/api/service-qr/requests/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        service_request = ServiceRequest.objects.get(reference=response.json()["data"]["reference"])
        self.assertIsNone(service_request.guest_id)
        self.assertIsNone(service_request.stay_id)
        self.assertIsNone(service_request.room_id)
        self.assertEqual(service_request.table_number, "Lounge 4")
        self.assertEqual(service_request.category, ServiceRequest.Category.FOOD_BEVERAGE)
        self.assertEqual(service_request.owner_team, ServiceRequest.OwnerTeam.FOOD_BEVERAGE)
        self.assertEqual(service_request.channel, ServiceRequest.Channel.QR)

        forged = self.client.post(
            "/api/service-qr/requests/",
            {**payload, "idempotency_key": "qr-table-forged-category-0001", "category": "MAINTENANCE"},
            format="json",
        )
        self.assertEqual(forged.status_code, 400, forged.content)
        direct_order = self.client.post(
            "/api/service-qr/requests/",
            {**payload, "idempotency_key": "qr-table-direct-order-0001", "lines": [{"menu_item_id": 1, "quantity": 1}]},
            format="json",
        )
        self.assertEqual(direct_order.status_code, 400, direct_order.content)
        self.assertEqual(ServiceRequest.objects.filter(qr_link__isnull=False).count(), 1)

        self.auth(self.waiter)
        queue = self.client.get("/api/admin/service-requests/")
        self.assertEqual(queue.status_code, 200, queue.content)
        self.assertIn(service_request.reference, {row["reference"] for row in queue.json()["data"]})
        claimed = self.client.post(
            f"/api/admin/service-requests/{service_request.reference}/claim/", {}, format="json"
        )
        self.assertEqual(claimed.status_code, 200, claimed.content)
        updated = self.client.post(
            f"/api/admin/service-requests/{service_request.reference}/status/",
            {"status": "ACKNOWLEDGED", "note": "A server is on the way."},
            format="json",
        )
        self.assertEqual(updated.status_code, 200, updated.content)

    def test_room_qr_requires_one_current_in_house_occupant(self):
        _, token = self._issue(room_id=self.room.pk)
        self.stay.status = Stay.Status.CHECKED_OUT
        self.stay.save(update_fields=["status", "updated_at"])
        self._public_headers(token)
        response = self.client.post(
            "/api/service-qr/requests/",
            {"category": "GENERAL", "summary": "Help", "idempotency_key": "qr-room-vacant-request-0001"},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(ServiceRequest.objects.filter(qr_link__isnull=False).count(), 0)

    def test_room_qr_rejects_ambiguous_multiple_current_occupants(self):
        other_guest = make_guest("service-qr.other@example.com")
        other_booking = make_booking(
            other_guest,
            self.room_type,
            [self.room],
            check_in=hotel_today(),
            check_out=hotel_today() + timedelta(days=2),
            total="20000.00",
        )
        other_stay = Stay.objects.create(
            reference=f"STY-{other_booking.booking_reference}",
            booking=other_booking,
            guest=other_guest,
            status=Stay.Status.IN_HOUSE,
            expected_arrival=other_booking.check_in,
            expected_departure=other_booking.check_out,
        )
        StayRoom.objects.create(stay=other_stay, room=self.room, actual_check_in_at=timezone.now())
        _, token = self._issue(room_id=self.room.pk)
        self._public_headers(token)
        response = self.client.post(
            "/api/service-qr/requests/",
            {"category": "GENERAL", "summary": "Help", "idempotency_key": "qr-room-ambiguous-request-0001"},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(ServiceRequest.objects.filter(qr_link__isnull=False).count(), 0)

    def test_rotation_revocation_and_least_privilege_are_enforced(self):
        data, old_token = self._issue("TABLE", table_number="Bar 2")
        link = ServiceQRLink.objects.get(reference=data["reference"])
        rotated = self.client.post(f"/api/admin/service-qr-links/{link.reference}/rotate/", {}, format="json")
        self.assertEqual(rotated.status_code, 200, rotated.content)
        new_token = urlsplit(rotated.json()["data"]["service_url"]).fragment.removeprefix("token=")
        self._public_headers(old_token)
        self.assertEqual(self.client.get("/api/service-qr/context/").status_code, 404)
        self._public_headers(new_token)
        self.assertEqual(self.client.get("/api/service-qr/context/").status_code, 200)

        self.auth(self.manager)
        revoked = self.client.post(f"/api/admin/service-qr-links/{link.reference}/revoke/", {}, format="json")
        self.assertEqual(revoked.status_code, 200, revoked.content)
        self._public_headers(new_token)
        self.assertEqual(self.client.get("/api/service-qr/context/").status_code, 404)

        self.auth(self.waiter)
        denied = self.client.get("/api/admin/service-qr-links/")
        self.assertEqual(denied.status_code, 403, denied.content)
