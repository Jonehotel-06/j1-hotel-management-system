# tests/test_guest_discounts.py
"""Individual guest discounts: precedence, immutability, permissions, pricing.

The hotel can attach a personal discount to one guest. The rules pinned here:

* the backend decides the discount — the client never sends an amount;
* a personal discount and a public offer NEVER stack: the LARGER of the two
  wins, and a tie goes to the public offer;
* the amount applied to a booking is snapshotted, so later edits to the guest's
  discount can never change historical booking totals or a receipt;
* managers/admins may write guest discounts, all staff may read, guests cannot
  reach the admin endpoint at all.
"""
from datetime import timedelta
from decimal import Decimal

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.bookings.services.pricing import calculate_quote
from apps.core.utils import hotel_today
from apps.offers.models import GuestDiscount, GuestDiscountApplication, Offer
from apps.offers.services import best_discount_for_stay

from .base import BaseAPITestCase
from .factories import hotel_settings, make_guest, make_room, make_room_type, make_staff, make_user

ENDPOINT = "/api/admin/guest-discounts/"


class GuestDiscountTestMixin:
    def setUp(self):
        super().setUp()
        hotel_settings(tax_rate_percent=Decimal("0.00"), service_fee=Decimal("0.00"),
                       deposit_percent=Decimal("100.00"))
        self.room_type = make_room_type("Discount Suite", price="100000.00", max_guests=4)
        make_room(self.room_type, "DS1")
        make_room(self.room_type, "DS2")
        self.guest = make_guest("discount.guest@test.dev")
        self.today = hotel_today()
        self.check_in = self.today + timedelta(days=7)
        self.check_out = self.check_in + timedelta(days=2)   # 2 nights = 200,000

    def make_discount(self, *, value="10.00", discount_type=GuestDiscount.DiscountType.PERCENTAGE,
                      guest=None, **extra):
        return GuestDiscount.objects.create(
            guest=guest or self.guest,
            discount_type=discount_type,
            discount_value=Decimal(value),
            start_date=extra.pop("start_date", self.today - timedelta(days=1)),
            end_date=extra.pop("end_date", self.today + timedelta(days=60)),
            reason=extra.pop("reason", "Loyalty"),
            **extra,
        )

    def make_offer(self, *, value="5.00", discount_type=Offer.DiscountType.PERCENTAGE, **extra):
        return Offer.objects.create(
            title=extra.pop("title", "Public Saver"),
            description="Public offer",
            discount_type=discount_type,
            discount_value=Decimal(value),
            start_date=extra.pop("start_date", self.today - timedelta(days=1)),
            end_date=extra.pop("end_date", self.today + timedelta(days=60)),
            is_active=extra.pop("is_active", True),
            **extra,
        )

    def quote(self, guest=None, **kwargs):
        return calculate_quote(
            room_type=self.room_type,
            check_in=kwargs.pop("check_in", self.check_in),
            check_out=kwargs.pop("check_out", self.check_out),
            rooms=kwargs.pop("rooms", 1),
            adults=2, children=0, guest=guest, **kwargs,
        )


