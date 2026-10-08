# tests/test_reviews.py
"""Guest review system — eligibility, validation, security, statistics.

Covers the strict rules:
* only a genuine COMPLETED (checked-out) stay may be reviewed;
* verification requires the booking reference PLUS proof of ownership
  (guest email / owning account / guest access token) — never the reference
  alone (IDOR defence);
* one review per booking (database-enforced);
* review management is administrator-only — managers and receptionists are
  rejected server-side;
* no public listing endpoint exists;
* review content is stored as plain text (stored-XSS defence).
"""
from datetime import timedelta

from django.urls import reverse

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.core.utils import hotel_today
from apps.notifications.models import Notification
from apps.audit.models import AuditLog
from apps.reviews.models import Review

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room_type, make_staff, make_user

VERIFY_URL = "/api/reviews/verify/"
SUBMIT_URL = "/api/reviews/"
ADMIN_LIST_URL = "/api/admin/reviews/"
ADMIN_STATS_URL = "/api/admin/reviews/stats/"


class ReviewTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.room_type = make_room_type()
        self.guest = make_guest(email="ada@guest.dev")
        today = hotel_today()
        self.booking = make_booking(
            self.guest, self.room_type,
            check_in=today - timedelta(days=4), check_out=today - timedelta(days=1),
            status=Booking.Status.CHECKED_OUT,
        )

    def verify(self, reference=None, email=None):
        return self.client.post(VERIFY_URL, {
            "booking_reference": reference if reference is not None else self.booking.booking_reference,
            "email": email if email is not None else self.guest.email,
        }, format="json")

    def submit(self, *, reference=None, email=None, rating=5, comment="A calm, spotless and truly restful stay."):
        return self.client.post(SUBMIT_URL, {
            "booking_reference": reference if reference is not None else self.booking.booking_reference,
            "email": email if email is not None else self.guest.email,
            "rating": rating,
            "comment": comment,
        }, format="json")


class ReviewVerificationTests(ReviewTestBase):
    def test_verify_completed_stay_returns_safe_summary(self):
        res = self.verify()
        self.assertEqual(res.status_code, 200)
        data = res.data["data"]
        self.assertEqual(data["booking_reference"], self.booking.booking_reference)
        self.assertEqual(data["room_type_name"], self.room_type.name)
        self.assertFalse(data["already_reviewed"])
        # Never leaks contact/money data.
        for forbidden in ("email", "phone", "total_amount", "amount_paid"):
            self.assertNotIn(forbidden, data)

    def test_reference_alone_is_not_enough(self):
        res = self.verify(email="")
        self.assertEqual(res.status_code, 404)

    def test_wrong_email_is_rejected_as_not_found(self):
        res = self.verify(email="attacker@evil.dev")
        self.assertEqual(res.status_code, 404)

    def test_unknown_reference_gives_identical_error(self):
        res_unknown = self.verify(reference="J1-NOPE-00000")
        res_wrong_email = self.verify(email="attacker@evil.dev")
        self.assertEqual(res_unknown.status_code, 404)
        # Same message for both failures — references cannot be probed.
        self.assertEqual(res_unknown.data["message"], res_wrong_email.data["message"])

    def test_owning_account_can_verify_without_email(self):
        user = make_user(email="owner@guest.dev")
        self.guest.user = user
        self.guest.save()
        self.auth(user)
        res = self.verify(email="")
        self.assertEqual(res.status_code, 200)

    def test_other_account_cannot_verify_someone_elses_booking(self):
        stranger = make_user(email="stranger@guest.dev")
        self.auth(stranger)
        res = self.verify(email="")
        self.assertEqual(res.status_code, 404)

    def test_guest_access_token_verifies(self):
        token = self.booking.issue_guest_access_token()
        res = self.client.post(
            VERIFY_URL,
            {"booking_reference": self.booking.booking_reference, "email": ""},
            format="json", headers={"X-Guest-Access-Token": token},
        )
        self.assertEqual(res.status_code, 200)


