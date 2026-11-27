# tests/test_critical_repair_reschedule_discount.py
"""A missed-booking reschedule must keep the discount the guest was granted.

Reported acceptance case: a ₦70,000 room price with a ₦10,000 discount is a
₦60,000 total, and it must still be ₦60,000 after the booking is moved to new
dates.

Root cause: ``reschedule_booking`` re-priced the stay with
``pricing.calculate_quote`` and copied ``quote.discount`` onto the booking.
That function only grants a discount the guest/offer qualifies for TODAY, so a
personal discount approved for the original booking (deactivated, expired or
narrowed since), or an offer whose window has closed, came back as zero and the
guest was re-billed at full rate.

The repair carries the *terms* already granted (from the immutable
``GuestDiscountApplication`` snapshot, else the booking's offer, else the
recorded discount amount) onto the new stay, recalculates them once against the
new subtotal and still never lets a discount exceed the stay.
"""
from datetime import timedelta
from decimal import Decimal

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.bookings.models import Booking
from apps.bookings.services import pricing
from apps.core.utils import hotel_today
from apps.offers.models import GuestDiscount, GuestDiscountApplication, Offer

from .base import BaseAPITestCase
from .factories import hotel_settings, make_booking, make_guest, make_room, make_room_type, make_staff


class RescheduleDiscountTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        hotel_settings(tax_rate_percent=Decimal("0.00"), service_fee=Decimal("0.00"))
        self.manager = make_staff("resched.fix.manager@staff.dev", role=User.Role.MANAGER)
        self.receptionist = make_staff("resched.fix.desk@staff.dev", role=User.Role.RECEPTIONIST)
        self.auth(self.manager)
        # ₦70,000 per night, one night: the acceptance case.
        self.room_type = make_room_type("Missed-booking King", price="70000.00", max_guests=3)
        self.room = make_room(self.room_type, "R701")
        self.guest = make_guest("resched.fix.guest@test.dev")
        self.today = hotel_today()

    # ------------------------------------------------------------- helpers

    def missed_booking(self, *, discount="0.00", total="70000.00", amount_paid="0.00", **kwargs):
        booking = make_booking(
            self.guest, self.room_type, rooms=[self.room],
            check_in=self.today - timedelta(days=3), check_out=self.today - timedelta(days=2),
            status=Booking.Status.NO_SHOW, total=total, amount_paid=amount_paid, **kwargs,
        )
        if Decimal(discount) > 0:
            booking.subtotal = Decimal("70000.00")
            booking.discount_amount = Decimal(discount)
            booking.save(update_fields=["subtotal", "discount_amount", "updated_at"])
        return booking

    def grant_guest_discount(self, *, discount_type="FIXED_AMOUNT", value="10000.00", active=True):
        return GuestDiscount.objects.create(
            guest=self.guest, discount_type=discount_type, discount_value=Decimal(value),
            start_date=self.today - timedelta(days=30), end_date=None, is_active=active,
            reason="Manager approved courtesy discount", created_by=self.manager,
        )

    def attach_application(self, booking, discount, *, amount="10000.00"):
        return GuestDiscountApplication.objects.create(
            booking=booking, guest_discount=discount, guest=self.guest,
            discount_type=discount.discount_type, discount_value=discount.discount_value,
            amount=Decimal(amount), currency="NGN", reason=discount.reason,
        )

    def post(self, booking, *, days_out=21, nights=1):
        check_in = self.today + timedelta(days=days_out)
        return self.client.post(
            f"/api/admin/bookings/{booking.booking_reference}/reschedule/",
            {"check_in": check_in.isoformat(), "check_out": (check_in + timedelta(days=nights)).isoformat()},
            format="json",
        )

    # --------------------------------------------------------------- tests

    def test_fixed_discount_is_preserved_after_reschedule(self):
        """₦70,000 − ₦10,000 = ₦60,000, before AND after the date change."""
        discount = self.grant_guest_discount()
        booking = self.missed_booking(discount="10000.00", total="60000.00")
        self.attach_application(booking, discount)
        # The discount is withdrawn from the guest's live record afterwards: the
        # grant already made must not evaporate because of that.
        GuestDiscount.objects.filter(pk=discount.pk).update(is_active=False)

        response = self.post(booking)
        self.assertEqual(response.status_code, 200, response.content)
        booking.refresh_from_db()
        self.assertEqual(booking.subtotal, Decimal("70000.00"))
        self.assertEqual(booking.discount_amount, Decimal("10000.00"))
        self.assertEqual(booking.total_amount, Decimal("60000.00"))
        self.assertEqual(booking.amount_due, Decimal("60000.00"))
        payload = response.json()["data"]["reschedule"]
        self.assertEqual(payload["discount_amount"], "10000.00")
        self.assertEqual(payload["discount_preserved"]["source"], "GUEST_DISCOUNT_APPLICATION")

    def test_percentage_discount_is_recalculated_once_on_the_new_stay(self):
        discount = self.guest_discount_percentage()
        booking = self.missed_booking(discount="7000.00", total="63000.00")
        self.attach_application(booking, discount, amount="7000.00")
        GuestDiscount.objects.filter(pk=discount.pk).update(is_active=False)

        response = self.post(booking, nights=2)
        self.assertEqual(response.status_code, 200, response.content)
        booking.refresh_from_db()
        # 10% of the NEW two-night subtotal, applied exactly once.
        self.assertEqual(booking.subtotal, Decimal("140000.00"))
        self.assertEqual(booking.discount_amount, Decimal("14000.00"))
        self.assertEqual(booking.total_amount, Decimal("126000.00"))

    def guest_discount_percentage(self):
        return self.grant_guest_discount(discount_type="PERCENTAGE", value="10.00")

    def test_a_zero_discount_booking_stays_at_full_rate(self):
        booking = self.missed_booking()
        response = self.post(booking)
        self.assertEqual(response.status_code, 200, response.content)
        booking.refresh_from_db()
        self.assertEqual(booking.discount_amount, Decimal("0.00"))
        self.assertEqual(booking.total_amount, Decimal("70000.00"))

    def test_a_public_offer_discount_is_carried_past_its_window(self):
        offer = Offer.objects.create(
            title="Missed-booking courtesy", discount_type=Offer.DiscountType.FIXED_AMOUNT,
            discount_value=Decimal("10000.00"),
            start_date=self.today - timedelta(days=60), end_date=self.today - timedelta(days=30),
            is_active=False,
        )
        booking = self.missed_booking(discount="10000.00", total="60000.00")
        booking.offer = offer
        booking.save(update_fields=["offer", "updated_at"])

        response = self.post(booking)
        self.assertEqual(response.status_code, 200, response.content)
        booking.refresh_from_db()
        self.assertEqual(booking.discount_amount, Decimal("10000.00"))
        self.assertEqual(booking.total_amount, Decimal("60000.00"))

    def test_prior_payments_are_untouched_and_the_balance_is_reported(self):
        discount = self.grant_guest_discount()
        booking = self.missed_booking(discount="10000.00", total="60000.00", amount_paid="30000.00")
        self.attach_application(booking, discount)
        GuestDiscount.objects.filter(pk=discount.pk).update(is_active=False)

        response = self.post(booking)
        self.assertEqual(response.status_code, 200, response.content)
        booking.refresh_from_db()
        self.assertEqual(booking.total_amount, Decimal("60000.00"))
        self.assertEqual(booking.amount_paid, Decimal("30000.00"))
        self.assertEqual(Decimal(response.json()["data"]["reschedule"]["balance_due"]), Decimal("30000.00"))
        self.assertEqual(booking.amount_due, Decimal("30000.00"))

    def test_the_discount_is_never_applied_twice(self):
        """An eligible guest discount must not be added on top of itself."""
        discount = self.grant_guest_discount()          # still active
        booking = self.missed_booking(discount="10000.00", total="60000.00")
        self.attach_application(booking, discount)

        response = self.post(booking)
        self.assertEqual(response.status_code, 200, response.content)
        booking.refresh_from_db()
        self.assertEqual(booking.discount_amount, Decimal("10000.00"))
        self.assertEqual(booking.total_amount, Decimal("60000.00"))

    def test_a_discount_can_never_exceed_the_stay(self):
        discount = self.grant_guest_discount(value="500000.00")
        booking = self.missed_booking(discount="70000.00", total="0.00")
        self.attach_application(booking, discount, amount="70000.00")

        response = self.post(booking)
        self.assertEqual(response.status_code, 200, response.content)
        booking.refresh_from_db()
        self.assertEqual(booking.discount_amount, Decimal("70000.00"))
        self.assertEqual(booking.total_amount, Decimal("0.00"))

    def test_the_carried_discount_is_audited(self):
        discount = self.grant_guest_discount()
        booking = self.missed_booking(discount="10000.00", total="60000.00")
        self.attach_application(booking, discount)

        response = self.post(booking)
        self.assertEqual(response.status_code, 200, response.content)
        entry = AuditLog.objects.filter(action="BOOKING_RESCHEDULED").latest("created_at")
        self.assertEqual(entry.changes["discount_amount"], ["10000.00", "10000.00"])
        self.assertEqual(entry.metadata["discount_preserved"]["discount_type"], "FIXED_AMOUNT")
        self.assertEqual(entry.metadata["discount_applied"], "10000.00")

    def test_the_original_grant_snapshot_is_not_rewritten(self):
        discount = self.grant_guest_discount()
        booking = self.missed_booking(discount="10000.00", total="60000.00")
        application = self.attach_application(booking, discount)

        response = self.post(booking, nights=3)
        self.assertEqual(response.status_code, 200, response.content)
        application.refresh_from_db()
        # The historical grant keeps its original amount; the booking carries
        # the recalculated figure.
        self.assertEqual(application.amount, Decimal("10000.00"))
        booking.refresh_from_db()
        self.assertEqual(booking.discount_amount, Decimal("10000.00"))
        self.assertEqual(booking.total_amount, Decimal("200000.00"))

    def test_receptionist_still_cannot_reschedule_or_change_a_discount(self):
        self.auth(self.receptionist)
        booking = self.missed_booking(discount="10000.00", total="60000.00")
        self.assertEqual(self.post(booking).status_code, 403)
        # Discount editing is manager/admin only, so a receptionist cannot
        # increase a discount through the guest-discount endpoint either.
        discount = self.grant_guest_discount()
        response = self.client.patch(
            f"/api/admin/guest-discounts/{discount.pk}/",
            {"discount_value": "50000.00"}, format="json",
        )
        self.assertEqual(response.status_code, 403, response.content)
        discount.refresh_from_db()
        self.assertEqual(discount.discount_value, Decimal("10000.00"))

    def test_price_helpers_are_deterministic(self):
        quote = pricing.Quote(
            room_type_id=1, check_in=self.today, check_out=self.today + timedelta(days=1),
            rooms=1, adults=2, children=0, guests=2, nights=1, currency="NGN",
            price_per_night=Decimal("70000.00"), subtotal=Decimal("70000.00"),
            discount=Decimal("0.00"), total=Decimal("70000.00"),
        )
        carried = pricing.CarriedDiscount(
            discount_type="FIXED_AMOUNT", discount_value=Decimal("10000.00"), source="TEST",
        )
        applied = pricing.apply_carried_discount(quote, carried)
        self.assertEqual(applied.discount, Decimal("10000.00"))
        self.assertEqual(applied.total, Decimal("60000.00"))
