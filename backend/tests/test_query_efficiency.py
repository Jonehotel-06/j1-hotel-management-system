# tests/test_query_efficiency.py
"""Task 24: the list endpoints this project added or changed must not issue a
query per row. Each test asserts the query count is stable as the number of
rows grows — the only reliable way to catch an N+1 regression.
"""
from datetime import timedelta
from decimal import Decimal

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.core.utils import hotel_today
from apps.offers.models import GuestDiscount, Offer

from .base import BaseAPITestCase
from .factories import (
    hotel_settings,
    make_booking,
    make_guest,
    make_room,
    make_room_type,
    make_staff,
)


class QueryEfficiencyTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        hotel_settings()
        self.room_type = make_room_type("Efficiency", price="20000.00")
        self.rooms = [make_room(self.room_type, f"E{i:02d}") for i in range(1, 13)]
        self.manager = make_staff("qe.manager@staff.dev", role=User.Role.MANAGER)
        self.auth(self.manager)
        self.today = hotel_today()

    def _guests(self, count, prefix):
        return [make_guest(f"{prefix}{i}@example.com") for i in range(count)]

    def _count(self, url):
        """Queries issued by one GET, excluding first-request warm-up.

        The hotel settings singleton and similar per-process caches are filled
        on the first call, so an unwarmed measurement would overstate the
        baseline and hide a real N+1.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.client.get(url)  # warm caches
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.content[:300])
        return len(ctx.captured_queries)

    def test_public_room_option_availability_query_count_is_flat(self):
        """One blocked-ID query must serve every physical room in the response."""
        check_in = self.today + timedelta(days=10)
        check_out = check_in + timedelta(days=3)
        url = (
            f"/api/rooms/{self.room_type.slug}/rooms/?"
            f"check_in={check_in.isoformat()}&check_out={check_out.isoformat()}"
        )
        few = self._count(url)

        for index in range(13, 21):
            make_room(self.room_type, f"E{index:02d}")
        many = self._count(url)

        self.assertEqual(few, many, "physical room options issue a blocking query per room")

    def test_public_availability_query_count_is_flat_across_room_types(self):
        """Range search batches inventory and public-offer work by room type."""
        check_in = self.today + timedelta(days=10)
        check_out = check_in + timedelta(days=3)
        Offer.objects.create(
            title="Efficiency public", discount_type=Offer.DiscountType.PERCENTAGE,
            discount_value=Decimal("10.00"), start_date=self.today,
            end_date=self.today + timedelta(days=60),
        )
        url = (
            "/api/rooms/availability/?"
            f"check_in={check_in.isoformat()}&check_out={check_out.isoformat()}&guests=2&rooms=1"
        )
        few = self._count(url)

        for index in range(1, 9):
            room_type = make_room_type(f"Efficiency extra {index}", price="20000.00")
            make_room(room_type, f"X{index:02d}")
        many = self._count(url)

        self.assertEqual(few, many, "availability search issues work per room type")

    def test_guests_list_query_count_is_flat(self):
        """Per-guest booking counts must be annotated, not looped."""
        for index, guest in enumerate(self._guests(2, "qe.few")):
            make_booking(guest, self.room_type, [self.rooms[index]])
        few = self._count("/api/admin/guests/")

        for index, guest in enumerate(self._guests(8, "qe.many")):
            make_booking(guest, self.room_type, [self.rooms[index + 2]])
        many = self._count("/api/admin/guests/")

        self.assertEqual(few, many, "guest list issues a query per guest")

    def test_occupancy_calendar_query_count_is_flat(self):
        guest = make_guest("qe.occ@example.com")
        make_booking(guest, self.room_type, self.rooms[:1],
                     check_in=self.today + timedelta(days=2))
        few = self._count(f"/api/admin/bookings/calendar/?year={self.today.year}&month={self.today.month}")

        for offset, room in enumerate(self.rooms[1:9]):
            make_booking(make_guest(f"qe.occ{offset}@example.com"), self.room_type, [room],
                         check_in=self.today + timedelta(days=2 + offset % 3))
        many = self._count(f"/api/admin/bookings/calendar/?year={self.today.year}&month={self.today.month}")

        self.assertEqual(few, many, "occupancy calendar issues a query per booking/room")

    def test_missed_bookings_query_count_is_flat(self):
        def missed(email, room):
            return make_booking(
                make_guest(email), self.room_type, [room],
                check_in=self.today - timedelta(days=4),
                check_out=self.today - timedelta(days=2),
                amount_paid="20000.00",
            )

        missed("qe.miss0@example.com", self.rooms[0])
        few = self._count("/api/admin/bookings/missed/")

        for index in range(1, 9):
            missed(f"qe.miss{index}@example.com", self.rooms[index])
        many = self._count("/api/admin/bookings/missed/")

        self.assertEqual(few, many, "missed bookings issues a query per booking")

    def test_guest_discount_list_query_count_is_flat(self):
        def discount(email):
            guest = make_guest(email)
            return GuestDiscount.objects.create(
                guest=guest, discount_type=GuestDiscount.DiscountType.PERCENTAGE,
                discount_value=Decimal("10.00"), start_date=self.today,
                created_by=self.manager, reason="Loyalty",
            )

        discount("qe.gd0@example.com")
        few = self._count("/api/admin/guest-discounts/")

        for index in range(1, 9):
            discount(f"qe.gd{index}@example.com")
        many = self._count("/api/admin/guest-discounts/")

        self.assertEqual(few, many, "guest discount list issues a query per row")
