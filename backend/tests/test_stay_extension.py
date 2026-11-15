# tests/test_stay_extension.py
"""Extending a stay — quote, confirm, money and the guards around them.

WHY THIS FILE EXISTS
Reported from the front desk: *"The date and night in extend modal is failing.
When a guest wants to extend the days, it's not going through."* The whole
extension feature (``extend-quote`` / ``extend-stay`` / ``extensions``) shipped
with **no test anywhere in the repository** — not one backend test, not one
frontend test — so nothing could tell a broken deployment from a working one.
The red text the desk was reading turned out to come from the browser (see
``frontend/tests-js/test-extend-stay-flow.js``), but the endpoints on this side
were equally unguarded.

What is pinned here:
  * both ways of asking (a date OR a number of nights) price the same stay
    correctly and never write anything;
  * confirming moves the check-out date, **every** room assignment, the stay's
    expected departure, the totals and the payment — in one transaction;
  * a retry that targets the same date cannot charge twice;
  * a part payment leaves the difference on the booking; an over-payment is
    refused and writes nothing;
  * the refusals the desk can hit are distinct and named: cancelled booking, no
    physical room, and another booking holding the room for the added nights;
  * the authority boundary is unchanged: the front desk may extend, other
    departments may not;
  * ``original_check_out`` survives any number of extensions;
  * the URL the browser calls is the URL this app serves (a stale backend
    answering 404 for ``extend-quote`` is what the desk experienced).
"""
import json
from datetime import timedelta
from decimal import Decimal

from django.urls import reverse

from apps.accounts.models import User
from apps.bookings.models import Booking, BookingExtension
from apps.bookings.services.extension_service import extension_history
from apps.core.utils import hotel_today
from apps.payments.models import Payment
from tests.base import BaseAPITestCase
from tests.factories import make_booking, make_guest, make_room, make_room_type, make_staff

BASE = "/api/admin/bookings"


class StayExtensionTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.rec = make_staff("extend-rec@staff.dev", role=User.Role.RECEPTIONIST)
        self.room_type = make_room_type("Extension Suite", price="20000.00")
        self.room = make_room(self.room_type, "101")
        self.spare = make_room(self.room_type, "102")
        self.guest = make_guest("extend-guest@example.com")
        self.today = hotel_today()

    def in_house(self, rooms=None, check_in=None, check_out=None, number_of_rooms=1):
        booking = make_booking(
            self.guest, self.room_type, rooms=rooms if rooms is not None else [self.room],
            check_in=check_in or self.today - timedelta(days=1),
            check_out=check_out or self.today + timedelta(days=1),
            total="40000.00", amount_paid="40000.00", number_of_rooms=number_of_rooms,
        )
        booking.status = Booking.Status.CHECKED_IN
        booking.save(update_fields=["status"])
        return booking

    def quote(self, booking, payload, actor=None):
        self.auth(actor or self.rec)
        return self.client.post(
            f"{BASE}/{booking.booking_reference}/extend-quote/", payload, format="json"
        )

    def confirm(self, booking, payload, actor=None):
        self.auth(actor or self.rec)
        return self.client.post(
            f"{BASE}/{booking.booking_reference}/extend-stay/", payload, format="json"
        )

    def history(self, booking, actor=None):
        self.auth(actor or self.rec)
        return self.client.get(f"{BASE}/{booking.booking_reference}/extensions/")

    def body(self, response):
        return json.loads(response.content.decode("utf8"))