class GuestDiscountPrecedenceTests(GuestDiscountTestMixin, BaseAPITestCase):
    """The larger discount wins and the two are never added together."""

    def test_guest_discount_alone_is_applied(self):
        self.make_discount(value="10.00")            # 10% of 200,000 = 20,000
        quote = self.quote(guest=self.guest)
        self.assertEqual(quote.discount, Decimal("20000.00"))
        self.assertEqual(quote.discount_source, "GUEST_DISCOUNT")
        self.assertEqual(quote.total, Decimal("180000.00"))

    def test_larger_guest_discount_beats_public_offer(self):
        self.make_offer(value="5.00")                # 10,000
        self.make_discount(value="25.00")            # 50,000
        quote = self.quote(guest=self.guest)
        self.assertEqual(quote.discount, Decimal("50000.00"))
        self.assertEqual(quote.discount_source, "GUEST_DISCOUNT")
        self.assertIsNone(quote.offer)

    def test_larger_public_offer_beats_guest_discount(self):
        self.make_offer(value="30.00")               # 60,000
        self.make_discount(value="5.00")             # 10,000
        quote = self.quote(guest=self.guest)
        self.assertEqual(quote.discount, Decimal("60000.00"))
        self.assertEqual(quote.discount_source, "OFFER")
        self.assertIsNone(quote.guest_discount)

    def test_discounts_are_never_stacked(self):
        self.make_offer(value="10.00")               # 20,000
        self.make_discount(value="20.00")            # 40,000
        quote = self.quote(guest=self.guest)
        # Stacked would be 60,000; the rule is "larger wins".
        self.assertEqual(quote.discount, Decimal("40000.00"))
        self.assertEqual(quote.total, Decimal("160000.00"))

    def test_tie_goes_to_the_public_offer(self):
        self.make_offer(value="10.00")
        self.make_discount(value="10.00")
        quote = self.quote(guest=self.guest)
        self.assertEqual(quote.discount, Decimal("20000.00"))
        self.assertEqual(quote.discount_source, "OFFER")

    def test_fixed_amount_guest_discount_is_capped_at_subtotal(self):
        self.make_discount(value="999999.00",
                           discount_type=GuestDiscount.DiscountType.FIXED_AMOUNT)
        quote = self.quote(guest=self.guest)
        self.assertEqual(quote.discount, Decimal("200000.00"))
        self.assertEqual(quote.total, Decimal("0.00"))

    def test_inactive_expired_and_future_discounts_are_ignored(self):
        cases = {
            "inactive": dict(is_active=False),
            "expired": dict(start_date=self.today - timedelta(days=30),
                            end_date=self.today - timedelta(days=1)),
            "not_started": dict(start_date=self.today + timedelta(days=90),
                                end_date=self.today + timedelta(days=120)),
        }
        for label, kwargs in cases.items():
            with self.subTest(case=label):
                GuestDiscount.objects.all().delete()
                self.make_discount(value="50.00", **kwargs)
                quote = self.quote(guest=self.guest)
                self.assertEqual(quote.discount, Decimal("0.00"))
                self.assertEqual(quote.discount_source, "")

    def test_open_ended_discount_stays_valid(self):
        self.make_discount(value="10.00", end_date=None)
        quote = self.quote(guest=self.guest)
        self.assertEqual(quote.discount_source, "GUEST_DISCOUNT")

    def test_discount_belongs_to_one_guest_only(self):
        other = make_guest("other.guest@test.dev")
        self.make_discount(value="30.00", guest=other)
        self.assertEqual(self.quote(guest=self.guest).discount, Decimal("0.00"))
        self.assertEqual(self.quote(guest=other).discount, Decimal("60000.00"))

    def test_anonymous_quote_gets_no_personal_discount(self):
        self.make_discount(value="30.00")
        self.assertEqual(self.quote(guest=None).discount, Decimal("0.00"))

    def test_best_discount_for_stay_reports_its_decision(self):
        self.make_offer(value="5.00")
        discount = self.make_discount(value="25.00")
        offer, guest_discount, amount, source = best_discount_for_stay(
            guest=self.guest, room_type=self.room_type, check_in=self.check_in,
            check_out=self.check_out, nights=2, subtotal=Decimal("200000.00"),
        )
        self.assertIsNone(offer)
        self.assertEqual(guest_discount, discount)
        self.assertEqual(amount, Decimal("50000.00"))
        self.assertEqual(source, "GUEST_DISCOUNT")


class GuestDiscountBookingTests(GuestDiscountTestMixin, BaseAPITestCase):
    """The booking that results carries an immutable snapshot."""

    def create_booking(self):
        payload = {
            "room_type": self.room_type.slug,
            "check_in": self.check_in.isoformat(),
            "check_out": self.check_out.isoformat(),
            "rooms": 1, "adults": 2, "children": 0,
            "guest": {
                "first_name": self.guest.first_name, "last_name": self.guest.last_name,
                "email": self.guest.email, "phone": self.guest.phone,
            },
        }
        return self.client.post("/api/bookings/", payload, format="json")

    def test_booking_total_uses_the_personal_discount(self):
        self.make_discount(value="25.00")
        res = self.create_booking()
        self.assertIn(res.status_code, (200, 201), res.json())
        booking = Booking.objects.get(guest__email=self.guest.email)
        self.assertEqual(booking.discount_amount, Decimal("50000.00"))
        self.assertEqual(booking.total_amount, Decimal("150000.00"))

    def test_application_snapshot_is_written_once_and_survives_edits(self):
        discount = self.make_discount(value="25.00")
        self.create_booking()
        booking = Booking.objects.get(guest__email=self.guest.email)
        application = GuestDiscountApplication.objects.get(booking=booking)
        self.assertEqual(application.discount_value, Decimal("25.00"))
        self.assertEqual(application.amount, Decimal("50000.00"))

        # The hotel later changes (or removes) the guest's standing discount.
        discount.discount_value = Decimal("5.00")
        discount.is_active = False
        discount.save()

        application.refresh_from_db()
        booking.refresh_from_db()
        self.assertEqual(application.discount_value, Decimal("25.00"))
        self.assertEqual(application.amount, Decimal("50000.00"))
        self.assertEqual(booking.discount_amount, Decimal("50000.00"))
        self.assertEqual(booking.total_amount, Decimal("150000.00"))

    def test_no_snapshot_when_the_public_offer_wins(self):
        self.make_offer(value="40.00")
        self.make_discount(value="5.00")
        self.create_booking()
        booking = Booking.objects.get(guest__email=self.guest.email)
        self.assertFalse(GuestDiscountApplication.objects.filter(booking=booking).exists())
        self.assertEqual(booking.discount_amount, Decimal("80000.00"))

    def test_quote_endpoint_reflects_the_personal_discount_for_the_signed_in_guest(self):
        user = make_user(email="signed.in@test.dev")
        self.guest.user = user
        self.guest.save(update_fields=["user"])
        self.make_discount(value="25.00")

        params = {
            "room_type": self.room_type.slug,
            "check_in": self.check_in.isoformat(),
            "check_out": self.check_out.isoformat(),
            "rooms": 1, "adults": 2, "children": 0,
        }
        anonymous = self.client.post("/api/bookings/quote/", params, format="json")
        self.assertEqual(anonymous.status_code, 200, anonymous.json())
        self.assertEqual(Decimal(anonymous.json()["data"]["discount"]), Decimal("0.00"))

        self.auth(user)
        personal = self.client.post("/api/bookings/quote/", params, format="json")
        self.assertEqual(personal.status_code, 200, personal.json())
        data = personal.json()["data"]
        self.assertEqual(Decimal(data["discount"]), Decimal("50000.00"))
        self.assertEqual(data["discount_source"], "GUEST_DISCOUNT")
        self.assertEqual(data["guest_discount"]["discount_value"], "25.00")

    def test_client_cannot_inject_its_own_discount(self):
        """Amounts posted by the client are ignored — pricing is backend-only."""
        self.make_discount(value="10.00")
        payload = {
            "room_type": self.room_type.slug,
            "check_in": self.check_in.isoformat(),
            "check_out": self.check_out.isoformat(),
            "rooms": 1, "adults": 2, "children": 0,
            "discount_amount": "199999.00", "total_amount": "1.00",
            "guest": {
                "first_name": "Ada", "last_name": "Obi",
                "email": self.guest.email, "phone": "08031234567",
            },
        }
        res = self.client.post("/api/bookings/", payload, format="json")
        self.assertIn(res.status_code, (200, 201), res.json())
        booking = Booking.objects.get(guest__email=self.guest.email)
        self.assertEqual(booking.discount_amount, Decimal("20000.00"))
        self.assertEqual(booking.total_amount, Decimal("180000.00"))


