# tests/test_offer_eligibility.py
# backend/tests/test_offer_eligibility.py
"""Offer eligibility rejections carry the SPECIFIC reason, per stay.

The quote endpoint is the single authority on whether a promo code applies.
The frontend shows the reason it returns on the offer field and re-prices the
stay at the standard rate, so each rejection must be distinguishable by code —
a generic OFFER_NOT_APPLICABLE would leave the guest guessing.
"""
from datetime import timedelta
from decimal import Decimal

from apps.core.utils import hotel_today
from apps.offers.models import Offer
from tests.base import BaseAPITestCase
from tests.factories import make_room, make_room_type

QUOTE_URL = "/api/bookings/quote/"


def make_offer(code="SAVE20", *, start=None, end=None, min_nights=1, max_nights=None,
               discount_type=Offer.DiscountType.PERCENTAGE, value="20.00",
               is_active=True, room_types=None):
    today = hotel_today()
    offer = Offer.objects.create(
        title=f"Offer {code}", code=code,
        discount_type=discount_type, discount_value=Decimal(value),
        start_date=start or today, end_date=end or today + timedelta(days=60),
        min_nights=min_nights, max_nights=max_nights, is_active=is_active,
    )
    if room_types:
        offer.room_types.set(room_types)
    return offer


class OfferEligibilityQuoteTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.rt = make_room_type(name="Executive", price="75000.00", max_guests=3)
        for number in ("401", "402", "403"):
            make_room(self.rt, number)
        self.today = hotel_today()

    def quote(self, *, nights=3, rooms=1, offer_code=None, start_offset=10, room_type=None):
        check_in = self.today + timedelta(days=start_offset)
        payload = {
            "room_type": (room_type or self.rt).slug,
            "check_in": check_in.isoformat(),
            "check_out": (check_in + timedelta(days=nights)).isoformat(),
            "rooms": rooms, "adults": 2, "children": 0,
        }
        if offer_code is not None:
            payload["offer_code"] = offer_code
        return self.client.post(QUOTE_URL, payload, format="json")

    # --- Accepted -------------------------------------------------------
    def test_valid_offer_discounts_the_quote(self):
        make_offer("SAVE20", min_nights=2)
        res = self.quote(nights=3, offer_code="SAVE20")
        self.assertEqual(res.status_code, 200)
        data = res.data["data"]
        self.assertEqual(Decimal(data["subtotal"]), Decimal("225000.00"))
        self.assertEqual(Decimal(data["discount"]), Decimal("45000.00"))
        self.assertEqual(Decimal(data["total"]), Decimal("180000.00"))
        self.assertEqual(data["offer"]["code"], "SAVE20")

    def test_offer_code_is_case_insensitive(self):
        make_offer("SAVE20")
        res = self.quote(offer_code="save20")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["data"]["offer"]["code"], "SAVE20")

    def test_multi_room_discount_applies_to_the_whole_stay(self):
        """Nightly x nights x rooms, THEN the discount — not per room."""
        make_offer("SAVE20")
        res = self.quote(nights=3, rooms=2, offer_code="SAVE20")
        self.assertEqual(res.status_code, 200)
        data = res.data["data"]
        self.assertEqual(data["rooms"], 2)
        self.assertEqual(data["nights"], 3)
        self.assertEqual(Decimal(data["subtotal"]), Decimal("450000.00"))
        self.assertEqual(Decimal(data["discount"]), Decimal("90000.00"))
        self.assertEqual(Decimal(data["total"]), Decimal("360000.00"))

    def test_fixed_amount_offer_never_exceeds_the_subtotal(self):
        make_offer("BIG", discount_type=Offer.DiscountType.FIXED_AMOUNT, value="999000.00")
        res = self.quote(nights=1, offer_code="BIG")
        self.assertEqual(res.status_code, 200)
        data = res.data["data"]
        self.assertEqual(Decimal(data["discount"]), Decimal("75000.00"))
        self.assertEqual(Decimal(data["total"]), Decimal("0.00"))

    # --- Rejected, each with its own reason ------------------------------
    def assert_rejected(self, response, code):
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], code)
        self.assertFalse(response.data["success"])
        self.assertTrue(response.data["message"])

    def test_unknown_code_is_invalid(self):
        self.assert_rejected(self.quote(offer_code="NOPE"), "OFFER_CODE_INVALID")

    def test_inactive_offer_is_expired(self):
        make_offer("OFF", is_active=False)
        self.assert_rejected(self.quote(offer_code="OFF"), "OFFER_EXPIRED")

    def test_stay_entirely_after_the_offer_window_is_expired(self):
        make_offer("PAST", start=self.today - timedelta(days=30),
                   end=self.today - timedelta(days=1))
        self.assert_rejected(self.quote(offer_code="PAST"), "OFFER_EXPIRED")

    def test_stay_before_the_offer_starts_is_not_started(self):
        make_offer("SOON", start=self.today + timedelta(days=40))
        res = self.quote(start_offset=10, offer_code="SOON")
        self.assert_rejected(res, "OFFER_NOT_STARTED")
        # The guest is told WHEN it starts, so the message is actionable.
        self.assertIn(f"{self.today + timedelta(days=40):%d %b %Y}", res.data["message"])

    def test_stay_running_past_the_end_date_does_not_cover_the_stay(self):
        make_offer("ENDS", start=self.today, end=self.today + timedelta(days=11))
        self.assert_rejected(self.quote(start_offset=10, nights=5, offer_code="ENDS"),
                             "OFFER_DOES_NOT_COVER_STAY")

    def test_stay_shorter_than_min_nights(self):
        make_offer("LONG", min_nights=4)
        res = self.quote(nights=2, offer_code="LONG")
        self.assert_rejected(res, "OFFER_STAY_TOO_SHORT")
        self.assertIn("4", res.data["message"])

    def test_stay_longer_than_max_nights(self):
        make_offer("SHORT", max_nights=2)
        res = self.quote(nights=5, offer_code="SHORT")
        self.assert_rejected(res, "OFFER_STAY_TOO_LONG")
        self.assertIn("2", res.data["message"])

    def test_offer_restricted_to_another_room_type(self):
        other = make_room_type(name="Deluxe", price="50000.00")
        make_offer("DELUXE", room_types=[other])
        res = self.quote(offer_code="DELUXE")
        self.assert_rejected(res, "OFFER_ROOM_TYPE_NOT_ELIGIBLE")
        self.assertIn("Executive", res.data["message"])

    def test_room_type_restriction_allows_the_listed_type(self):
        make_offer("EXEC", room_types=[self.rt])
        self.assertEqual(self.quote(offer_code="EXEC").status_code, 200)

    # --- The stay itself must survive a bad code -------------------------
    def test_rejecting_a_code_never_breaks_the_base_quote(self):
        """After an OFFER_* rejection the same stay must still price normally.

        This is the regression behind "total unavailable": an optional code
        that does not apply must cost the guest the discount, never the quote.
        """
        make_offer("LONG", min_nights=9)
        self.assert_rejected(self.quote(nights=3, rooms=2, offer_code="LONG"),
                             "OFFER_STAY_TOO_SHORT")
        res = self.quote(nights=3, rooms=2)
        self.assertEqual(res.status_code, 200)
        data = res.data["data"]
        self.assertEqual(data["nights"], 3)
        self.assertEqual(data["rooms"], 2)
        self.assertEqual(Decimal(data["subtotal"]), Decimal("450000.00"))
        self.assertEqual(Decimal(data["discount"]), Decimal("0.00"))
        self.assertEqual(Decimal(data["total"]), Decimal("450000.00"))

    def test_blank_offer_code_is_treated_as_no_code(self):
        res = self.quote(nights=2, offer_code="")
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.data["data"]["offer"])


