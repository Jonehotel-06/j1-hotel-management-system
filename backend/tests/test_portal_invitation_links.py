# tests/test_portal_invitation_links.py
"""The guest-portal invitation carries the guest's ACTUAL access link (C3).

Before the repair the invitation email only pointed at the sign-in page and
promised that "we will send a one-time sign-in link" — the guest who followed
the email still had to request a second email. Critical repair C3:

* the check-in invitation and every room-guest invitation embed a magic link
  minted for the RECIPIENT'S OWN address (SHA-256 digest stored; the raw
  token exists only inside the email body — never a log or audit row);
* the invitation link stays redeemable for the length of the stay — it may
  be opened on several devices — and dies at checkout, cancellation or when
  a newer invitation supersedes it;
* requested sign-in links keep their existing contract: single-use, one
  outstanding at a time per address, and they never touch invitation links;
* delivery failures are recorded on the invitation and the staff resend
  re-mints a fresh, working link.
"""
import re
from datetime import timedelta
from unittest import mock

from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.bookings.models import RoomOccupant
from apps.bookings.services.booking_service import check_in_booking
from apps.core.utils import hotel_today
from apps.notifications.models import EmailLog
from apps.portal.invitations import resend_portal_invitation
from apps.portal.models import GuestPortalInvitation, PortalAccessChallenge, PortalSession
from apps.portal.services import (
    consume_access_challenge,
    request_access_challenge,
)

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type, make_staff

TOKEN_RE = re.compile(r"[?&]token=([A-Za-z0-9_\-]+)")


def _token_from(body: str) -> str:
    match = TOKEN_RE.search(body or "")
    assert match, f"no token= in email body: {body[:200]!r}"
    return match.group(1)


class PortalInvitationLinkTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.receptionist = make_staff("invite-desk@staff.dev", role=User.Role.RECEPTIONIST)
        self.guest = make_guest("invite.guest@example.com")
        self.room_type = make_room_type("Invite suite", price="10000.00")
        self.room = make_room(self.room_type, "IR-01")
        self.booking = make_booking(
            self.guest, self.room_type, [self.room],
            check_in=hotel_today(), check_out=hotel_today() + timedelta(days=3),
            total="30000.00",
        )

    def _check_in(self):
        with self.captureOnCommitCallbacks(execute=True):
            check_in_booking(booking=self.booking, staff_user=self.receptionist)
        return EmailLog.objects.filter(kind="PORTAL_INVITATION").first()

    def test_the_invitation_email_link_signs_the_guest_in(self):
        log = self._check_in()
        self.assertIsNotNone(log)
        token = _token_from(log.body)

        session, raw_session_token = consume_access_challenge(raw_token=token)
        self.assertEqual(session.email, self.guest.email.lower())
        self.assertTrue(raw_session_token)
        self.assertEqual(
            session.authentication_method, PortalSession.AuthenticationMethod.EMAIL_MAGIC_LINK
        )

    def test_the_invitation_link_stays_redeemable_for_the_stay(self):
        """Valid through the stay: opening it on a second device also works."""
        log = self._check_in()
        token = _token_from(log.body)

        first, _ = consume_access_challenge(raw_token=token)
        second, _ = consume_access_challenge(raw_token=token)
        self.assertNotEqual(first.pk, second.pk)

        # …and it is the one challenge that stays open: no consumption burn.
        challenge = PortalAccessChallenge.objects.get()
        self.assertEqual(challenge.purpose, PortalAccessChallenge.Purpose.INVITATION)
        self.assertIsNone(challenge.consumed_at)

    def test_the_invitation_link_expires_at_checkout(self):
        log = self._check_in()
        token = _token_from(log.body)
        consume_access_challenge(raw_token=token)

        from apps.portal.services import end_portal_access_for_checked_out_stay
        from apps.stays.models import Stay

        stay = self.booking.stay
        stay.status = Stay.Status.CHECKED_OUT
        stay.save(update_fields=["status", "updated_at"])
        end_portal_access_for_checked_out_stay(stay)

        with self.assertRaises(AuthenticationFailed):
            consume_access_challenge(raw_token=token)

    def test_a_cancellation_ends_the_invitation_link(self):
        log = self._check_in()
        token = _token_from(log.body)

        from apps.bookings.models import Booking

        self.booking.status = Booking.Status.CANCELLED
        self.booking.save(update_fields=["status", "updated_at"])

        with self.assertRaises(AuthenticationFailed):
            consume_access_challenge(raw_token=token)

    def test_requested_sign_in_links_stay_single_use_and_leave_invitations_alone(self):
        log = self._check_in()
        invite_token = _token_from(log.body)

        with self.captureOnCommitCallbacks(execute=True):
            request_access_challenge(email=self.guest.email)
        request_log = EmailLog.objects.filter(kind="GENERIC").order_by("-pk").first()
        requested_token = _token_from(request_log.body)

        # Requested link: exactly one use.
        consume_access_challenge(raw_token=requested_token)
        with self.assertRaises(AuthenticationFailed):
            consume_access_challenge(raw_token=requested_token)

        # The requested link did NOT supersede the invitation link.
        session, _ = consume_access_challenge(raw_token=invite_token)
        self.assertEqual(session.email, self.guest.email.lower())

        # A second request supersedes the first requested link only.
        with self.captureOnCommitCallbacks(execute=True):
            request_access_challenge(email=self.guest.email)
        newer = _token_from(EmailLog.objects.filter(kind="GENERIC").order_by("-pk").first().body)
        self.assertNotEqual(newer, requested_token)
        with self.assertRaises(AuthenticationFailed):
            consume_access_challenge(raw_token=requested_token)
        consume_access_challenge(raw_token=newer)  # the newest requested link works

    def test_room_guests_receive_their_own_links(self):
        assignment = self.booking.room_assignments.first()
        occupant = RoomOccupant.objects.create(
            booking_room=assignment, full_name="Bala Aliyu", email="invite.occupant@example.com",
        )
        self._check_in()
        occupant_log = EmailLog.objects.filter(
            kind="GENERIC", to_email="invite.occupant@example.com"
        ).first()
        self.assertIsNotNone(occupant_log, "each room guest must be emailed")
        token = _token_from(occupant_log.body)

        session, _ = consume_access_challenge(raw_token=token)
        # The link is bound to the OCCUPANT's address — never the booker's.
        self.assertEqual(session.email, "invite.occupant@example.com")
        self.assertNotEqual(session.email, self.guest.email.lower())

        occupant.refresh_from_db()
        self.assertIsNotNone(occupant.portal_invited_at)

    def test_no_raw_token_reaches_the_audit_trail(self):
        log = self._check_in()
        token = _token_from(log.body)

        for entry in AuditLog.objects.all():
            blob = f"{entry.action} {entry.summary} {entry.metadata} {entry.changes}"
            self.assertNotIn(token, blob, f"audit row {entry.pk} leaked the raw token")
        # Only the digest is stored.
        challenge = PortalAccessChallenge.objects.get()
        self.assertNotEqual(challenge.token_hash, token)
        self.assertNotIn(token, str(challenge.metadata))

    def test_a_failed_delivery_is_recorded_and_the_resend_recovers(self):
        with mock.patch(
            "apps.portal.invitations.send_email_safe", side_effect=RuntimeError("smtp down")
        ):
            self._check_in()

        invitation = GuestPortalInvitation.objects.get()
        self.assertEqual(invitation.status, GuestPortalInvitation.Status.FAILED)
        self.assertEqual(invitation.last_error_code, "DELIVERY_EXCEPTION")

        # The staff resend re-mints a fresh, WORKING link (safe retry).
        resend_portal_invitation(stay=self.booking.stay, actor=self.receptionist)
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, GuestPortalInvitation.Status.SENT)
        log = EmailLog.objects.filter(kind="PORTAL_INVITATION").order_by("-pk").first()
        session, _ = consume_access_challenge(raw_token=_token_from(log.body))
        self.assertEqual(session.email, self.guest.email.lower())
