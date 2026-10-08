"""Range-predicate and index coverage for staff financial reports.

These checks protect the MySQL-safe date filtering added in Tranche 0.  A
DateTimeField ``__date`` lookup wraps the indexed column in a database function;
half-open local-day bounds preserve the API's inclusive date semantics without
that query-plan regression.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.utils import timezone

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.core.utils import hotel_date_bounds, hotel_today
from apps.payments.models import Payment
from apps.reports.services.reports import revenue_report

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type, make_staff


class ReportRangePredicateTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.today = hotel_today()
        self.manager = make_staff("report-range@staff.dev", role=User.Role.MANAGER)
        self.room_type = make_room_type("Report range suite", price="25000.00")
        self.room = make_room(self.room_type, "RR01")
        self.booking = make_booking(
            make_guest("report-range@example.com"),
            self.room_type,
            [self.room],
            check_in=self.today + timedelta(days=10),
            check_out=self.today + timedelta(days=12),
        )

    @staticmethod
    def _at(day, at=time(12, 0)):
        value = datetime.combine(day, at)
        return timezone.make_aware(value) if timezone.is_aware(timezone.now()) else value

    def _payment(self, reference, paid_at, amount):
        return Payment.objects.create(
            booking=self.booking,
            reference=reference,
            provider=Payment.Provider.CASH,
            amount=Decimal(amount),
            status=Payment.Status.SUCCESS,
            paid_at=paid_at,
        )

    def test_hotel_date_bounds_are_start_inclusive_and_next_day_exclusive(self):
        start = self.today - timedelta(days=1)
        end = self.today
        range_start, range_end = hotel_date_bounds(start, end)
        self.assertEqual(range_start, self._at(start, time.min))
        self.assertEqual(range_end, self._at(end + timedelta(days=1), time.min))

    def test_revenue_report_keeps_both_inclusive_calendar_day_boundaries(self):
        start = self.today - timedelta(days=1)
        end = self.today
        self._payment("J1P-RANGE-BEFORE", self._at(start - timedelta(days=1), time(23, 59)), "10.00")
        self._payment("J1P-RANGE-START", self._at(start, time.min), "20.00")
        self._payment("J1P-RANGE-END", self._at(end, time(23, 59, 59)), "30.00")
        self._payment("J1P-RANGE-AFTER", self._at(end + timedelta(days=1), time.min), "40.00")

        self.booking.refund_amount = Decimal("12.00")
        self.booking.cancelled_at = self._at(end, time(23, 59, 59))
        self.booking.save(update_fields=["refund_amount", "cancelled_at", "updated_at"])

        report = revenue_report(start, end)

        self.assertEqual(report["grand_total"], "50.00")
        self.assertEqual(report["transactions"], 2)
        self.assertEqual(report["refunds"], {"total": "12.00", "count": 1})
        self.assertEqual([row["date"] for row in report["by_day"]], [start.isoformat(), end.isoformat()])

    def test_revenue_endpoint_retains_the_existing_inclusive_date_contract(self):
        self.auth(self.manager)
        self._payment("J1P-RANGE-API", self._at(self.today, time(23, 59, 59)), "1250.00")

        response = self.client.get(
            "/api/admin/reports/revenue/",
            {"start_date": self.today.isoformat(), "end_date": self.today.isoformat()},
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()["data"]
        self.assertEqual(payload["grand_total"], "1250.00")
        self.assertEqual(payload["transactions"], 1)

    def test_report_indexes_are_declared_for_range_filters(self):
        payment_indexes = [tuple(index.fields) for index in Payment._meta.indexes]
        booking_indexes = [tuple(index.fields) for index in Booking._meta.indexes]
        self.assertIn(("status", "paid_at"), payment_indexes)
        self.assertIn(("cancelled_at",), booking_indexes)
