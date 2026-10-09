"""Service QR expiry, revocation, rotation and fail-safe scan behaviour."""
from datetime import timedelta
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.guest_services.models import ServiceQRLink, ServiceRequest
from apps.guest_services.services.qr_service import (
    STATE_ACTIVE, STATE_EXPIRED, STATE_INVALID, STATE_REVOKED, public_service_url, service_qr_link_state,
)
from apps.stays.models import Stay, StayRoom
from tests.base import BaseAPITestCase
from tests.factories import make_booking, make_guest, make_room, make_room_type, make_staff


class ServiceQRLifecycleTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("qr-life.manager@staff.dev", role=User.Role.MANAGER)
        self.guest = make_guest("qr-life.guest@example.com")
        self.room_type = make_room_type("QR lifecycle suite", price="10000.00")
        self.room = make_room(self.room_type, "QL-01")
        booking = make_booking(
            self.guest, self.room_type, [self.room],
            check_in=hotel_today(), check_out=hotel_today() + timedelta(days=2), total="20000.00",
        )
        self.stay = Stay.objects.create(
            reference=f"STY-{booking.booking_reference}", booking=booking, guest=self.guest,
            status=Stay.Status.IN_HOUSE, expected_arrival=booking.check_in, expected_departure=booking.check_out,
        )
        StayRoom.objects.create(stay=self.stay, room=self.room, actual_check_in_at=timezone.now())

    # helpers -----------------------------------------------------------------
    def _issue_room(self):
        self.auth(self.manager)
        response = self.client.post(
            "/api/admin/service-qr-links/", {"target_type": "ROOM", "room_id": self.room.pk}, format="json",
        )
        self.assertIn(response.status_code, (200, 201), response.content)
        data = response.json()["data"]
        return data, urlsplit(data["service_url"]).fragment.removeprefix("token=")

    def _scan_context(self, token):
        self.client.force_authenticate(user=None)
        return self.client.get("/api/service-qr/context/", HTTP_X_SERVICE_QR_TOKEN=token)

    def _submit(self, token, key="qr-life-lifecycle-0001"):
        self.client.force_authenticate(user=None)
        return self.client.post(
            "/api/service-qr/requests/",
            {"summary": "Extra towels", "idempotency_key": key},
            format="json", HTTP_X_SERVICE_QR_TOKEN=token,
        )

    def _expire(self, reference):
        ServiceQRLink.objects.filter(reference=reference).update(expires_at=timezone.now() - timedelta(minutes=1))

    # tests -------------------------------------------------------------------
    def test_new_links_get_a_validity_window_from_settings(self):
        with override_settings(SERVICE_QR_LINK_TTL_HOURS=2):
            data, _ = self._issue_room()
        link = ServiceQRLink.objects.get(reference=data["reference"])
        remaining = link.expires_at - timezone.now()
        self.assertTrue(timedelta(hours=1, minutes=55) < remaining <= timedelta(hours=2))
        self.assertFalse(data["is_expired"])
        self.assertIsNotNone(data["expires_at"])

    def test_expired_token_is_refused_with_distinct_code_on_context_and_submit(self):
        data, token = self._issue_room()
        self._expire(data["reference"])
        context = self._scan_context(token)
        self.assertEqual(context.status_code, 410)
        self.assertEqual(context.json()["code"], "QR_LINK_EXPIRED")
        submit = self._submit(token)
        self.assertEqual(submit.status_code, 410)
        self.assertFalse(ServiceRequest.objects.filter(summary="Extra towels").exists())

    def test_expired_scan_does_not_update_last_used(self):
        data, token = self._issue_room()
        self._expire(data["reference"])
        self._scan_context(token)
        self.assertIsNone(ServiceQRLink.objects.get(reference=data["reference"]).last_used_at)

    def test_legacy_link_without_expiry_fails_closed(self):
        data, token = self._issue_room()
        ServiceQRLink.objects.filter(reference=data["reference"]).update(expires_at=None)
        self.assertEqual(self._scan_context(token).status_code, 410)

    def test_revoked_link_is_refused_as_not_active_even_before_expiry(self):
        data, token = self._issue_room()
        self.auth(self.manager)
        self.client.post(f"/api/admin/service-qr-links/{data['reference']}/revoke/")
        response = self._scan_context(token)
        self.assertEqual(response.status_code, 404)
        self.assertNotEqual(response.json().get("code"), "QR_LINK_EXPIRED")
        self.assertEqual(self._submit(token).status_code, 404)

    def test_unknown_and_malformed_tokens_fail_safely(self):
        self._issue_room()
        self.assertEqual(self._scan_context("x" * 40).status_code, 404)
        self.assertEqual(self._scan_context("short").status_code, 404)
        self.assertEqual(self._scan_context("").status_code, 404)

    def test_rotating_an_expired_link_issues_a_working_token_and_kills_the_old_one(self):
        data, old_token = self._issue_room()
        self._expire(data["reference"])
        self.auth(self.manager)
        rotated = self.client.post(f"/api/admin/service-qr-links/{data['reference']}/rotate/")
        self.assertEqual(rotated.status_code, 200)
        new_token = urlsplit(rotated.json()["data"]["service_url"]).fragment.removeprefix("token=")
        self.assertNotEqual(old_token, new_token)
        self.assertEqual(self._scan_context(old_token).status_code, 404)
        self.assertEqual(self._scan_context(new_token).status_code, 200)

    def test_creating_again_after_expiry_reactivates_with_fresh_expiry(self):
        data, _ = self._issue_room()
        self._expire(data["reference"])
        self.auth(self.manager)
        response = self.client.post(
            "/api/admin/service-qr-links/", {"target_type": "ROOM", "room_id": self.room.pk}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        link = ServiceQRLink.objects.get(reference=data["reference"])
        self.assertGreater(link.expires_at, timezone.now())

    def test_creating_while_active_still_requires_rotation(self):
        self._issue_room()
        self.auth(self.manager)
        response = self.client.post(
            "/api/admin/service-qr-links/", {"target_type": "ROOM", "room_id": self.room.pk}, format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_staff_list_exposes_expiry_state(self):
        data, _ = self._issue_room()
        self._expire(data["reference"])
        self.auth(self.manager)
        listing = self.client.get("/api/admin/service-qr-links/").json()["data"]
        row = next(item for item in listing if item["reference"] == data["reference"])
        self.assertTrue(row["is_expired"])
        self.assertIn("expires_at", row)

    def test_state_resolver_reports_each_outcome(self):
        data, token = self._issue_room()
        self.assertEqual(service_qr_link_state(token)[1], STATE_ACTIVE)
        self.assertEqual(service_qr_link_state("y" * 40)[1], STATE_INVALID)
        self._expire(data["reference"])
        self.assertEqual(service_qr_link_state(token)[1], STATE_EXPIRED)
        ServiceQRLink.objects.filter(reference=data["reference"]).update(is_active=False)
        self.assertEqual(service_qr_link_state(token)[1], STATE_REVOKED)

    def test_public_url_requires_https_when_the_production_rule_is_enabled(self):
        with override_settings(SERVICE_QR_REQUIRE_HTTPS=True, SERVICE_QR_FRONTEND_URL="http://insecure.example"):
            with self.assertRaises(ValidationError):
                public_service_url("t" * 40)
        with override_settings(SERVICE_QR_REQUIRE_HTTPS=True, SERVICE_QR_FRONTEND_URL="https://guest.example/"):
            self.assertEqual(public_service_url("t" * 40), "https://guest.example/qr-service.html#token=" + "t" * 40)
        with override_settings(SERVICE_QR_REQUIRE_HTTPS=False, SERVICE_QR_FRONTEND_URL="http://localhost:8080"):
            self.assertTrue(public_service_url("t" * 40).startswith("http://localhost:8080/qr-service.html#token="))