class ReviewEligibilityTests(ReviewTestBase):
    def _booking_with_status(self, status, email="other@guest.dev"):
        guest = make_guest(email=email)
        return guest, make_booking(guest, self.room_type, status=status)

    def test_completed_stay_can_review(self):
        res = self.submit()
        self.assertEqual(res.status_code, 201)
        self.assertEqual(Review.objects.count(), 1)
        review = Review.objects.get()
        self.assertEqual(review.booking, self.booking)
        self.assertEqual(review.guest_name, self.guest.full_name)

    def test_pending_booking_cannot_review(self):
        guest, booking = self._booking_with_status(Booking.Status.PENDING)
        res = self.submit(reference=booking.booking_reference, email=guest.email)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "STAY_NOT_STARTED")

    def test_confirmed_future_booking_cannot_review(self):
        guest, booking = self._booking_with_status(Booking.Status.CONFIRMED)
        res = self.submit(reference=booking.booking_reference, email=guest.email)
        self.assertEqual(res.status_code, 409)

    def test_cancelled_booking_cannot_review(self):
        guest, booking = self._booking_with_status(Booking.Status.CANCELLED)
        res = self.submit(reference=booking.booking_reference, email=guest.email)
        self.assertEqual(res.status_code, 409)

    def test_expired_booking_cannot_review(self):
        guest, booking = self._booking_with_status(Booking.Status.EXPIRED)
        res = self.submit(reference=booking.booking_reference, email=guest.email)
        self.assertEqual(res.status_code, 409)

    def test_checked_in_booking_can_review(self):
        # A guest becomes eligible to review as soon as they are checked in.
        guest, booking = self._booking_with_status(Booking.Status.CHECKED_IN)
        res = self.submit(reference=booking.booking_reference, email=guest.email)
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(Review.objects.filter(booking=booking).count(), 1)


class ReviewValidationTests(ReviewTestBase):
    def test_rating_below_one_rejected(self):
        res = self.submit(rating=0)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(Review.objects.count(), 0)

    def test_rating_above_five_rejected(self):
        res = self.submit(rating=6)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(Review.objects.count(), 0)

    def test_empty_comment_rejected(self):
        res = self.submit(comment="")
        self.assertEqual(res.status_code, 400)

    def test_too_short_comment_rejected(self):
        res = self.submit(comment="Nice.")
        self.assertEqual(res.status_code, 400)

    def test_whitespace_only_comment_rejected(self):
        res = self.submit(comment="                     ")
        self.assertEqual(res.status_code, 400)

    def test_excessively_long_comment_rejected(self):
        res = self.submit(comment="x" * 2501)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(Review.objects.count(), 0)

    def test_comment_whitespace_trimmed(self):
        res = self.submit(comment="   A calm and truly restful stay.   ")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(Review.objects.get().comment, "A calm and truly restful stay.")

    def test_html_is_stripped_from_comment(self):
        res = self.submit(comment='<script>alert("xss")</script>Wonderful rooms and warm staff!')
        self.assertEqual(res.status_code, 201)
        review = Review.objects.get()
        self.assertNotIn("<script>", review.comment)
        self.assertNotIn("</script>", review.comment)
        self.assertIn("Wonderful rooms", review.comment)

    def test_honeypot_submission_is_silently_dropped(self):
        res = self.client.post(SUBMIT_URL, {
            "booking_reference": self.booking.booking_reference,
            "email": self.guest.email,
            "rating": 5,
            "comment": "A calm, spotless and truly restful stay.",
            "website": "http://spam.example",
        }, format="json")
        self.assertEqual(res.status_code, 201)   # bot sees success…
        self.assertEqual(Review.objects.count(), 0)  # …but nothing is stored


class DuplicateReviewTests(ReviewTestBase):
    def test_same_booking_cannot_review_twice(self):
        self.assertEqual(self.submit().status_code, 201)
        res = self.submit(rating=1, comment="Trying to overwrite my previous review!")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "ALREADY_REVIEWED")
        self.assertEqual(Review.objects.count(), 1)
        self.assertEqual(Review.objects.get().rating, 5)  # original untouched

    def test_verify_flags_already_reviewed(self):
        self.submit()
        res = self.verify()
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["data"]["already_reviewed"])

    def test_database_constraint_is_the_backstop(self):
        Review.objects.create(
            booking=self.booking, guest=self.guest, guest_name="Ada Obi",
            rating=4, comment="Direct row for constraint check.",
        )
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Review.objects.create(
                    booking=self.booking, guest=self.guest, guest_name="Ada Obi",
                    rating=2, comment="Should be impossible at the DB level.",
                )


