"""Batch 03 regressions for general/private offer boundaries."""
from datetime import timedelta

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.offers.models import GuestDiscount, Offer
from tests.base import BaseAPITestCase
from tests.factories import make_guest, make_room_type, make_staff


class OfferCreationTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("offers-manager@test.dev", role=User.Role.MANAGER)
        self.room_type = make_room_type("Offers room")
        self.guest = make_guest("offers-guest@test.dev")
        self.today = hotel_today()
        self.auth(self.manager)

    def general(self, **overrides):
        values = {"title": "Special", "description": "A public offer",
                  "discount_type": "PERCENTAGE", "discount_value": "10.00",
                  "start_date": str(self.today), "end_date": str(self.today + timedelta(days=30)),
                  "min_nights": 2, "max_nights": 5, "room_type_ids": [self.room_type.pk],
                  "terms": "Subject to availability", "is_active": True}
        values.update(overrides)
        return values

    def test_general_offer_creation_and_public_listing(self):
        response = self.client.post("/api/admin/offers/", self.general(), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        offer = Offer.objects.get(pk=response.json()["data"]["id"])
        self.assertEqual(list(offer.room_types.all()), [self.room_type])
        self.unauth()
        listing = self.client.get("/api/offers/")
        self.assertEqual(listing.status_code, 200, listing.content)
        self.assertIn("Special", str(listing.json()))

    def test_general_offer_rejects_invalid_values(self):
        for overrides in ({"discount_value": "0"}, {"discount_value": "101"},
                          {"min_nights": 0}, {"min_nights": 4, "max_nights": 3}):
            with self.subTest(overrides=overrides):
                result = self.client.post("/api/admin/offers/", self.general(**overrides), format="json")
                self.assertEqual(result.status_code, 400, result.content)

    def test_guest_offer_is_private_and_bound_to_selected_guest(self):
        payload = {"guest": self.guest.pk, "discount_type": "FIXED_AMOUNT",
                   "discount_value": "1200.00", "start_date": str(self.today),
                   "reason": "service gesture", "notes": "staff only"}
        response = self.client.post("/api/admin/guest-discounts/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(GuestDiscount.objects.get().guest, self.guest)
        self.unauth()
        listing = self.client.get("/api/offers/")
        self.assertNotIn("service gesture", str(listing.json()))
        self.assertNotIn("staff only", str(listing.json()))
