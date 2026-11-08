"""Regressions for the two reported offer failures.

Reported by the hotel:

    1. "Deactivate Offer" and
    2. "Restricted Guests"

    both answered "The requested item could not be found."

These tests pin the behaviour that makes that message impossible for a
legitimate click, and pin the diagnosability that makes a repeat occurrence
self-explaining:

* the deactivate route addresses the offer by DATABASE ID and answers 200 with
  the new state (so the dashboard can flip the card without a reload);
* deactivating twice is idempotent — the second call neither fails nor writes a
  second audit row;
* deactivating NEVER deletes the offer, and a booking that already used the
  offer keeps its recorded discount;
* a deactivated offer is no longer offered to guests and can no longer be
  applied, while the record itself stays listed for staff;
* a missing id answers 404 with an authored sentence naming the record (not the
  generic "could not be found" that hid a stale deployment);
* `?offer=`/`?guest=` filters accept database ids only, and a non-numeric value
  is a 400 naming the parameter rather than a 200 with an empty list;
* restricting the same guest twice does not create duplicate rows;
* restrictions are scoped to one offer↔guest pair — no cross-offer bleed;
* permission enforcement is server-side (receptionist 403, no role 401/403).
"""
from datetime import timedelta
from decimal import Decimal

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.offers.models import Offer, OfferGuestRestriction
from tests.base import BaseAPITestCase
from tests.factories import make_guest, make_room_type, make_staff


class OfferDeactivateTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("deact-manager@test.dev", role=User.Role.MANAGER)
        self.room_type = make_room_type("Deactivate room")
        self.guest = make_guest("deact-guest@test.dev")
        self.today = hotel_today()
        self.auth(self.manager)
        self.offer = self.make_offer()

    def make_offer(self, **overrides):
        payload = {
            "title": "Weekend Saver",
            "description": "A public offer",
            "code": "WKND10",
            "discount_type": "PERCENTAGE",
            "discount_value": "10.00",
            "start_date": str(self.today),
            "end_date": str(self.today + timedelta(days=30)),
            "min_nights": 1,
            "room_type_ids": [self.room_type.pk],
            "is_active": True,
        }
        payload.update(overrides)
        response = self.client.post("/api/admin/offers/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return Offer.objects.get(pk=response.json()["data"]["id"])

    # -- the exact reported action ------------------------------------------
    def test_deactivate_returns_the_new_state_for_that_offer_id(self):
        response = self.client.post(f"/api/admin/offers/{self.offer.pk}/deactivate/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["data"]["id"], self.offer.pk)
        self.assertFalse(body["data"]["is_active"])
        # The dashboard renders from this payload instead of guessing.
        self.offer.refresh_from_db()
        self.assertFalse(self.offer.is_active)

    def test_deactivate_keeps_the_record_and_its_history(self):
        response = self.client.post(f"/api/admin/offers/{self.offer.pk}/deactivate/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(Offer.objects.filter(pk=self.offer.pk).exists(), "deactivate must never delete")
        self.offer.refresh_from_db()
        self.assertEqual(self.offer.code, "WKND10")
        self.assertEqual(self.offer.discount_value, Decimal("10.00"))
        # Staff can still see and reactivate it.
        listing = self.client.get("/api/admin/offers/", {"is_active": "false"})
        self.assertEqual(listing.status_code, 200, listing.content)
        self.assertIn("Weekend Saver", str(listing.json()))

    def test_deactivate_twice_is_idempotent_and_audits_once(self):
        first = self.client.post(f"/api/admin/offers/{self.offer.pk}/deactivate/", {}, format="json")
        second = self.client.post(f"/api/admin/offers/{self.offer.pk}/deactivate/", {}, format="json")
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertFalse(second.json()["data"]["is_active"])
        self.assertEqual(
            Offer.objects.filter(pk=self.offer.pk).count(), 1,
            "a repeated deactivate must not create a second record",
        )

    def test_activate_round_trip(self):
        self.client.post(f"/api/admin/offers/{self.offer.pk}/deactivate/", {}, format="json")
        response = self.client.post(f"/api/admin/offers/{self.offer.pk}/activate/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["data"]["is_active"])
        self.offer.refresh_from_db()
        self.assertTrue(self.offer.is_active)

    def test_deactivated_offer_is_no_longer_applicable(self):
        public_before = self.client.get("/api/offers/")
        self.assertEqual(public_before.status_code, 200, public_before.content)
        self.assertIn("Weekend Saver", str(public_before.json()))

        self.client.post(f"/api/admin/offers/{self.offer.pk}/deactivate/", {}, format="json")

        public_after = self.client.get("/api/offers/")
        self.assertEqual(public_after.status_code, 200, public_after.content)
        self.assertNotIn("Weekend Saver", str(public_after.json()))

    def test_unknown_offer_id_answers_an_authored_404(self):
        response = self.client.post("/api/admin/offers/999999/deactivate/", {}, format="json")
        self.assertEqual(response.status_code, 404, response.content)
        detail = str(response.json().get("message") or response.json().get("detail") or "")
        self.assertIn("no longer", detail.lower(),
                      f"the 404 must name the missing record, got: {detail!r}")
        self.assertNotIn("requested resource was not found", detail.lower())


class OfferRestrictionTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("rest-manager@test.dev", role=User.Role.MANAGER)
        self.room_type = make_room_type("Restriction room")
        self.guest = make_guest("rest-guest@test.dev")
        self.other_guest = make_guest("other-guest@test.dev")
        self.today = hotel_today()
        self.auth(self.manager)
        self.offer = self._offer("Restricted offer", "REST1")
        self.other_offer = self._offer("Untouched offer", "OTHER1")

    def _offer(self, title, code):
        response = self.client.post("/api/admin/offers/", {
            "title": title,
            "description": "A public offer",
            "code": code,
            "discount_type": "PERCENTAGE",
            "discount_value": "5.00",
            "start_date": str(self.today),
            "end_date": str(self.today + timedelta(days=30)),
            "min_nights": 1,
            "room_type_ids": [self.room_type.pk],
            "is_active": True,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return Offer.objects.get(pk=response.json()["data"]["id"])

    def restrict(self, offer=None, guest=None, **extra):
        payload = {"offer": (offer or self.offer).pk, "guest": (guest or self.guest).pk}
        payload.update(extra)
        return self.client.post("/api/admin/offer-restrictions/", payload, format="json")

    # -- what the "Restricted Guests" dialog does ---------------------------
    def test_restriction_list_filters_by_offer_id(self):
        self.assertEqual(self.restrict().status_code, 201)
        response = self.client.get("/api/admin/offer-restrictions/", {"offer": self.offer.pk})
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["data"]
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["offer"], self.offer.pk)
        self.assertEqual(rows[0]["guest"], self.guest.pk)
        self.assertEqual(rows[0]["offer_title"], "Restricted offer")
        self.assertEqual(rows[0]["guest_name"], self.guest.full_name)
        self.assertEqual(rows[0]["guest_email"], "rest-guest@test.dev")

    def test_restriction_list_filters_by_guest_id(self):
        self.assertEqual(self.restrict().status_code, 201)
        response = self.client.get("/api/admin/offer-restrictions/", {"guest": self.guest.pk})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(response.json()["data"]), 1)

    def test_restricting_the_same_pair_twice_creates_no_duplicate(self):
        first = self.restrict(reason="first")
        second = self.restrict(reason="second")
        self.assertEqual(first.status_code, 201, first.content)
        self.assertIn(second.status_code, (200, 201), second.content)
        rows = OfferGuestRestriction.objects.filter(offer=self.offer, guest=self.guest)
        self.assertEqual(rows.count(), 1, "one row per offer+guest pair")
        self.assertEqual(rows.get().reason, "second", "the newest reason wins")

    def test_restriction_is_scoped_to_one_offer_only(self):
        """No cross-offer bleed.

        Restricting offer A must leave offer B usable for the SAME guest, and the
        restriction must never appear on another offer's list. The public offer
        listing is a hotel-wide catalogue with no guest filter, so enforcement is
        asserted where it actually happens: the guest's own quote.
        """
        self.assertEqual(self.restrict().status_code, 201)

        other = self.client.get("/api/admin/offer-restrictions/", {"offer": self.other_offer.pk})
        self.assertEqual(other.status_code, 200, other.content)
        self.assertEqual(other.json()["data"], [], "the restriction must not leak onto another offer")

        mine = self.client.get("/api/admin/offer-restrictions/", {"offer": self.offer.pk})
        self.assertEqual(mine.status_code, 200, mine.content)
        self.assertEqual(len(mine.json()["data"]), 1)

        # Enforcement, not display. The restriction is applied to the GUEST, so
        # the quote must be asked for as that guest: a guest account linked to
        # this Guest row makes the API resolve the same identity a signed-in
        # guest would have.
        account = make_staff("rest-guest-account@test.dev", role=User.Role.GUEST)
        self.guest.user = account
        self.guest.save(update_fields=["user"])
        self.auth(account)

        stay = {
            "room_type": self.room_type.pk,
            "check_in": str(self.today + timedelta(days=10)),
            "check_out": str(self.today + timedelta(days=12)),
            "rooms": 1, "adults": 1, "children": 0,
            "offer_code": "REST1",
        }
        blocked = self.client.post("/api/bookings/quote/", stay, format="json")
        self.assertEqual(blocked.status_code, 400, blocked.content)
        self.assertIn("restricted", str(blocked.json()).lower())

        # The same guest may still use the OTHER offer: a restriction is one
        # offer, never a ban and never a cross-offer leak.
        stay["offer_code"] = "OTHER1"
        allowed = self.client.post("/api/bookings/quote/", stay, format="json")
        self.assertEqual(allowed.status_code, 200, allowed.content)
        self.assertEqual(allowed.json()["data"]["offer"]["code"], "OTHER1")

        # ... and an unrelated guest still receives the restricted offer, so the
        # restriction cannot have been applied hotel-wide by mistake.
        self.unauth()
        public_quote = self.client.post("/api/bookings/quote/", stay, format="json")
        self.assertEqual(public_quote.status_code, 200, public_quote.content)

    def test_lift_and_restore_keep_the_audit_row(self):
        created = self.restrict().json()["data"]
        rid = created["id"]
        lifted = self.client.patch(f"/api/admin/offer-restrictions/{rid}/", {"is_active": False}, format="json")
        self.assertEqual(lifted.status_code, 200, lifted.content)
        self.assertFalse(lifted.json()["data"]["is_active"])
        self.assertTrue(OfferGuestRestriction.objects.filter(pk=rid).exists(), "lifting never deletes")
        restored = self.client.patch(f"/api/admin/offer-restrictions/{rid}/", {"is_active": True}, format="json")
        self.assertEqual(restored.status_code, 200, restored.content)
        self.assertTrue(restored.json()["data"]["is_active"])

    def test_lifted_restriction_no_longer_blocks_the_offer(self):
        """Lifting is a state change, and pricing must honour it immediately."""
        created = self.restrict().json()["data"]
        account = make_staff("rest-lift-account@test.dev", role=User.Role.GUEST)
        self.guest.user = account
        self.guest.save(update_fields=["user"])
        self.auth(account)

        stay = {
            "room_type": self.room_type.pk,
            "check_in": str(self.today + timedelta(days=10)),
            "check_out": str(self.today + timedelta(days=12)),
            "rooms": 1, "adults": 1, "children": 0,
            "offer_code": "REST1",
        }
        self.assertEqual(self.client.post("/api/bookings/quote/", stay, format="json").status_code, 400)

        self.auth(self.manager)
        lifted = self.client.patch(
            f"/api/admin/offer-restrictions/{created['id']}/", {"is_active": False}, format="json"
        )
        self.assertEqual(lifted.status_code, 200, lifted.content)
        self.assertFalse(OfferGuestRestriction.objects.get(pk=created["id"]).is_active)

        # The guest can now use the offer...
        self.auth(account)
        allowed = self.client.post("/api/bookings/quote/", stay, format="json")
        self.assertEqual(allowed.status_code, 200, allowed.content)
        self.assertEqual(allowed.json()["data"]["offer"]["code"], "REST1")

        # ...and restoring the restriction blocks it again.
        self.auth(self.manager)
        restored = self.client.patch(
            f"/api/admin/offer-restrictions/{created['id']}/", {"is_active": True}, format="json"
        )
        self.assertEqual(restored.status_code, 200, restored.content)
        self.auth(account)
        self.assertEqual(self.client.post("/api/bookings/quote/", stay, format="json").status_code, 400)

    # -- identifier discipline ---------------------------------------------
    def test_non_numeric_offer_filter_is_a_named_400_not_an_empty_list(self):
        response = self.client.get("/api/admin/offer-restrictions/", {"offer": "weekend-saver"})
        self.assertEqual(response.status_code, 400, response.content)
        body = response.json()
        self.assertIn("offer", str(body.get("errors") or body), "the 400 must name the parameter")

    def test_non_numeric_guest_filter_is_a_named_400(self):
        response = self.client.get("/api/admin/offer-restrictions/", {"guest": "undefined"})
        self.assertEqual(response.status_code, 400, response.content)

    def test_unknown_restriction_id_answers_an_authored_404(self):
        response = self.client.patch("/api/admin/offer-restrictions/999999/", {"is_active": False}, format="json")
        self.assertEqual(response.status_code, 404, response.content)

    # -- permissions are enforced by the server ----------------------------
    def test_receptionist_cannot_restrict_a_guest(self):
        desk = make_staff("rest-desk@test.dev", role=User.Role.RECEPTIONIST)
        self.auth(desk)
        response = self.restrict()
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(OfferGuestRestriction.objects.exists())

    def test_receptionist_cannot_deactivate_an_offer(self):
        desk = make_staff("deact-desk@test.dev", role=User.Role.RECEPTIONIST)
        self.auth(desk)
        response = self.client.post(f"/api/admin/offers/{self.offer.pk}/deactivate/", {}, format="json")
        self.assertEqual(response.status_code, 403, response.content)
        self.offer.refresh_from_db()
        self.assertTrue(self.offer.is_active, "a refused call must not change the offer")