class OfferPublicListTests(BaseAPITestCase):
    """The public list must expose everything the site displays."""

    def setUp(self):
        super().setUp()
        self.rt = make_room_type(name="Executive", price="75000.00")
        self.today = hotel_today()

    def rows(self):
        res = self.client.get("/api/offers/")
        self.assertEqual(res.status_code, 200)
        payload = res.data
        return payload["data"] if isinstance(payload, dict) else payload

    def test_upcoming_offers_are_listed_with_their_rules(self):
        offer = make_offer("OCT20", start=self.today + timedelta(days=20),
                           end=self.today + timedelta(days=40),
                           min_nights=3, max_nights=10, room_types=[self.rt])
        rows = {row["code"]: row for row in self.rows()}
        self.assertIn("OCT20", rows)
        row = rows["OCT20"]
        self.assertEqual(row["start_date"], offer.start_date.isoformat())
        self.assertEqual(row["end_date"], offer.end_date.isoformat())
        self.assertEqual(row["min_nights"], 3)
        self.assertEqual(row["max_nights"], 10)
        self.assertEqual([t["slug"] for t in row["applicable_room_types"]], [self.rt.slug])

    def test_ended_and_inactive_offers_are_hidden(self):
        make_offer("OVER", start=self.today - timedelta(days=20),
                   end=self.today - timedelta(days=1))
        make_offer("OFF", is_active=False)
        codes = {row["code"] for row in self.rows()}
        self.assertNotIn("OVER", codes)
        self.assertNotIn("OFF", codes)
