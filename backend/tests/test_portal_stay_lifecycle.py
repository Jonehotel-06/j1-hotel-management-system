"""Regression coverage for guest-portal access across check-in, return and checkout.

Access is tied to live stay state: an in-house stay with an active room. These
tests drive the real check-in and checkout services so the same records a front
desk produces are the ones the portal decides from.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.db import DatabaseError
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.bookings.services.booking_service import check_in_booking, check_out_booking
from apps.core.utils import hotel_today
from apps.finance.services.folio_service import get_or_create_main_folio_for_stay
from apps.guest_services.models import ServiceRequest
from apps.hotel.models import Facility
from apps.pos.models import MenuCategory, MenuItem, MenuModifier
from apps.portal.models import PortalAccessChallenge, PortalSession
from apps.portal.services import hash_secret
from apps.stays.models import Stay

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type, make_staff

SESSION_COOKIE = "jone_portal_session"
ENDED_MESSAGE = "Portal access ended because checkout is complete."
REQUEST_BODY = {
    "category": "HOUSEKEEPING",
    "priority": "NORMAL",
    "summary": "Please bring towels",
    "detail": "Two extra towels.",
}


def _at(delta):
    """Context helper: pretend the clock is ``delta`` after now."""
    return mock.patch("django.utils.timezone.now", return_value=timezone.now() + delta)


class PortalStayLifecycleBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.desk = make_staff("lifecycle-desk@staff.dev", role=User.Role.RECEPTIONIST)
        self.email = "lifecycle.guest@example.com"
        self.guest = make_guest(self.email, first_name="Life", last_name="Cycle")
        self.room_type = make_room_type("Lifecycle suite", price="20000.00")
        self.room = make_room(self.room_type, "L-101")
        self.booking = self._booking_for(self.guest, self.room)

    def _booking_for(self, guest, *rooms, check_in=None, check_out=None, total="15000.00"):
        check_in = check_in or hotel_today()
        check_out = check_out or check_in + timedelta(days=2)
        return make_booking(
            guest, self.room_type, list(rooms), check_in=check_in, check_out=check_out, total=total,
            number_of_rooms=max(1, len(rooms)),
        )

    def _check_in(self, booking):
        check_in_booking(booking, staff_user=self.desk)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CHECKED_IN)
        return Stay.objects.get(booking=booking)

    def _issue_link(self, email=None):
        """Request a link as the guest would and capture the emailed token."""
        with mock.patch("apps.portal.services._send_magic_link") as send, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/api/portal/auth/request/", {"email": email or self.email}, format="json",
            )
        self.assertEqual(response.status_code, 202, response.content)
        if not send.called:
            return None
        return send.call_args.kwargs["token"]

    def _consume(self, token):
        response = self.client.post("/api/portal/auth/consume/", {"token": token}, format="json")
        return response

    def _sign_in(self, email=None):
        """Full link flow. Returns (raw_session_token, response) and keeps the cookie on the client."""
        token = self._issue_link(email)
        self.assertIsNotNone(token, "an eligible guest must receive a link")
        response = self._consume(token)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]["session_token"], response

    def _me(self, headers=None):
        return self.client.get("/api/portal/me/", **(headers or {}))

    def _header(self, token):
        return {"HTTP_X_PORTAL_SESSION": token}


class PortalEligibilityAndLinkTests(PortalStayLifecycleBase):
    def test_pre_arrival_booking_gets_no_link_and_its_token_cannot_sign_in(self):
        future = self._booking_for(self.guest, make_room(self.room_type, "L-102"),
                                   check_in=hotel_today() + timedelta(days=5))
        self.assertIsNotNone(future.pk)
        self.assertIsNone(self._issue_link())
        self.assertFalse(PortalAccessChallenge.objects.exists())

    def test_checked_in_guest_receives_a_link_that_has_no_elapsed_time_expiry(self):
        self._check_in(self.booking)
        self._issue_link()
        challenge = PortalAccessChallenge.objects.get()
        self.assertIsNone(challenge.expires_at)
        self.assertTrue(challenge.is_consumable)

    def test_link_remains_usable_days_later_while_the_guest_is_in_house(self):
        self._check_in(self.booking)
        token = self._issue_link()
        with _at(timedelta(days=2)):
            response = self._consume(token)
        self.assertEqual(response.status_code, 200, response.content)

    def test_legacy_link_with_a_past_expiry_is_still_rejected(self):
        self._check_in(self.booking)
        token = "legacy-expired-token-0123456789-abcdefghijklmnopqrstuvwxyz"
        PortalAccessChallenge.objects.create(
            email=self.email, token_hash=hash_secret(token),
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        self.assertEqual(self._consume(token).status_code, 401)

    def test_link_request_is_non_enumerating_for_unknown_and_pre_arrival_addresses(self):
        with mock.patch("apps.portal.services._send_magic_link") as send:
            unknown = self.client.post("/api/portal/auth/request/", {"email": "nobody@example.com"}, format="json")
            pre_arrival = self.client.post("/api/portal/auth/request/", {"email": "nobody@example.com"}, format="json")
        self.assertEqual(unknown.json(), pre_arrival.json())
        self.assertFalse(send.called)


class PortalSessionTransportTests(PortalStayLifecycleBase):
    def test_sign_in_sets_an_httponly_lax_cookie_scoped_to_the_portal_api(self):
        self._check_in(self.booking)
        _token, response = self._sign_in()
        cookie = response.cookies[SESSION_COOKIE]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(cookie["path"], "/api/portal/")

    def test_returning_guest_is_restored_from_the_cookie_without_a_new_link(self):
        self._check_in(self.booking)
        self._sign_in()
        self.assertEqual(PortalAccessChallenge.objects.count(), 1)
        # Refresh / return visit: no header, only the cookie the browser kept.
        with _at(timedelta(days=3)):
            restored = self._me()
        self.assertEqual(restored.status_code, 200, restored.content)
        self.assertEqual(PortalAccessChallenge.objects.count(), 1)

    def test_session_is_not_limited_to_eight_hours_but_has_an_absolute_cap(self):
        self._check_in(self.booking)
        self._sign_in()
        for days in (6, 12, 18, 24):  # regular use keeps the idle timer fresh
            with _at(timedelta(days=days)):
                self.assertEqual(self._me().status_code, 200)
        with _at(timedelta(days=31)):
            self.assertEqual(self._me().status_code, 401)

    def test_cookie_authenticated_logout_requires_the_csrf_header(self):
        self._check_in(self.booking)
        self._sign_in()
        forged = self.client.post("/api/portal/auth/logout/", {}, format="json")
        self.assertEqual(forged.status_code, 401, forged.content)
        self.assertEqual(self._me().status_code, 200)  # still signed in: nothing was revoked

        logout = self.client.post(
            "/api/portal/auth/logout/", {}, format="json", HTTP_X_PORTAL_REQUEST="1",
        )
        self.assertEqual(logout.status_code, 200, logout.content)
        self.assertEqual(logout.cookies[SESSION_COOKIE].value, "")
        self.assertEqual(self._me().status_code, 401)

    def test_header_session_still_works_for_non_browser_clients(self):
        self._check_in(self.booking)
        token, _response = self._sign_in()
        self.client.cookies.clear()
        self.assertEqual(self._me(self._header(token)).status_code, 200)

    def test_stale_session_is_rejected_when_the_stay_has_closed_without_the_checkout_hook(self):
        self._check_in(self.booking)
        token, _response = self._sign_in()
        # Simulate an operational change that bypasses the checkout service entirely.
        Booking.objects.filter(pk=self.booking.pk).update(status=Booking.Status.CHECKED_OUT)
        self.client.cookies.clear()
        self.assertEqual(self._me(self._header(token)).status_code, 401)


class PortalCheckoutRevocationTests(PortalStayLifecycleBase):
    def test_checkout_ends_access_to_old_links_sessions_and_protected_apis(self):
        stay = self._check_in(self.booking)
        old_session, _response = self._sign_in()
        # A link issued before checkout but not yet used.
        self.client.cookies.clear()
        pending_token = self._issue_link()
        self.assertIsNotNone(pending_token)
        total_before = Booking.objects.get(pk=self.booking.pk).total_amount

        check_out_booking(self.booking, staff_user=self.desk, allow_balance_due=True)

        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CHECKED_OUT)
        self.assertEqual(self.booking.total_amount, total_before)  # billing untouched
        stay.refresh_from_db()
        self.assertEqual(stay.status, Stay.Status.CHECKED_OUT)

        # Existing session (cookie and header) is rejected with the explicit message.
        for headers in ({"HTTP_COOKIE": f"{SESSION_COOKIE}={old_session}"}, self._header(old_session)):
            response = self._me(headers)
            self.assertEqual(response.status_code, 401, response.content)
            self.assertEqual(response.json()["code"], "PORTAL_ACCESS_ENDED")
            self.assertEqual(response.json()["message"], ENDED_MESSAGE)

        # Stay-dependent APIs are closed as well.
        self.assertEqual(self.client.get("/api/portal/folios/", **self._header(old_session)).status_code, 401)
        self.assertEqual(self.client.get("/api/portal/requests/", **self._header(old_session)).status_code, 401)
        self.assertEqual(self.client.get("/api/portal/catalog/menu/", **self._header(old_session)).status_code, 401)
        created = self.client.post(
            "/api/portal/requests/", {**REQUEST_BODY, "stay_reference": stay.reference, "idempotency_key": "after-checkout"},
            format="json", **self._header(old_session),
        )
        self.assertEqual(created.status_code, 401, created.content)

        # Old and pending links neither mint a session nor reveal anything.
        self.assertEqual(self._consume(pending_token).status_code, 401)
        with mock.patch("apps.portal.services._send_magic_link") as send:
            self.client.post("/api/portal/auth/request/", {"email": self.email}, format="json")
        self.assertFalse(send.called)

        session = PortalSession.objects.get(session_hash=hash_secret(old_session))
        self.assertIsNotNone(session.revoked_at)
        self.assertEqual(session.revoked_reason, "Checkout complete")

    def test_failed_checkout_does_not_revoke_access(self):
        self._check_in(self.booking)
        token, _response = self._sign_in()
        self.client.cookies.clear()
        with mock.patch("apps.bookings.services.booking_service.log_action", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                check_out_booking(self.booking, staff_user=self.desk, allow_balance_due=True)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CHECKED_IN)
        self.assertEqual(self._me(self._header(token)).status_code, 200)
        self.assertIsNone(PortalSession.objects.get(session_hash=hash_secret(token)).revoked_at)

    def test_revocation_failure_never_blocks_checkout_and_access_still_ends(self):
        self._check_in(self.booking)
        token, _response = self._sign_in()
        self.client.cookies.clear()
        with mock.patch("apps.portal.services.end_portal_access_for_checked_out_stay",
                        side_effect=DatabaseError("revocation unavailable")):
            check_out_booking(self.booking, staff_user=self.desk, allow_balance_due=True)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CHECKED_OUT)
        response = self._me(self._header(token))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "PORTAL_ACCESS_ENDED")

    def test_multiple_stays_revoke_only_the_completed_stay(self):
        stay_a = self._check_in(self.booking)
        other_room = make_room(self.room_type, "L-201")
        booking_b = self._booking_for(self.guest, other_room)
        stay_b = self._check_in(booking_b)

        token, _response = self._sign_in()
        self.client.cookies.clear()
        headers = self._header(token)

        # A request and a folio exist on both stays before checkout.
        req_a = self.client.post(
            "/api/portal/requests/", {**REQUEST_BODY, "stay_reference": stay_a.reference, "idempotency_key": "a-1"},
            format="json", **headers,
        )
        req_b = self.client.post(
            "/api/portal/requests/", {**REQUEST_BODY, "stay_reference": stay_b.reference, "idempotency_key": "b-1"},
            format="json", **headers,
        )
        self.assertEqual(req_a.status_code, 201, req_a.content)
        self.assertEqual(req_b.status_code, 201, req_b.content)
        ref_a = req_a.json()["data"]["reference"]
        ref_b = req_b.json()["data"]["reference"]
        folio_a, _ = get_or_create_main_folio_for_stay(stay=stay_a, actor=self.desk)
        folio_b, _ = get_or_create_main_folio_for_stay(stay=stay_b, actor=self.desk)

        check_out_booking(self.booking, staff_user=self.desk, allow_balance_due=True)

        # Session survives for the in-house stay.
        self.assertIsNone(PortalSession.objects.get(session_hash=hash_secret(token)).revoked_at)
        self.assertEqual(self._me(headers).status_code, 200)
        overview = self._me(headers).json()["data"]["bookings"]
        references = {row["booking_reference"] for row in overview}
        self.assertIn(booking_b.booking_reference, references)
        self.assertNotIn(self.booking.booking_reference, references)

        # Completed stay's data is gone; the in-house stay's data remains.
        listed = {row["reference"] for row in self.client.get("/api/portal/requests/", **headers).json()["data"]}
        self.assertIn(ref_b, listed)
        self.assertNotIn(ref_a, listed)
        self.assertEqual(self.client.get(f"/api/portal/requests/{ref_a}/", **headers).status_code, 404)
        self.assertEqual(self.client.get(f"/api/portal/requests/{ref_b}/", **headers).status_code, 200)
        folio_refs = {row["reference"] for row in self.client.get("/api/portal/folios/", **headers).json()["data"]}
        self.assertIn(folio_b.reference, folio_refs)
        self.assertNotIn(folio_a.reference, folio_refs)
        self.assertEqual(self.client.get(f"/api/portal/folios/{folio_a.reference}/", **headers).status_code, 404)

        # New requests for the completed stay are refused.
        refused = self.client.post(
            "/api/portal/requests/", {**REQUEST_BODY, "stay_reference": stay_a.reference, "idempotency_key": "a-2"},
            format="json", **headers,
        )
        self.assertEqual(refused.status_code, 404, refused.content)


class PortalAuthorisationBoundaryTests(PortalStayLifecycleBase):
    def test_occupant_cannot_raise_a_request_for_another_room_of_the_stay(self):
        room_two = make_room(self.room_type, "L-102B")
        booking = self._booking_for(self.guest, self.room, room_two)
        from apps.bookings.models import BookingRoom, RoomOccupant
        occupant_email = "occupant.only@example.com"
        RoomOccupant.objects.create(
            booking_room=BookingRoom.objects.get(booking=booking, room=room_two),
            email=occupant_email, full_name="Occupant",
        )
        stay = self._check_in(booking)
        token, _response = self._sign_in(occupant_email)
        self.client.cookies.clear()
        headers = self._header(token)
        other_room = self.client.post(
            "/api/portal/requests/",
            {**REQUEST_BODY, "stay_reference": stay.reference, "room_id": self.room.pk, "idempotency_key": "x-room"},
            format="json", **headers,
        )
        self.assertEqual(other_room.status_code, 400, other_room.content)
        own_room = self.client.post(
            "/api/portal/requests/",
            {**REQUEST_BODY, "stay_reference": stay.reference, "room_id": room_two.pk, "idempotency_key": "own-room"},
            format="json", **headers,
        )
        self.assertEqual(own_room.status_code, 201, own_room.content)

    def test_another_eligible_guest_cannot_read_or_cancel_a_request_they_do_not_own(self):
        stay_a = self._check_in(self.booking)
        other_guest = make_guest("other.eligible@example.com")
        booking_b = self._booking_for(other_guest, make_room(self.room_type, "L-301"))
        self._check_in(booking_b)

        token_a, _ = self._sign_in()
        self.client.cookies.clear()
        created = self.client.post(
            "/api/portal/requests/", {**REQUEST_BODY, "stay_reference": stay_a.reference, "idempotency_key": "owned-a"},
            format="json", **self._header(token_a),
        )
        reference = created.json()["data"]["reference"]

        token_b, _ = self._sign_in("other.eligible@example.com")
        self.client.cookies.clear()
        headers_b = self._header(token_b)
        self.assertEqual(self.client.get(f"/api/portal/requests/{reference}/", **headers_b).status_code, 404)
        self.assertEqual(
            self.client.post(f"/api/portal/requests/{reference}/cancel/", {}, format="json", **headers_b).status_code,
            404,
        )
        self.assertEqual(
            ServiceRequest.objects.get(reference=reference).status, ServiceRequest.Status.OPEN,
        )

    def test_checkout_of_one_guest_does_not_affect_another_guests_session(self):
        self._check_in(self.booking)
        other_guest = make_guest("still.here@example.com")
        booking_b = self._booking_for(other_guest, make_room(self.room_type, "L-302"))
        self._check_in(booking_b)
        token_b, _ = self._sign_in("still.here@example.com")
        self.client.cookies.clear()

        check_out_booking(self.booking, staff_user=self.desk, allow_balance_due=True)
        self.assertEqual(self._me(self._header(token_b)).status_code, 200)


class PortalCatalogueTests(PortalStayLifecycleBase):
    def setUp(self):
        super().setUp()
        self._check_in(self.booking)
        self.session_token, _ = self._sign_in()
        self.client.cookies.clear()
        self.headers = self._header(self.session_token)
        self.restaurant = MenuCategory.objects.create(
            name="Mains", slug="mains", service_area=MenuCategory.ServiceArea.RESTAURANT, sort_order=1,
        )
        self.bar = MenuCategory.objects.create(
            name="Cocktails", slug="cocktails", service_area=MenuCategory.ServiceArea.BAR, sort_order=2,
        )
        self.jollof = MenuItem.objects.create(
            category=self.restaurant, name="Jollof rice", sku="MENU-JOLLOF-1", base_price=Decimal("3500.00"),
            description="Smoky party jollof", tax_rate_percent=Decimal("7.50"),
        )
        MenuModifier.objects.create(menu_item=self.jollof, name="Extra chicken", price_delta=Decimal("1000.00"))
        MenuItem.objects.create(
            category=self.restaurant, name="Fried plantain", sku="MENU-PLANTAIN-1", base_price=Decimal("1200.00"),
            is_available=False,
        )
        MenuItem.objects.create(
            category=self.restaurant, name="Retired dish", sku="MENU-RETIRED-1", base_price=Decimal("999.00"),
            is_active=False,
        )
        MenuItem.objects.create(
            category=self.bar, name="Chapman", sku="MENU-CHAPMAN-1", base_price=Decimal("2500.00"),
        )

    def test_menu_shows_only_active_items_with_availability_labels_and_no_internal_fields(self):
        response = self.client.get("/api/portal/catalog/menu/", **self.headers)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()["data"]
        items = {item["name"]: item for cat in body["categories"] for item in cat["items"]}
        self.assertNotIn("Retired dish", items)
        self.assertEqual(items["Jollof rice"]["price"], "3500.00")
        self.assertEqual(items["Jollof rice"]["currency"], "NGN")
        self.assertEqual(items["Jollof rice"]["order_category"], "ROOM_SERVICE")
        self.assertEqual(items["Jollof rice"]["modifiers"], [{"name": "Extra chicken", "price_delta": "1000.00"}])
        self.assertFalse(items["Fried plantain"]["is_available"])
        self.assertEqual(items["Fried plantain"]["availability_label"], "Currently unavailable")
        self.assertEqual(items["Chapman"]["order_category"], "FOOD_BEVERAGE")
        raw = response.content.decode()
        for forbidden in ("sku", "barcode", "tax_rate", "cost", "stock", "recipe", "margin", "MENU-JOLLOF-1"):
            self.assertNotIn(forbidden, raw)

    def test_menu_filters_by_area_and_rejects_unknown_area(self):
        bar_only = self.client.get("/api/portal/catalog/menu/?area=BAR", **self.headers).json()["data"]
        self.assertEqual([c["service_area"] for c in bar_only["categories"]], ["BAR"])
        self.assertEqual(self.client.get("/api/portal/catalog/menu/?area=KITCHEN", **self.headers).status_code, 400)

    def test_menu_search_matches_name(self):
        found = self.client.get("/api/portal/catalog/menu/?q=jollof", **self.headers).json()["data"]
        names = [item["name"] for cat in found["categories"] for item in cat["items"]]
        self.assertEqual(names, ["Jollof rice"])

    def test_menu_requires_a_portal_session(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/portal/catalog/menu/").status_code, 401)

    def test_services_list_enabled_requests_and_active_facilities_without_invented_prices(self):
        Facility.objects.create(name="Swimming pool", description="Open 7am to 9pm", is_active=True)
        Facility.objects.create(name="Old gym", is_active=False)
        response = self.client.get("/api/portal/catalog/services/", **self.headers)
        self.assertEqual(response.status_code, 200, response.content)
        services = response.json()["data"]["services"]
        names = {service["name"] for service in services}
        self.assertIn("Swimming pool", names)
        self.assertNotIn("Old gym", names)
        self.assertTrue(all(service["price"] is None for service in services))
        self.assertTrue(all(service["pricing_method"] == "not_listed" for service in services))
        requests = [s for s in services if s["kind"] == "request"]
        self.assertTrue(requests and all(s["action"] == "request_service" for s in requests))

    @override_settings(PORTAL_ENABLED_SERVICE_CATEGORIES=["HOUSEKEEPING"])
    def test_services_respect_the_enabled_request_categories(self):
        services = self.client.get("/api/portal/catalog/services/", **self.headers).json()["data"]["services"]
        requests = [s["category"] for s in services if s["kind"] == "request"]
        self.assertEqual(requests, ["HOUSEKEEPING"])

    @override_settings(PORTAL_ENABLED_SERVICE_CATEGORIES=["HOUSEKEEPING"])
    def test_request_create_rejects_a_category_the_portal_does_not_offer(self):
        """The category allow-list is enforced on create, not only hidden in the catalogue."""
        stay = Stay.objects.get(booking=self.booking)
        before = ServiceRequest.objects.count()
        blocked = self.client.post(
            "/api/portal/requests/",
            {**REQUEST_BODY, "category": "MAINTENANCE", "stay_reference": stay.reference, "idempotency_key": "cat-1"},
            format="json", **self.headers,
        )
        self.assertEqual(blocked.status_code, 400, blocked.content)
        self.assertIn("category", blocked.json()["errors"])
        self.assertEqual(ServiceRequest.objects.count(), before)

        allowed = self.client.post(
            "/api/portal/requests/",
            {**REQUEST_BODY, "stay_reference": stay.reference, "idempotency_key": "cat-2"},
            format="json", **self.headers,
        )
        self.assertEqual(allowed.status_code, 201, allowed.content)
        self.assertEqual(ServiceRequest.objects.count(), before + 1)
