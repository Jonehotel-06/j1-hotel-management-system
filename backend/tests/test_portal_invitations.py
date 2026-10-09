"""Automatic guest-portal invitation after check-in, staff fallback, and portal service catalogue."""
from datetime import timedelta
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.core.utils import hotel_today
from apps.notifications.models import EmailLog
from apps.portal.invitations import INVITATION_KIND, mask_email, resend_cooldown
from apps.portal.models import GuestPortalInvitation, PortalAccessChallenge
from apps.portal.services import hash_secret
from apps.stays.models import Stay, StayRoom
from tests.base import BaseAPITestCase
from tests.factories import make_booking, make_guest, make_room, make_room_type, make_staff

PORTAL_TOKEN = "portal-invite-token-0123456789-abcdefghijklmnopqrstuvwxyz"


class PortalInvitationTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.receptionist = make_staff("invite.reception@staff.dev", role=User.Role.RECEPTIONIST)
        self.housekeeper = make_staff("invite.housekeeping@staff.dev", role=User.Role.HOUSEKEEPING)
        self.guest = make_guest("invite.guest@example.com")
        self.room_type = make_room_type("Invitation suite", price="10000.00")
        self.room = make_room(self.room_type, "INV-01")
        self.booking = make_booking(
            self.guest, self.room_type, [self.room], check_in=hotel_today(),
            check_out=hotel_today() + timedelta(days=2), total="20000.00",
        )

    def _check_in(self):
        self.auth(self.receptionist)
        return self.client.post(f"/api/admin/bookings/{self.booking.booking_reference}/check-in/", {}, format="json")

    # --- automatic invitation ---------------------------------------------
    def test_successful_check_in_sends_one_invitation_on_commit(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self._check_in()
        self.assertEqual(response.status_code, 200, response.content)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CHECKED_IN)
        invitation = GuestPortalInvitation.objects.get()
        self.assertEqual(invitation.status, GuestPortalInvitation.Status.SENT)
        self.assertEqual(invitation.email_log.kind, INVITATION_KIND)
        self.assertEqual(invitation.email_log.status, EmailLog.Status.SENT)
        self.assertEqual(EmailLog.objects.filter(kind=INVITATION_KIND).count(), 1)

    def test_invitation_body_contains_portal_link_but_no_token(self):
        with self.captureOnCommitCallbacks(execute=True):
            self._check_in()
        log = GuestPortalInvitation.objects.get().email_log
        self.assertEqual(log.to_email, self.guest.email)
        self.assertIn("/portal/login.html", log.body)
        self.assertNotIn("token", log.body.lower())

    def test_rescheduling_is_idempotent(self):
        with self.captureOnCommitCallbacks(execute=True):
            self._check_in()
        from apps.portal.invitations import _create_and_deliver, schedule_portal_invitation
        stay = Stay.objects.get(booking=self.booking)
        with self.captureOnCommitCallbacks(execute=True):
            schedule_portal_invitation(stay.pk)
            _create_and_deliver(stay.pk, None)
        self.assertEqual(GuestPortalInvitation.objects.filter(stay=stay).count(), 1)
        self.assertEqual(EmailLog.objects.filter(kind=INVITATION_KIND).count(), 1)

    def test_notification_failure_never_undoes_check_in(self):
        with mock.patch("apps.portal.invitations.send_email_safe", side_effect=RuntimeError("provider down")):
            with self.captureOnCommitCallbacks(execute=True):
                response = self._check_in()
        self.assertEqual(response.status_code, 200, response.content)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CHECKED_IN)
        self.assertEqual(Stay.objects.get(booking=self.booking).status, Stay.Status.IN_HOUSE)
        invitation = GuestPortalInvitation.objects.get()
        self.assertEqual(invitation.status, GuestPortalInvitation.Status.FAILED)
        self.assertEqual(invitation.last_error_code, "DELIVERY_EXCEPTION")

    def test_provider_rejection_is_recorded_as_failed_not_sent(self):
        failed_log = EmailLog.objects.create(
            kind=INVITATION_KIND, to_email=self.guest.email, subject="x", status=EmailLog.Status.FAILED,
        )
        with mock.patch("apps.portal.invitations.send_email_safe", return_value=failed_log):
            with self.captureOnCommitCallbacks(execute=True):
                self._check_in()
        self.assertEqual(GuestPortalInvitation.objects.get().status, GuestPortalInvitation.Status.FAILED)

    def test_guest_without_email_is_recorded_as_no_email_and_check_in_still_succeeds(self):
        self.guest.email = ""
        self.guest.save(update_fields=["email"])
        with self.captureOnCommitCallbacks(execute=True):
            response = self._check_in()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(GuestPortalInvitation.objects.get().status, GuestPortalInvitation.Status.NO_EMAIL)
        self.assertFalse(EmailLog.objects.filter(kind=INVITATION_KIND).exists())

    def test_rolled_back_check_in_schedules_nothing(self):
        with mock.patch("apps.bookings.services.booking_service.log_action", side_effect=RuntimeError("boom")):
            with self.captureOnCommitCallbacks(execute=True):
                try:
                    self._check_in()
                except RuntimeError:
                    pass
        self.assertFalse(GuestPortalInvitation.objects.exists())
        self.assertFalse(EmailLog.objects.filter(kind=INVITATION_KIND).exists())

    # --- staff fallback ----------------------------------------------------
    def _in_house_stay(self):
        with self.captureOnCommitCallbacks(execute=True):
            self._check_in()
        return Stay.objects.get(booking=self.booking)

    def test_staff_status_masks_recipient_and_copy_text_has_no_token(self):
        self._in_house_stay()
        self.auth(self.receptionist)
        response = self.client.get(f"/api/admin/bookings/{self.booking.booking_reference}/portal-invitation/")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["status"], "SENT")
        self.assertNotIn("invite.guest@example.com", str(data))
        self.assertTrue(data["recipient_hint"].endswith("@example.com"))
        self.assertIn("/portal/login.html", data["copy_text"])
        self.assertNotIn("token", data["copy_text"].lower())

    def test_resend_is_rate_limited_after_a_successful_send(self):
        self._in_house_stay()
        self.auth(self.receptionist)
        url = f"/api/admin/bookings/{self.booking.booking_reference}/portal-invitation/"
        response = self.client.post(url, {}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "INVITATION_RECENTLY_SENT")

    def test_resend_after_cooldown_delivers_again(self):
        self._in_house_stay()
        GuestPortalInvitation.objects.update(last_attempt_at=timezone.now() - resend_cooldown() - timedelta(minutes=1))
        self.auth(self.receptionist)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"/api/admin/bookings/{self.booking.booking_reference}/portal-invitation/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(EmailLog.objects.filter(kind=INVITATION_KIND).count(), 2)
        self.assertEqual(GuestPortalInvitation.objects.get().attempts, 2)

    def test_resend_is_denied_to_roles_without_stay_check_in(self):
        self._in_house_stay()
        self.auth(self.housekeeper)
        response = self.client.post(f"/api/admin/bookings/{self.booking.booking_reference}/portal-invitation/", {}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_mask_email_hides_local_part(self):
        self.assertEqual(mask_email("ngozi.eze@example.com"), "ng*******@example.com")
        self.assertEqual(mask_email("ab@example.com"), "a*@example.com")
        self.assertEqual(mask_email("not-an-email"), "")


class PortalServiceCatalogTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.email = "catalog.guest@example.com"
        self.guest = make_guest(self.email)
        self.room_type = make_room_type("Catalog suite", price="10000.00")
        self.room = make_room(self.room_type, "CAT-01")
        self.booking = make_booking(
            self.guest, self.room_type, [self.room], check_in=hotel_today(),
            check_out=hotel_today() + timedelta(days=2), total="20000.00",
        )
        self.stay = Stay.objects.create(
            reference=f"STY-{self.booking.booking_reference}", booking=self.booking, guest=self.guest,
            status=Stay.Status.IN_HOUSE, expected_arrival=hotel_today(), expected_departure=hotel_today() + timedelta(days=2),
        )
        StayRoom.objects.create(stay=self.stay, room=self.room, actual_check_in_at=timezone.now())
        self.session_token = self._session()

    def _session(self):
        token = "portal-catalog-token-0123456789-abcdefghijklmnopqrstuvwxyz"
        PortalAccessChallenge.objects.create(
            email=self.email, token_hash=hash_secret(token), expires_at=timezone.now() + timedelta(minutes=15),
        )
        response = self.client.post("/api/portal/auth/consume/", {"token": token}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]["session_token"]

    def test_catalog_requires_a_portal_session(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get("/api/portal/requests/catalog/").status_code, 401)

    def test_catalog_lists_services_with_department_routing(self):
        response = self.client.get("/api/portal/requests/catalog/", HTTP_X_PORTAL_SESSION=self.session_token)
        self.assertEqual(response.status_code, 200, response.content)
        services = {item["category"]: item for item in response.json()["data"]["services"]}
        self.assertEqual(services["HOUSEKEEPING"]["department"], "HOUSEKEEPING")
        self.assertEqual(services["MAINTENANCE"]["department"], "MAINTENANCE")
        self.assertEqual(services["FOOD_BEVERAGE"]["department_label"], "Food & beverage")

    @override_settings(PORTAL_ENABLED_SERVICE_CATEGORIES=["HOUSEKEEPING", "MAINTENANCE"])
    def test_disabled_services_are_not_offered(self):
        response = self.client.get("/api/portal/requests/catalog/", HTTP_X_PORTAL_SESSION=self.session_token)
        categories = [item["category"] for item in response.json()["data"]["services"]]
        self.assertEqual(sorted(categories), ["HOUSEKEEPING", "MAINTENANCE"])
