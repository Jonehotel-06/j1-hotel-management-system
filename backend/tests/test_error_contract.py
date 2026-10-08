# tests/test_error_contract.py
"""Task 26: bad input to the endpoints this project added must come back as a
structured, machine-readable error — never a 500 and never a bare string.

Every response is checked for the envelope the dashboard's error handling
relies on: success=false, a stable `code`, and per-field `errors`.
"""
from datetime import timedelta
from decimal import Decimal

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.offers.models import GuestDiscount

from .base import BaseAPITestCase
from .factories import (
    hotel_settings,
    make_booking,
    make_guest,
    make_room,
    make_room_type,
    make_staff,
)


class ErrorContractTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        hotel_settings()
        self.today = hotel_today()
        self.room_type = make_room_type("Errors", price="20000.00")
        self.room = make_room(self.room_type, "X1")
        self.guest = make_guest("errors@example.com")
        self.admin = make_staff("errors.admin@staff.dev", role=User.Role.ADMIN)
        self.auth(self.admin)

    def assertValidationError(self, response, field, *, contains=None):
        self.assertEqual(response.status_code, 400, response.content[:300])
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "VALIDATION_ERROR")
        self.assertIn(field, body["errors"])
        if contains:
            self.assertIn(contains, " ".join(body["errors"][field]))

    # --- occupancy calendar --------------------------------------------------
    def test_calendar_rejects_non_numeric_month(self):
        self.assertValidationError(
            self.client.get("/api/admin/bookings/calendar/?year=abc&month=9"), "month")

    def test_calendar_rejects_out_of_range_month_and_year(self):
        self.assertValidationError(
            self.client.get("/api/admin/bookings/calendar/?year=2026&month=44"), "month")
        self.assertValidationError(
            self.client.get("/api/admin/bookings/calendar/?year=1800&month=1"), "year")

    def test_calendar_defaults_to_the_current_month_when_unspecified(self):
        response = self.client.get("/api/admin/bookings/calendar/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["data"]["start_date"].startswith(
            f"{self.today.year}-{self.today.month:02d}"))

    # --- reschedule ----------------------------------------------------------
    def _missed(self):
        return make_booking(self.guest, self.room_type, [self.room],
                            check_in=self.today - timedelta(days=3),
                            check_out=self.today - timedelta(days=1))

    def test_reschedule_requires_both_dates(self):
        response = self.client.post(
            f"/api/admin/bookings/{self._missed().booking_reference}/reschedule/", {})
        self.assertValidationError(response, "check_in")
        self.assertIn("check_out", response.json()["errors"])

    def test_reschedule_rejects_checkout_before_checkin(self):
        self.assertValidationError(
            self.client.post(
                f"/api/admin/bookings/{self._missed().booking_reference}/reschedule/",
                {"check_in": str(self.today + timedelta(days=5)),
                 "check_out": str(self.today + timedelta(days=2))}),
            "check_out")

    def test_reschedule_rejects_malformed_dates(self):
        self.assertValidationError(
            self.client.post(
                f"/api/admin/bookings/{self._missed().booking_reference}/reschedule/",
                {"check_in": "not-a-date", "check_out": "also-not"}),
            "check_in", contains="YYYY-MM-DD")

    def test_reschedule_unknown_booking_is_a_structured_404(self):
        response = self.client.post("/api/admin/bookings/J1-DOES-NOT-EXIST/reschedule/",
                                    {"check_in": str(self.today + timedelta(days=5)),
                                     "check_out": str(self.today + timedelta(days=7))})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "RESOURCE_NOT_FOUND")

    # --- guest discounts -----------------------------------------------------
    def _discount(self, **overrides):
        payload = {
            "guest": self.guest.pk,
            "discount_type": GuestDiscount.DiscountType.PERCENTAGE,
            "discount_value": "10.00",
            "start_date": str(self.today),
            "reason": "Corporate rate",
        }
        payload.update(overrides)
        return self.client.post("/api/admin/guest-discounts/", payload)

    def test_percentage_over_one_hundred_is_rejected(self):
        self.assertValidationError(self._discount(discount_value="150.00"),
                                   "discount_value", contains="cannot exceed 100")

    def test_zero_or_negative_discount_is_rejected(self):
        self.assertValidationError(self._discount(discount_value="0"), "discount_value")
        self.assertValidationError(self._discount(discount_value="-5"), "discount_value")

    def test_end_date_before_start_date_is_rejected(self):
        self.assertValidationError(
            self._discount(end_date=str(self.today - timedelta(days=5))),
            "end_date", contains="before the start date")

    def test_unknown_guest_and_bad_type_are_rejected(self):
        self.assertValidationError(self._discount(guest=999999), "guest")
        self.assertValidationError(self._discount(discount_type="BOGUS"), "discount_type")

    def test_reason_is_required_so_every_discount_is_explainable(self):
        response = self.client.post("/api/admin/guest-discounts/", {
            "guest": self.guest.pk,
            "discount_type": GuestDiscount.DiscountType.PERCENTAGE,
            "discount_value": "10.00",
            "start_date": str(self.today),
        })
        self.assertValidationError(response, "reason")

    def test_a_valid_discount_still_succeeds(self):
        self.assertEqual(self._discount().status_code, 201)

    # --- pagination ----------------------------------------------------------
    def test_out_of_range_page_is_a_structured_404_not_a_crash(self):
        response = self.client.get("/api/admin/bookings/missed/?page=9999")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "RESOURCE_NOT_FOUND")
