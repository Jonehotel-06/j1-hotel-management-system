"""Additive Stay lifecycle coverage around existing check-in/check-out flows."""
from datetime import timedelta

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.core.utils import hotel_today
from apps.finance.models import Folio
from apps.housekeeping.models import HousekeepingTask
from apps.stays.models import Stay, StayEvent, StayRoom

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type, make_staff


class StayLifecycleTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.today = hotel_today()
        self.staff = make_staff("stay-desk@staff.dev", role=User.Role.RECEPTIONIST)
        self.room_type = make_room_type("Stay foundation suite", price="20000.00")
        self.room = make_room(self.room_type, "S01")
        self.booking = make_booking(
            make_guest("stay-guest@example.com"),
            self.room_type,
            [self.room],
            check_in=self.today,
            check_out=self.today + timedelta(days=2),
            amount_paid="40000.00",
            total="40000.00",
        )
        self.booking.payment_status = Booking.PaymentStatus.PAID
        self.booking.save(update_fields=["payment_status", "updated_at"])
        self.auth(self.staff)

    def _check_in(self):
        response = self.client.post(f"/api/admin/bookings/{self.booking.booking_reference}/check-in/")
        self.assertEqual(response.status_code, 200, response.content)

    def test_check_in_creates_one_operational_stay_room_history_and_event(self):
        self._check_in()
        stay = Stay.objects.get(booking=self.booking)
        self.assertEqual(stay.status, Stay.Status.IN_HOUSE)
        self.assertEqual(stay.reference, f"STY-{self.booking.booking_reference}")
        self.assertEqual(stay.guest_id, self.booking.guest_id)
        folio = Folio.objects.get(stay=stay, kind=Folio.Kind.MAIN)
        self.assertEqual(folio.booking_id, self.booking.pk)
        self.assertEqual(folio.status, Folio.Status.OPEN)
        self.assertEqual(StayRoom.objects.filter(stay=stay, room=self.room, released_at__isnull=True).count(), 1)
        event = StayEvent.objects.get(stay=stay, type=StayEvent.Type.CHECKED_IN)
        self.assertEqual(event.details["booking_reference"], self.booking.booking_reference)

    def test_check_in_retry_does_not_duplicate_stay_or_event(self):
        self._check_in()
        self._check_in()
        stay = Stay.objects.get(booking=self.booking)
        self.assertEqual(Stay.objects.filter(booking=self.booking).count(), 1)
        self.assertEqual(StayRoom.objects.filter(stay=stay).count(), 1)
        self.assertEqual(StayEvent.objects.filter(stay=stay, type=StayEvent.Type.CHECKED_IN).count(), 1)

    def test_checkout_closes_stay_and_room_occupancy(self):
        self._check_in()
        response = self.client.post(f"/api/admin/bookings/{self.booking.booking_reference}/check-out/")
        self.assertEqual(response.status_code, 200, response.content)

        stay = Stay.objects.get(booking=self.booking)
        stay_room = StayRoom.objects.get(stay=stay, room=self.room)
        self.assertEqual(stay.status, Stay.Status.CHECKED_OUT)
        self.assertIsNotNone(stay.actual_check_out_at)
        self.assertIsNotNone(stay_room.released_at)
        self.assertIsNotNone(stay_room.actual_check_out_at)
        self.assertTrue(StayEvent.objects.filter(stay=stay, type=StayEvent.Type.CHECKED_OUT).exists())
        self.assertTrue(StayEvent.objects.filter(stay=stay, type=StayEvent.Type.ROOM_RELEASED).exists())
        task = HousekeepingTask.objects.get(stay=stay, room=self.room, type=HousekeepingTask.Type.CHECKOUT_CLEAN)
        self.assertEqual(task.status, HousekeepingTask.Status.OPEN)
        self.assertEqual(task.priority, HousekeepingTask.Priority.HIGH)

    def test_legacy_in_house_booking_is_lazily_bridged_when_checked_out(self):
        self.booking.status = Booking.Status.CHECKED_IN
        self.booking.checked_in_at = self.booking.created_at
        self.booking.save(update_fields=["status", "checked_in_at", "updated_at"])

        response = self.client.post(f"/api/admin/bookings/{self.booking.booking_reference}/check-out/")
        self.assertEqual(response.status_code, 200, response.content)
        stay = Stay.objects.get(booking=self.booking)
        self.assertEqual(stay.status, Stay.Status.CHECKED_OUT)
        self.assertIn("Bridged", stay.notes)

    def test_stay_events_are_append_only(self):
        self._check_in()
        event = StayEvent.objects.get(type=StayEvent.Type.CHECKED_IN)
        event.details = {"tampered": True}
        with self.assertRaisesMessage(Exception, "append-only"):
            event.save()
        with self.assertRaisesMessage(Exception, "append-only"):
            event.delete()