class ReviewNotificationAuditTests(ReviewTestBase):
    def test_new_review_notifies_admins_only(self):
        admin = make_staff("admin@jone.dev", role=User.Role.ADMIN)
        manager = make_staff("manager@jone.dev", role=User.Role.MANAGER)
        receptionist = make_staff("reception@jone.dev", role=User.Role.RECEPTIONIST)
        self.submit()
        self.assertTrue(Notification.objects.filter(recipient=admin, type="REVIEW_NEW").exists())
        self.assertFalse(Notification.objects.filter(recipient=manager, type="REVIEW_NEW").exists())
        self.assertFalse(Notification.objects.filter(recipient=receptionist, type="REVIEW_NEW").exists())

    def test_submission_is_audited(self):
        self.submit()
        self.assertTrue(AuditLog.objects.filter(action="REVIEW_SUBMITTED").exists())


class ReviewAdminPermissionTests(ReviewTestBase):
    def setUp(self):
        super().setUp()
        self.submit()
        self.review = Review.objects.get()
        self.admin = make_staff("admin@jone.dev", role=User.Role.ADMIN)
        self.manager = make_staff("manager@jone.dev", role=User.Role.MANAGER)
        self.receptionist = make_staff("reception@jone.dev", role=User.Role.RECEPTIONIST)
        self.guest_user = make_user(email="guestacct@jone.dev")

    def test_anonymous_cannot_list_reviews(self):
        self.unauth()
        # 401 (no credentials) — either way, no data leaves the endpoint.
        self.assertIn(self.client.get(ADMIN_LIST_URL).status_code, (401, 403))

    def test_guest_account_cannot_list_reviews(self):
        self.auth(self.guest_user)
        self.assertEqual(self.client.get(ADMIN_LIST_URL).status_code, 403)

    def test_receptionist_cannot_access_reviews(self):
        self.auth(self.receptionist)
        self.assertEqual(self.client.get(ADMIN_LIST_URL).status_code, 403)
        self.assertEqual(self.client.get(ADMIN_STATS_URL).status_code, 403)
        self.assertEqual(self.client.get(f"{ADMIN_LIST_URL}{self.review.pk}/").status_code, 403)

    def test_manager_cannot_access_reviews(self):
        self.auth(self.manager)
        self.assertEqual(self.client.get(ADMIN_LIST_URL).status_code, 403)
        self.assertEqual(self.client.get(ADMIN_STATS_URL).status_code, 403)
        self.assertEqual(
            self.client.patch(f"{ADMIN_LIST_URL}{self.review.pk}/", {"status": "REVIEWED"},
                              format="json").status_code, 403)
        self.assertEqual(self.client.delete(f"{ADMIN_LIST_URL}{self.review.pk}/").status_code, 403)

    def test_admin_can_list_and_read(self):
        self.auth(self.admin)
        res = self.client.get(ADMIN_LIST_URL)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.data["data"]), 1)
        detail = self.client.get(f"{ADMIN_LIST_URL}{self.review.pk}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["data"]["booking_reference"], self.booking.booking_reference)

    def test_admin_can_mark_reviewed_with_notes_and_it_is_audited(self):
        self.auth(self.admin)
        res = self.client.patch(
            f"{ADMIN_LIST_URL}{self.review.pk}/",
            {"status": "REVIEWED", "internal_notes": "Called the guest to thank them."},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, Review.Status.REVIEWED)
        self.assertEqual(self.review.reviewed_by, self.admin)
        self.assertIsNotNone(self.review.reviewed_at)
        self.assertTrue(AuditLog.objects.filter(action="REVIEW_UPDATED").exists())

    def test_admin_can_delete_and_it_is_audited(self):
        self.auth(self.admin)
        res = self.client.delete(f"{ADMIN_LIST_URL}{self.review.pk}/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(Review.objects.count(), 0)
        self.assertTrue(AuditLog.objects.filter(action="REVIEW_DELETED").exists())

    def test_no_public_review_list_endpoint(self):
        """GET /api/reviews/ must never return review data to anyone."""
        self.unauth()
        res = self.client.get("/api/reviews/")
        self.assertEqual(res.status_code, 405)  # POST-only submit endpoint
        # And even the response body carries no review content.
        self.assertNotIn(b"restful stay", res.content)

    def test_admin_rating_and_search_filters(self):
        # Second review from another completed stay.
        guest2 = make_guest(email="chi@guest.dev", first_name="Chi", last_name="Eze")
        today = hotel_today()
        booking2 = make_booking(
            guest2, self.room_type,
            check_in=today - timedelta(days=9), check_out=today - timedelta(days=7),
            status=Booking.Status.CHECKED_OUT,
        )
        Review.objects.create(booking=booking2, guest=guest2, guest_name=guest2.full_name,
                              rating=2, comment="The generator noise kept me awake.")
        self.auth(self.admin)
        res = self.client.get(ADMIN_LIST_URL, {"rating": 2})
        self.assertEqual([r["rating"] for r in res.data["data"]], [2])
        res = self.client.get(ADMIN_LIST_URL, {"search": booking2.booking_reference})
        self.assertEqual(len(res.data["data"]), 1)
        res = self.client.get(ADMIN_LIST_URL, {"search": "generator noise"})
        self.assertEqual(len(res.data["data"]), 1)
        res = self.client.get(ADMIN_LIST_URL, {"ordering": "lowest"})
        self.assertEqual([r["rating"] for r in res.data["data"]], [2, 5])
        res = self.client.get(ADMIN_LIST_URL, {"ordering": "highest"})
        self.assertEqual([r["rating"] for r in res.data["data"]], [5, 2])


class ReviewStatsTests(ReviewTestBase):
    def setUp(self):
        super().setUp()
        self.admin = make_staff("admin@jone.dev", role=User.Role.ADMIN)

    def test_empty_stats_report_null_average(self):
        self.auth(self.admin)
        res = self.client.get(ADMIN_STATS_URL)
        self.assertEqual(res.status_code, 200)
        data = res.data["data"]
        self.assertEqual(data["total"], 0)
        self.assertIsNone(data["average_rating"])  # never a fake 0.0

    def test_counts_and_average_are_correct(self):
        today = hotel_today()
        ratings = [5, 4, 4, 1]
        for i, rating in enumerate(ratings):
            guest = make_guest(email=f"stat{i}@guest.dev")
            booking = make_booking(
                guest, self.room_type,
                check_in=today - timedelta(days=20 + i * 3),
                check_out=today - timedelta(days=18 + i * 3),
                status=Booking.Status.CHECKED_OUT,
            )
            Review.objects.create(booking=booking, guest=guest, guest_name=guest.full_name,
                                  rating=rating, comment=f"Statistics fixture review #{i}.")
        self.auth(self.admin)
        data = self.client.get(ADMIN_STATS_URL).data["data"]
        self.assertEqual(data["total"], 4)
        self.assertEqual(data["average_rating"], 3.5)
        self.assertEqual(data["by_rating"], {"5": 1, "4": 2, "3": 0, "2": 0, "1": 1})
        self.assertEqual(data["new_count"], 4)


class ReviewInvitationTests(ReviewTestBase):
    def test_checkout_sends_review_invitation_email(self):
        from django.core import mail

        from apps.bookings.services.booking_service import check_out_booking

        staff = make_staff("desk@jone.dev", role=User.Role.RECEPTIONIST)
        guest = make_guest(email="stayed@guest.dev")
        today = hotel_today()
        booking = make_booking(
            guest, self.room_type,
            check_in=today - timedelta(days=2), check_out=today,
            status=Booking.Status.CHECKED_IN, amount_paid="15000.00",
        )
        mail.outbox = []
        # TestCase wraps everything in a transaction, so on_commit hooks must
        # be captured explicitly — exactly how the invitation runs in prod.
        with self.captureOnCommitCallbacks(execute=True):
            check_out_booking(booking, staff_user=staff)
        review_mails = [m for m in mail.outbox if "How was your stay" in m.subject]
        self.assertEqual(len(review_mails), 1)
        self.assertIn(booking.booking_reference, review_mails[0].body)
        self.assertIn("/review.html", review_mails[0].body)
        self.assertEqual(review_mails[0].to, [guest.email])