class ExtensionQuoteTests(StayExtensionTestBase):
    """A quote prices the stay and changes nothing."""

    def test_quote_by_nights_prices_the_extra_nights(self):
        booking = self.in_house()
        response = self.quote(booking, {"additional_nights": 2})
        self.assertEqual(response.status_code, 200, response.content)
        data = self.body(response)["data"]
        self.assertEqual(data["additional_nights"], 2)
        self.assertEqual(data["new_check_out"], str(booking.check_out + timedelta(days=2)))
        self.assertEqual(Decimal(data["extension_amount"]), Decimal("40000.00"))
        self.assertEqual(Decimal(data["nightly_rate"]), Decimal("20000.00"))
        self.assertTrue(data["available"])
        self.assertEqual(data["conflicts"], [])

    def test_quote_by_date_prices_the_same_way(self):
        booking = self.in_house()
        target = booking.check_out + timedelta(days=2)
        response = self.quote(booking, {"new_check_out": target.isoformat()})
        self.assertEqual(response.status_code, 200, response.content)
        data = self.body(response)["data"]
        self.assertEqual(data["additional_nights"], 2)
        self.assertEqual(Decimal(data["extension_amount"]), Decimal("40000.00"))

    def test_quote_multi_room_prices_every_room(self):
        booking = self.in_house(rooms=[self.room, self.spare], number_of_rooms=2)
        response = self.quote(booking, {"additional_nights": 1})
        self.assertEqual(response.status_code, 200, response.content)
        data = self.body(response)["data"]
        self.assertEqual(data["number_of_rooms"], 2)
        self.assertEqual(Decimal(data["extension_amount"]), Decimal("40000.00"))

    def test_quote_writes_nothing(self):
        booking = self.in_house()
        before_check_out = booking.check_out
        before_total = booking.total_amount
        self.quote(booking, {"additional_nights": 3})
        booking.refresh_from_db()
        self.assertEqual(booking.check_out, before_check_out)
        self.assertEqual(booking.total_amount, before_total)
        self.assertFalse(BookingExtension.objects.filter(booking=booking).exists())
        self.assertFalse(Payment.objects.filter(booking=booking).exists())

    def test_a_date_and_nights_that_disagree_is_refused(self):
        booking = self.in_house()
        response = self.quote(booking, {
            "new_check_out": (booking.check_out + timedelta(days=5)).isoformat(),
            "additional_nights": 2,
        })
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("do not match", self.body(response)["message"].lower())

    def test_a_date_before_the_current_checkout_is_refused(self):
        booking = self.in_house()
        response = self.quote(booking, {"new_check_out": booking.check_in.isoformat()})
        self.assertEqual(response.status_code, 400, response.content)

    def test_asking_for_neither_is_refused(self):
        booking = self.in_house()
        response = self.quote(booking, {})
        self.assertEqual(response.status_code, 400, response.content)

    def test_the_endpoints_the_browser_calls_are_the_routes_this_app_serves(self):
        """A stale backend answering 404 here is exactly what the desk reported."""
        reference = self.in_house().booking_reference
        for name, suffix in (
            ("admin-bookings:booking-extend-quote", "extend-quote"),
            ("admin-bookings:booking-extend-stay", "extend-stay"),
            ("admin-bookings:booking-extensions", "extensions"),
        ):
            with self.subTest(route=name):
                url = reverse(name, kwargs={"lookup": reference})
                self.assertTrue(url.endswith(f"/{suffix}/"), url)
                self.assertIn("/api/admin/bookings/", url)