class GuestDiscountAdminApiTests(GuestDiscountTestMixin, BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("gd.manager@staff.dev", role=User.Role.MANAGER)
        self.receptionist = make_staff("gd.desk@staff.dev", role=User.Role.RECEPTIONIST)
        self.guest_user = make_user(email="gd.guest@test.dev")

    def payload(self, **extra):
        data = {
            "guest": self.guest.pk,
            "discount_type": GuestDiscount.DiscountType.PERCENTAGE,
            "discount_value": "15.00",
            "start_date": self.today.isoformat(),
            "end_date": (self.today + timedelta(days=30)).isoformat(),
            "reason": "Corporate rate",
        }
        data.update(extra)
        return data

    def test_manager_can_create_and_it_is_audited(self):
        from apps.audit.models import AuditLog

        self.auth(self.manager)
        res = self.client.post(ENDPOINT, self.payload(), format="json")
        self.assertEqual(res.status_code, 201, res.json())
        discount = GuestDiscount.objects.get(pk=res.json()["data"]["id"])
        self.assertEqual(discount.created_by, self.manager)
        self.assertTrue(
            AuditLog.objects.filter(action="GUEST_DISCOUNT_CREATED").exists()
        )

    def test_receptionist_can_read_but_not_write(self):
        self.make_discount()
        self.auth(self.receptionist)
        self.assertEqual(self.client.get(ENDPOINT).status_code, 200)
        self.assertEqual(
            self.client.post(ENDPOINT, self.payload(), format="json").status_code, 403
        )

    def test_guest_and_anonymous_are_refused(self):
        self.auth(self.guest_user)
        self.assertEqual(self.client.get(ENDPOINT).status_code, 403)
        self.unauth()
        self.assertIn(self.client.get(ENDPOINT).status_code, (401, 403))

    def test_delete_deactivates_instead_of_destroying_history(self):
        discount = self.make_discount()
        self.auth(self.manager)
        res = self.client.delete(f"{ENDPOINT}{discount.pk}/")
        self.assertIn(res.status_code, (200, 204))
        discount.refresh_from_db()
        self.assertFalse(discount.is_active)

    def test_list_filters_by_guest_and_validity(self):
        self.make_discount(value="10.00")
        other = make_guest("filter.other@test.dev")
        self.make_discount(value="10.00", guest=other, is_active=False)
        self.auth(self.manager)

        by_guest = self.client.get(ENDPOINT, {"guest": self.guest.pk})
        self.assertEqual(by_guest.status_code, 200, by_guest.json())
        self.assertTrue(by_guest.json()["data"])
        for row in by_guest.json()["data"]:
            self.assertEqual(row["guest"], self.guest.pk)

        active_only = self.client.get(ENDPOINT, {"is_active": "true"})
        for row in active_only.json()["data"]:
            self.assertTrue(row["is_active"])

    def test_invalid_percentage_is_rejected(self):
        self.auth(self.manager)
        res = self.client.post(ENDPOINT, self.payload(discount_value="150.00"), format="json")
        self.assertEqual(res.status_code, 400, res.json())
