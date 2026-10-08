# tests/test_dashboard_departures.py
"""Regression coverage for the operational departures list on the dashboard."""
from datetime import time

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.core.utils import hotel_today

from .base import BaseAPITestCase
from .factories import hotel_settings, make_booking, make_guest, make_room, make_room_type, make_staff


class DashboardDeparturesTests(BaseAPITestCase):
    def test_departure_count_and_rows_share_authoritative_checked_in_data(self):
        hotel_settings(check_out_time=time(12, 30))
        staff = make_staff("departures@staff.dev", role=User.Role.RECEPTIONIST)
        room_type = make_room_type("Departure Suite", price="30000.00")
        room = make_room(room_type, "212")
        booking = make_booking(
            make_guest("leaving@example.com", first_name="Ife", last_name="Okoro"),
            room_type,
            rooms=[room],
            check_in=hotel_today(),
            check_out=hotel_today(),
            status=Booking.Status.CHECKED_IN,
            amount_paid="30000.00",
            total="30000.00",
        )
        self.auth(staff)

        response = self.client.get("/api/admin/dashboard/")
        self.assertEqual(response.status_code, 200, response.json())
        data = response.json()["data"]
        self.assertEqual(data["today"]["departures"], 1)
        self.assertEqual(len(data["departures_today"]), 1)
        row = data["departures_today"][0]
        self.assertEqual(row["booking_reference"], booking.booking_reference)
        self.assertEqual(row["guest_name"], "Ife Okoro")
        self.assertEqual(row["room_numbers"], ["212"])
        self.assertEqual(row["check_out_time"], "12:30")
        self.assertEqual(row["status"], Booking.Status.CHECKED_IN)