class ExtensionConfirmTests(StayExtensionTestBase):
    """Confirming takes the money and moves the stay, or does nothing."""

    def test_confirm_by_nights_moves_the_stay_and_records_the_money(self):
        booking = self.in_house()
        old_check_out = booking.check_out
        response = self.confirm(booking, {"additional_nights": 2, "provider": "CASH"})
        self.assertEqual(response.status_code, 200, response.content)

        booking.refresh_from_db()
        self.assertEqual(booking.check_out, old_check_out + timedelta(days=2))
        self.assertEqual(booking.total_amount, Decimal("80000.00"))
        # Every reserved room runs to the new date — otherwise the room becomes
        # sellable to somebody else while the guest is still in it.
        for assignment in booking.room_assignments.all():
            self.assertEqual(assignment.check_out, booking.check_out)

        extension = BookingExtension.objects.get(booking=booking)
        self.assertEqual(extension.additional_nights, 2)
        self.assertEqual(extension.previous_check_out, old_check_out)
        self.assertEqual(extension.new_check_out, booking.check_out)
        self.assertEqual(extension.extension_amount, Decimal("40000.00"))
        self.assertEqual(extension.amount_paid, Decimal("40000.00"))
        self.assertEqual(extension.payment_method, "CASH")
        self.assertTrue(extension.payment_reference.startswith("J1P-"))
        self.assertEqual(extension.processed_by, self.rec)
        self.assertTrue(Payment.objects.filter(booking=booking, reference=extension.payment_reference).exists())

    def test_confirm_by_date_is_the_same_operation(self):
        booking = self.in_house()
        target = booking.check_out + timedelta(days=1)
        response = self.confirm(booking, {"new_check_out": target.isoformat(), "provider": "POS"})
        self.assertEqual(response.status_code, 200, response.content)
        booking.refresh_from_db()
        self.assertEqual(booking.check_out, target)
        extension = BookingExtension.objects.get(booking=booking)
        self.assertEqual(extension.payment_method, "POS")

    def test_a_part_payment_leaves_the_difference_on_the_booking(self):
        booking = self.in_house()
        response = self.confirm(booking, {"additional_nights": 2, "amount_paid": "15000.00"})
        self.assertEqual(response.status_code, 200, response.content)
        extension = BookingExtension.objects.get(booking=booking)
        self.assertEqual(extension.amount_paid, Decimal("15000.00"))
        self.assertEqual(extension.outstanding_balance, Decimal("25000.00"))
        booking.refresh_from_db()
        self.assertEqual(booking.amount_due, Decimal("25000.00"))

    def test_paying_more_than_the_extension_costs_changes_nothing(self):
        booking = self.in_house()
        old_check_out = booking.check_out
        response = self.confirm(booking, {"additional_nights": 1, "amount_paid": "999999.00"})
        self.assertEqual(response.status_code, 400, response.content)
        booking.refresh_from_db()
        self.assertEqual(booking.check_out, old_check_out)
        self.assertFalse(BookingExtension.objects.filter(booking=booking).exists())
        self.assertFalse(Payment.objects.filter(booking=booking).exists())

    def test_a_repeated_submit_on_the_same_date_is_refused_and_charges_nothing(self):
        """The double-click case: the desk presses Confirm twice on one date.

        The second attempt asks for the date the stay already runs to, so it is
        refused ("must be after the current one") and no second payment is taken.
        (A race between two requests that both read the OLD checkout date is
        caught separately by the duplicate-target guard inside the transaction,
        which answers 409 "already been extended"; serially it cannot be reached
        because the date check fires first.)
        """
        booking = self.in_house()
        payload = {"new_check_out": (booking.check_out + timedelta(days=2)).isoformat()}
        first = self.confirm(booking, payload)
        self.assertEqual(first.status_code, 200, first.content)
        second = self.confirm(booking, payload)
        self.assertEqual(second.status_code, 400, second.content)
        self.assertEqual(self.body(second)["code"], "INVALID_DATES")
        self.assertIn("must be after the current one", self.body(second)["message"])
        self.assertEqual(BookingExtension.objects.filter(booking=booking).count(), 1)
        self.assertEqual(Payment.objects.filter(booking=booking).count(), 1)

    def test_a_second_deliberate_extension_on_top_of_the_first(self):
        booking = self.in_house()
        self.assertEqual(self.confirm(booking, {"additional_nights": 1}).status_code, 200)
        booking.refresh_from_db()
        response = self.confirm(booking, {"additional_nights": 1})
        self.assertEqual(response.status_code, 200, response.content)
        booking.refresh_from_db()
        self.assertEqual(BookingExtension.objects.filter(booking=booking).count(), 2)
        self.assertEqual(booking.check_out, self.today + timedelta(days=3))


class ExtensionRefusalTests(StayExtensionTestBase):
    """Every refusal the desk can meet is named, and writes nothing."""

    def test_a_cancelled_booking_cannot_be_extended(self):
        booking = self.in_house()
        booking.status = Booking.Status.CANCELLED
        booking.save(update_fields=["status"])
        response = self.quote(booking, {"additional_nights": 1})
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn("cancelled", self.body(response)["message"].lower())

    def test_a_booking_with_no_room_assigned_says_so(self):
        booking = make_booking(
            self.guest, self.room_type, check_in=self.today,
            check_out=self.today + timedelta(days=1), total="20000.00", amount_paid="20000.00",
        )
        booking.status = Booking.Status.CHECKED_IN
        booking.save(update_fields=["status"])
        response = self.quote(booking, {"additional_nights": 1})
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn("no physical room assigned", self.body(response)["message"].lower())

    def test_a_room_held_by_another_booking_is_named_and_blocks_both_calls(self):
        booking = self.in_house()
        other_guest = make_guest("blocker@example.com")
        make_booking(
            other_guest, self.room_type, rooms=[self.room],
            check_in=booking.check_out, check_out=booking.check_out + timedelta(days=2),
            total="40000.00", amount_paid="40000.00",
        )
        quote = self.quote(booking, {"additional_nights": 2})
        self.assertEqual(quote.status_code, 200, quote.content)
        data = self.body(quote)["data"]
        self.assertFalse(data["available"], "the desk must be told before taking money")
        self.assertTrue(data["conflicts"])
        self.assertIn("101", data["conflicts"][0])

        confirm = self.confirm(booking, {"additional_nights": 2})
        self.assertEqual(confirm.status_code, 409, confirm.content)
        booking.refresh_from_db()
        self.assertEqual(booking.check_out, self.today + timedelta(days=1), "nothing was moved")
        self.assertFalse(BookingExtension.objects.filter(booking=booking).exists())

    def test_the_same_room_type_in_another_room_is_not_a_conflict(self):
        booking = self.in_house()
        other_guest = make_guest("neighbour@example.com")
        make_booking(
            other_guest, self.room_type, rooms=[self.spare],
            check_in=self.today + timedelta(days=1), check_out=self.today + timedelta(days=4),
            total="60000.00", amount_paid="60000.00",
        )
        response = self.quote(booking, {"additional_nights": 2})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(self.body(response)["data"]["available"], "a neighbour's booking is irrelevant")

    def test_other_departments_cannot_extend_a_stay(self):
        booking = self.in_house()
        for role in (User.Role.HOUSEKEEPING, User.Role.STOREKEEPER, User.Role.WAITER):
            actor = make_staff(f"extend-{role.lower()}@staff.dev", role=role)
            with self.subTest(role=role):
                self.assertEqual(self.quote(booking, {"additional_nights": 1}, actor=actor).status_code, 403)
                self.assertEqual(self.confirm(booking, {"additional_nights": 1}, actor=actor).status_code, 403)


class ExtensionHistoryTests(StayExtensionTestBase):
    """The desk must be able to see what the guest originally booked."""

    def test_original_check_out_survives_every_extension(self):
        booking = self.in_house()
        original = booking.check_out
        self.assertEqual(self.confirm(booking, {"additional_nights": 1}).status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(self.confirm(booking, {"additional_nights": 2}).status_code, 200)

        response = self.history(booking)
        self.assertEqual(response.status_code, 200, response.content)
        data = self.body(response)["data"]
        self.assertEqual(data["original_check_out"], str(original))
        self.assertEqual(data["count"], 2)
        self.assertTrue(data["can_extend"])
        booking.refresh_from_db()
        self.assertEqual(data["current_check_out"], str(booking.check_out))

    def test_history_is_readable_by_the_desk_and_lists_who_took_the_money(self):
        booking = self.in_house()
        self.confirm(booking, {"additional_nights": 1})
        data = self.body(self.history(booking))["data"]
        self.assertEqual(len(data["results"]), 1)
        row = data["results"][0]
        self.assertEqual(row["processed_by_name"], "Staff Receptionist")
        self.assertTrue(row["is_fully_paid"])
        self.assertEqual(Decimal(row["outstanding_balance"]), Decimal("0.00"))
