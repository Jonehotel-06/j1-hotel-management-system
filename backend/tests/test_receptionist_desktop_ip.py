"""Receptionist Desktop by registered address (no code to enter at sign-in).

The front desk PC may be enrolled two ways. The key route (a secret pasted once
into that browser) is covered by test_receptionist_desktop_sign_in.py. This
suite covers the second route: an administrator registers the address the desk
reaches the API from, and staff signing in FROM that desk need nothing else.

Two properties carry the weight here and are tested hardest:

* an address is only believed when the server can trust where it came from, so
  a client cannot nominate its own address through a forwarding header; and
* registering one desk must not open the door for every other device.
"""
from django.core.cache import cache
from django.test import override_settings

from apps.accounts.desktop_policy import DESKTOP_HEADER, issue_desktop_key
from apps.accounts.models import User, Workstation
from apps.audit.models import AuditLog
from apps.hotel.models import HotelSettings
from apps.core.client_ip import client_ip_source, resolve_client_ip

from .base import BaseAPITestCase
from .factories import hotel_settings, make_user

PASSWORD = "Str0ng!Pass"
DESK_IP = "102.89.34.7"
OTHER_IP = "102.89.34.99"
PROXY_IP = "10.0.0.14"          # private: only the deployment's own proxy
# What the deployed host actually sends (measured on Render): the client keeps
# any X-Forwarded-For it sent, the provider appends its own Cloudflare edge and
# internal hop, and the edge headers carry the one address it really saw.
RENDER_PEER = "10.226.90.65"
RENDER_EDGE = "172.71.195.123"
DESK_ON_INTERNET = "81.107.0.110"
RENDER_HEADERS = {
    "HTTP_X_FORWARDED_FOR": f"192.0.2.44, {DESK_ON_INTERNET}, {RENDER_EDGE}, {RENDER_PEER}",
    "HTTP_TRUE_CLIENT_IP": DESK_ON_INTERNET,
    "HTTP_CF_CONNECTING_IP": DESK_ON_INTERNET,
}
LOGIN_URL = "/api/auth/login/"
SETTINGS_URL = "/api/admin/settings/"
REFRESH_URL = "/api/auth/token/refresh/"
WORKSTATIONS_URL = "/api/admin/users/terminals/"


class RegisteredDesktopIpTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.admin = make_user("ip-admin@jone.test", password=PASSWORD, role=User.Role.ADMIN)
        self.receptionist = make_user("ip-receptionist@jone.test", password=PASSWORD, role=User.Role.RECEPTIONIST)
        self.desk = Workstation.objects.create(
            name="Front desk reception 1", department=Workstation.Department.FRONT_DESK,
            location="Lobby", ip_address=DESK_IP,
        )

    def login(self, user=None, *, ip=DESK_IP, password=PASSWORD, headers=None, **meta):
        user = user or self.receptionist
        return self.client.post(
            LOGIN_URL, {"email": user.email, "password": password},
            REMOTE_ADDR=ip, **(headers or {}), **meta,
        )

    # --- The requested behaviour ------------------------------------------

    def test_a_receptionist_signs_in_with_credentials_only_from_the_registered_address(self):
        response = self.login()
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertIn("access", body["data"]["tokens"])
        self.assertEqual(body["data"]["user"]["role"], User.Role.RECEPTIONIST)

    def test_sign_in_is_still_refused_from_any_other_address(self):
        response = self.login(ip=OTHER_IP)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["code"], "STAFF_SIGN_IN_RESTRICTED")

    def test_the_refusal_names_the_address_so_an_admin_can_register_it(self):
        response = self.login(ip=OTHER_IP)
        # The desk that moved needs the new address; the refusal is where the
        # person fixing it finds it.
        self.assertEqual(response.json()["errors"]["client_ip"], OTHER_IP)
        entry = AuditLog.objects.filter(action="STAFF_SIGN_IN_RESTRICTED").latest("id")
        self.assertEqual(entry.metadata["client_ip"], OTHER_IP)

    def test_a_successful_browserless_sign_in_is_audited_with_its_method(self):
        self.login()
        entry = AuditLog.objects.filter(action="STAFF_SIGN_IN", actor=self.receptionist).latest("id")
        self.assertEqual(entry.metadata["desktop_reference"], self.desk.reference)
        self.assertEqual(entry.metadata["desktop_method"], "registered_ip")
        self.assertEqual(entry.metadata["client_ip"], DESK_IP)

    def test_the_sign_in_record_names_the_desk_not_the_deployment_proxy(self):
        # On the deployed host every request arrives from the platform proxy, so
        # the sign-in record has to carry the resolved address itself for an
        # administrator to see where the desk signed in from.
        self.login(ip=PROXY_IP, headers={"HTTP_X_FORWARDED_FOR": f"{OTHER_IP}, {DESK_IP}"})
        entry = AuditLog.objects.filter(action="STAFF_SIGN_IN", actor=self.receptionist).latest("id")
        self.assertEqual(entry.metadata["client_ip"], DESK_IP)
        self.assertNotEqual(entry.metadata["client_ip"], PROXY_IP)

    def test_wrong_password_is_still_unauthorized_from_the_desk(self):
        response = self.login(password="not-the-password")
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(response.json()["code"], "UNAUTHORIZED")

    # --- Only the desk, not the neighbourhood ------------------------------

    def test_a_workstation_in_another_department_does_not_enrol_a_desk(self):
        Workstation.objects.create(
            name="Kitchen tablet", department=Workstation.Department.KITCHEN, ip_address=OTHER_IP,
        )
        response = self.login(ip=OTHER_IP)
        self.assertEqual(response.status_code, 403, response.content)

    def test_a_deactivated_desk_stops_admitting_staff(self):
        Workstation.objects.filter(pk=self.desk.pk).update(is_active=False)
        response = self.login()
        self.assertEqual(response.status_code, 403, response.content)

    def test_a_versioned_address_is_stored_canonically_and_still_matches(self):
        # Two spellings of one address must never look like two different desks.
        Workstation.objects.filter(pk=self.desk.pk).update(
            ip_address="2001:0db8:0000:0000:0000:0000:0000:0001"
        )
        response = self.login(ip="2001:DB8::1")
        self.assertEqual(response.status_code, 200, response.content)

    def test_a_registered_network_admits_the_addresses_inside_it(self):
        # A router-handed address changes; a pinned network does not.
        self.auth(self.admin)
        saved = self.client.patch(
            f"{WORKSTATIONS_URL}{self.desk.pk}/", {"ip_address": "192.168.1.5/24"}, format="json",
        )
        self.assertEqual(saved.status_code, 200, saved.content)
        self.assertEqual(saved.json()["data"]["ip_address"], "192.168.1.0/24")
        self.assertEqual(self.login(ip="192.168.1.57").status_code, 200)
        self.assertEqual(self.login(ip="192.168.2.57").status_code, 403)

    # --- Address trust -----------------------------------------------------

    def test_a_client_cannot_nominate_its_own_address_when_the_peer_is_public(self):
        # A guest on the internet claims the desk's address. The connection did
        # not come from our proxy, so the claim is ignored and the real peer is
        # used.
        response = self.login(ip=OTHER_IP, headers={"HTTP_X_FORWARDED_FOR": DESK_IP})
        self.assertEqual(response.status_code, 403, response.content)

    def test_the_desk_is_recognized_behind_the_deployment_proxy(self):
        # Same forged prefix, but this request really did come from our proxy:
        # the proxy's appended address (the desk) is the last hop, so it wins.
        response = self.login(
            ip=PROXY_IP, headers={"HTTP_X_FORWARDED_FOR": f"{OTHER_IP}, {DESK_IP}"},
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_a_forged_prefix_behind_the_proxy_is_ignored(self):
        response = self.login(
            ip=PROXY_IP, headers={"HTTP_X_FORWARDED_FOR": f"{DESK_IP}, {OTHER_IP}"},
        )
        self.assertEqual(response.status_code, 403, response.content)

    def test_a_chain_of_our_own_proxies_is_walked_to_the_first_foreign_address(self):
        response = self.login(
            ip=PROXY_IP, headers={"HTTP_X_FORWARDED_FOR": f"{DESK_IP}, 10.0.0.9, 172.16.4.4"},
        )
        self.assertEqual(response.status_code, 200, response.content)

    # --- The deployed host (Render + Cloudflare) ---------------------------

    def test_the_deployed_host_identifies_the_desk_behind_its_edge(self):
        # A client can put anything it likes at the left of X-Forwarded-For and
        # the provider appends its own edge, so the chain alone would name a
        # Cloudflare address that differs per request. The desk is registered
        # against the address the edge reports, and that is what matches.
        Workstation.objects.filter(pk=self.desk.pk).update(ip_address=DESK_ON_INTERNET)
        response = self.login(ip=RENDER_PEER, headers=RENDER_HEADERS)
        self.assertEqual(response.status_code, 200, response.content)

    def test_the_chain_alone_is_not_mistaken_for_the_desk(self):
        # Same request with the edge headers absent: the walk lands on the
        # provider's edge address, so a desk registered for the real address is
        # NOT matched -- the wrong-but-plausible answer is not accepted.
        Workstation.objects.filter(pk=self.desk.pk).update(ip_address=DESK_ON_INTERNET)
        without_edge = {"HTTP_X_FORWARDED_FOR": RENDER_HEADERS["HTTP_X_FORWARDED_FOR"]}
        response = self.login(ip=RENDER_PEER, headers=without_edge)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["errors"]["client_ip"], RENDER_EDGE)

    def test_a_forged_edge_header_from_outside_is_ignored(self):
        # A guest on the internet claims the desk's address in the very header
        # the edge is known to write. The connection did not come from our
        # network, so nothing in the request is believed.
        response = self.login(ip=DESK_ON_INTERNET, headers={"HTTP_TRUE_CLIENT_IP": DESK_IP})
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["errors"]["client_ip"], DESK_ON_INTERNET)

    @override_settings(TRUSTED_CLIENT_IP_HEADERS=[])
    def test_a_deployment_can_turn_the_edge_header_off(self):
        # Then the forwarding chain is all that is left, and the address is
        # whatever the walk can justify.
        Workstation.objects.filter(pk=self.desk.pk).update(ip_address=DESK_ON_INTERNET)
        response = self.login(ip=RENDER_PEER, headers=RENDER_HEADERS)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["errors"]["client_ip"], RENDER_EDGE)

    def test_the_browser_is_told_which_rule_named_its_address(self):
        self.auth(self.receptionist)
        seen = self.client.get("/api/auth/client-address/", REMOTE_ADDR=RENDER_PEER, **RENDER_HEADERS)
        self.assertEqual(seen.status_code, 200, seen.content)
        self.assertEqual(seen.json()["data"]["client_ip"], DESK_ON_INTERNET)
        self.assertEqual(seen.json()["data"]["source"], "true-client-ip")

    # --- The key route keeps working, unchanged ---------------------------

    def test_the_desktop_key_route_still_works_without_a_registered_address(self):
        key_desk = Workstation.objects.create(
            name="Key-enrolled desk", department=Workstation.Department.FRONT_DESK,
        )
        key = issue_desktop_key(key_desk)
        key_desk.save()
        response = self.login(ip=OTHER_IP, headers={DESKTOP_HEADER: key})
        self.assertEqual(response.status_code, 200, response.content)
        entry = AuditLog.objects.filter(action="STAFF_SIGN_IN", actor=self.receptionist).latest("id")
        self.assertEqual(entry.metadata["desktop_method"], "key")

    def test_management_still_signs_in_from_anywhere(self):
        manager = make_user("ip-manager2@jone.test", password=PASSWORD, role=User.Role.MANAGER)
        self.assertEqual(self.login(manager, ip=OTHER_IP).status_code, 200)
        self.assertEqual(self.login(self.admin, ip=OTHER_IP).status_code, 200)

    # --- Session refresh ---------------------------------------------------

    def test_a_session_started_at_the_desk_refreshes_at_the_desk(self):
        login = self.login()
        refresh = login.json()["data"]["tokens"]["refresh"]
        again = self.client.post(REFRESH_URL, {"refresh": refresh}, format="json", REMOTE_ADDR=DESK_IP)
        self.assertEqual(again.status_code, 200, again.content)
        self.assertTrue(again.json()["data"]["tokens"]["access"])

    def test_a_session_cannot_be_refreshed_from_another_address(self):
        login = self.login()
        refresh = login.json()["data"]["tokens"]["refresh"]
        refused = self.client.post(REFRESH_URL, {"refresh": refresh}, format="json", REMOTE_ADDR=OTHER_IP)
        self.assertEqual(refused.status_code, 403, refused.content)
        self.assertEqual(refused.json()["code"], "STAFF_SIGN_IN_RESTRICTED")

    # --- Attendance at the same desk --------------------------------------

    def test_attendance_works_at_the_registered_desk_without_a_key(self):
        self.assertEqual(self.login().status_code, 200)
        self.auth(self.receptionist)
        clock = self.client.post(
            "/api/admin/staff-operations/attendance/clock/",
            {"action": "CLOCK_IN", "idempotency_key": "ip-route-clock-in"},
            format="json", REMOTE_ADDR=DESK_IP,
        )
        self.assertEqual(clock.status_code, 201, clock.content)

    def test_attendance_is_still_refused_away_from_the_desk(self):
        self.assertEqual(self.login().status_code, 200)
        self.auth(self.receptionist)
        clock = self.client.post(
            "/api/admin/staff-operations/attendance/clock/",
            {"action": "CLOCK_IN", "idempotency_key": "ip-route-clock-away"},
            format="json", REMOTE_ADDR=OTHER_IP,
        )
        self.assertEqual(clock.status_code, 403, clock.content)

    # --- The hotel-wide settings binding still applies on top -------------

    def test_the_settings_address_binds_the_desk_even_when_it_is_registered(self):
        hotel_settings(front_desk_desktop_ip="198.51.100.25")
        self.assertEqual(HotelSettings.get_settings().front_desk_desktop_ip, "198.51.100.25")
        blocked = self.login()
        self.assertEqual(blocked.status_code, 403, blocked.content)
        self.assertEqual(blocked.json()["errors"]["reason"], "front_desk_ip_mismatch")

    # --- Administration of the registration -------------------------------

    def test_an_admin_registers_and_clears_the_desktop_address(self):
        self.auth(self.admin)
        created = self.client.patch(
            f"{WORKSTATIONS_URL}{self.desk.pk}/", {"ip_address": "  2001:0DB8::0001  "}, format="json",
        )
        self.assertEqual(created.status_code, 200, created.content)
        data = created.json()["data"]
        self.assertEqual(data["ip_address"], "2001:db8::1")    # stored canonically
        self.assertTrue(data["receptionist_desktop_ready"])
        self.assertIsNotNone(data["desktop_ip_registered_at"])
        self.desk.refresh_from_db()
        self.assertEqual(self.desk.ip_address, "2001:db8::1")

        cleared = self.client.patch(
            f"{WORKSTATIONS_URL}{self.desk.pk}/", {"ip_address": ""}, format="json",
        )
        self.assertEqual(cleared.status_code, 200, cleared.content)
        self.assertEqual(cleared.json()["data"]["ip_address"], "")
        self.assertFalse(cleared.json()["data"]["receptionist_desktop_ready"])
        self.desk.refresh_from_db()
        self.assertIsNone(self.desk.desktop_ip_registered_at)

    def test_an_unusable_address_is_rejected_with_a_message(self):
        self.auth(self.admin)
        for bad in ("not-an-ip", "999.1.1.1", "102.89.34.7/999"):
            response = self.client.patch(
                f"{WORKSTATIONS_URL}{self.desk.pk}/", {"ip_address": bad}, format="json",
            )
            self.assertEqual(response.status_code, 400, f"{bad} was accepted")
            self.assertIn("ip_address", response.json()["errors"])

    def test_a_browser_can_read_the_address_the_server_sees(self):
        self.auth(self.receptionist)
        seen = self.client.get("/api/auth/client-address/", REMOTE_ADDR=DESK_IP)
        self.assertEqual(seen.status_code, 200, seen.content)
        self.assertEqual(seen.json()["data"]["client_ip"], DESK_IP)
        self.assertEqual(seen.json()["data"]["source"], "peer")
        behind_proxy = self.client.get(
            "/api/auth/client-address/", REMOTE_ADDR=PROXY_IP,
            HTTP_X_FORWARDED_FOR=f"{OTHER_IP}, {DESK_IP}",
        )
        self.assertEqual(behind_proxy.json()["data"]["client_ip"], DESK_IP)
        self.assertEqual(behind_proxy.json()["data"]["source"], "forwarded")
        self.assertTrue(behind_proxy.json()["data"]["trusted_proxy_hops"])

    def test_a_browser_can_tell_that_the_reported_address_is_the_platform(self):
        """Registration screens must not offer the hosting proxy as the desk.

        The front desk browser asks the API what address it arrives on. When no
        forwarding header survived, the honest answer is the platform's own
        proxy address — it changes per request and can never identify a desk.
        """
        self.auth(self.receptionist)
        platform = self.client.get(
            "/api/auth/client-address/", REMOTE_ADDR="100.64.0.18",
        )
        self.assertEqual(platform.status_code, 200, platform.content)
        data = platform.json()["data"]
        self.assertEqual(data["client_ip"], "100.64.0.18")
        self.assertTrue(data["client_ip_is_infrastructure"])
        self.assertFalse(data["trusted_proxy_hops"])

        real = self.client.get("/api/auth/client-address/", REMOTE_ADDR=DESK_IP)
        self.assertFalse(real.json()["data"]["client_ip_is_infrastructure"])


class ClientIpResolutionTests(BaseAPITestCase):
    """The resolver itself, independent of the desk policy."""

    class _Request:
        def __init__(self, remote, forwarded=None):
            self.META = {"REMOTE_ADDR": remote}
            if forwarded is not None:
                self.META["HTTP_X_FORWARDED_FOR"] = forwarded

    def test_no_forwarding_header_uses_the_peer(self):
        self.assertEqual(resolve_client_ip(self._Request(DESK_IP)), DESK_IP)

    def test_an_untrusted_peer_keeps_its_own_claim_out_of_the_answer(self):
        self.assertEqual(resolve_client_ip(self._Request(DESK_IP, OTHER_IP)), DESK_IP)

    def test_a_trusted_proxy_is_believed_about_the_last_hop(self):
        self.assertEqual(resolve_client_ip(self._Request(PROXY_IP, f"{OTHER_IP}, {DESK_IP}")), DESK_IP)

    def test_junk_entries_are_skipped_rather_than_accepted(self):
        self.assertEqual(
            resolve_client_ip(self._Request(PROXY_IP, f"unknown, {DESK_IP}, not-an-ip")), DESK_IP,
        )

    def test_a_blank_setting_means_unset_not_trust_nothing(self):
        # A copied .env often leaves a key blank. Blank must not silently turn
        # off the trust that makes a registered desk address usable.
        from apps.core.client_ip import trusted_client_ip_headers, trusted_proxy_networks

        with override_settings(TRUSTED_PROXY_CIDRS="", TRUSTED_CLIENT_IP_HEADERS=""):
            self.assertTrue(trusted_proxy_networks())
            self.assertTrue(trusted_client_ip_headers())
            self.assertEqual(resolve_client_ip(self._Request(PROXY_IP, f"{OTHER_IP}, {DESK_IP}")), DESK_IP)

    def test_a_deployment_can_name_its_own_edge_header(self):
        with override_settings(
            TRUSTED_PROXY_CIDRS="", TRUSTED_CLIENT_IP_HEADERS="x-real-ip",
        ):
            request = self._Request("10.0.0.14", f"{OTHER_IP}, {DESK_IP}")
            request.META["HTTP_X_REAL_IP"] = DESK_IP
            resolved = resolve_client_ip(request)
            source = client_ip_source(request)
        self.assertEqual(resolved, DESK_IP)
        self.assertEqual(source, "x-real-ip")

    def test_values_are_canonicalized(self):
        self.assertEqual(resolve_client_ip(self._Request("2001:0db8::0001")), "2001:db8::1")

    def test_an_empty_request_has_no_address(self):
        self.assertEqual(resolve_client_ip(None), "")


class RailwayProxyTests(BaseAPITestCase):
    """Railway reaches the container from 100.64.0.0/10 — a CGNAT address.

    That range was missing from the trusted proxies, so every proxied request
    looked like it came from the platform itself. The address shown to the front
    desk changed on every request (a different proxy instance each time), and no
    desk could ever be registered — the exact "the IP changes on every refresh"
    report.
    """

    RAILWAY_PEER = "100.64.0.18"
    DESK = "102.89.34.7"

    def setUp(self):
        super().setUp()
        cache.clear()
        self.receptionist = make_user(
            "railway-reception@jone.test", password=PASSWORD, role=User.Role.RECEPTIONIST
        )
        Workstation.objects.create(
            name="Lobby desk", department=Workstation.Department.FRONT_DESK,
            location="Lobby", ip_address=self.DESK,
        )

    def _request(self, **meta):
        return self.client.post(
            LOGIN_URL,
            {"email": self.receptionist.email, "password": PASSWORD},
            REMOTE_ADDR=self.RAILWAY_PEER,
            **meta,
        )

    def test_the_edge_address_is_trusted_so_the_desk_is_recognised(self):
        # Railway's edge rewrites X-Real-IP (and X-Forwarded-For) with the
        # client it saw.
        response = self._request(
            HTTP_X_REAL_IP=self.DESK, HTTP_X_FORWARDED_FOR=self.DESK,
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_the_resolved_address_is_the_visitor_not_the_platform(self):
        from django.test import RequestFactory

        from apps.core.client_ip import client_ip_source, resolve_client_ip

        request = RequestFactory().post(
            LOGIN_URL, REMOTE_ADDR=self.RAILWAY_PEER,
            HTTP_X_REAL_IP=self.DESK, HTTP_X_FORWARDED_FOR=f"203.0.113.9, {self.DESK}",
        )
        self.assertEqual(resolve_client_ip(request), self.DESK)
        self.assertEqual(client_ip_source(request), "x-real-ip")

    def test_an_appended_forwarding_chain_still_yields_the_client(self):
        from django.test import RequestFactory

        from apps.core.client_ip import resolve_client_ip

        request = RequestFactory().post(
            LOGIN_URL, REMOTE_ADDR=self.RAILWAY_PEER,
            HTTP_X_FORWARDED_FOR=f"{self.DESK}, {self.RAILWAY_PEER}",
        )
        self.assertEqual(resolve_client_ip(request), self.DESK)

    def test_a_refusal_names_the_real_address_not_the_proxy(self):
        response = self._request(
            HTTP_X_REAL_IP=OTHER_IP, HTTP_X_FORWARDED_FOR=OTHER_IP,
        )
        self.assertEqual(response.status_code, 403, response.content)
        reported = response.json()["errors"]["client_ip"]
        self.assertEqual(reported, OTHER_IP)
        self.assertNotEqual(reported, self.RAILWAY_PEER)

    def test_a_proxy_address_is_flagged_so_the_ui_does_not_suggest_registering_it(self):
        """With no forwarding header at all, the honest answer is "this is the platform"."""
        response = self._request()
        self.assertEqual(response.status_code, 403, response.content)
        errors = response.json()["errors"]
        self.assertEqual(errors["client_ip"], self.RAILWAY_PEER)
        self.assertTrue(errors["client_ip_is_infrastructure"])

    def test_a_public_peer_is_still_never_believed_about_a_forwarded_address(self):
        """Trusting the platform hop must not make every hop trusted."""
        from django.test import RequestFactory

        from apps.core.client_ip import resolve_client_ip

        request = RequestFactory().post(
            LOGIN_URL, REMOTE_ADDR=OTHER_IP,
            HTTP_X_FORWARDED_FOR=self.DESK, HTTP_X_REAL_IP=self.DESK,
        )
        self.assertEqual(resolve_client_ip(request), OTHER_IP)


class UnusableSavedBindingTests(BaseAPITestCase):
    """A saved binding that can never match must be named, not silently enforced.

    Before the proxy fix the API reported its own proxy address for every
    request, and an administrator could save that value in Settings. Once the
    address is resolved correctly the saved value can never equal a desk's
    address, so the front desk stays locked out. The refusal has to say that the
    SAVED value is the problem — otherwise the only advice it can give
    ("widen the entry") is wrong.
    """

    SAVED_PROXY = "100.64.0.18"
    DESK = "102.89.34.7"

    def setUp(self):
        super().setUp()
        cache.clear()
        self.admin = make_user("saved-admin@jone.test", password=PASSWORD, role=User.Role.ADMIN)
        self.receptionist = make_user(
            "saved-reception@jone.test", password=PASSWORD, role=User.Role.RECEPTIONIST
        )
        self.settings_obj = HotelSettings.get_settings()
        self.settings_obj.front_desk_desktop_ip = self.SAVED_PROXY
        self.settings_obj.save(update_fields=["front_desk_desktop_ip"])
        desk = Workstation.objects.create(
            name="Lobby desk", department=Workstation.Department.FRONT_DESK,
        )
        self.key = issue_desktop_key(desk)
        desk.save()

    def test_the_refusal_names_the_unusable_saved_value(self):
        response = self.client.post(
            LOGIN_URL,
            {"email": self.receptionist.email, "password": PASSWORD},
            REMOTE_ADDR=self.DESK, **{DESKTOP_HEADER: self.key},
        )
        self.assertEqual(response.status_code, 403, response.content)
        errors = response.json()["errors"]
        self.assertEqual(errors["reason"], "front_desk_ip_mismatch")
        self.assertTrue(errors["configured_address_unusable"])
        self.assertEqual(errors["configured_front_desk_address"], self.SAVED_PROXY)
        # The visitor itself is a real public address, so it is NOT flagged.
        self.assertNotIn("client_ip_is_infrastructure", errors)

    def test_a_private_saved_binding_is_usable_on_a_private_network(self):
        """A hotel LAN whose desk really is on 192.168.x must not be called broken."""
        self.settings_obj.front_desk_desktop_ip = "192.168.1.0/24"
        self.settings_obj.save(update_fields=["front_desk_desktop_ip"])
        response = self.client.post(
            LOGIN_URL,
            {"email": self.receptionist.email, "password": PASSWORD},
            REMOTE_ADDR="192.168.1.55", **{DESKTOP_HEADER: self.key},
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_the_settings_payload_flags_the_saved_value_too(self):
        self.auth(self.admin)
        response = self.client.get(SETTINGS_URL, REMOTE_ADDR=self.DESK)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertTrue(data["front_desk_desktop_ip_unusable"])
        self.assertFalse(data["request_ip_is_infrastructure"])

    def test_a_public_saved_binding_is_not_flagged(self):
        self.settings_obj.front_desk_desktop_ip = self.DESK
        self.settings_obj.save(update_fields=["front_desk_desktop_ip"])
        self.auth(self.admin)
        response = self.client.get(SETTINGS_URL, REMOTE_ADDR=self.DESK)
        self.assertFalse(response.json()["data"]["front_desk_desktop_ip_unusable"])

    def test_a_workstation_registered_with_the_proxy_address_is_named_too(self):
        """The sign-in refusal must cover the workstation route, not just settings.

        Most administrators register the desk from the Workstations page
        ("Register this address"), so a proxy address saved THERE is the more
        likely stale value.
        """
        self.settings_obj.front_desk_desktop_ip = ""
        self.settings_obj.save(update_fields=["front_desk_desktop_ip"])
        Workstation.objects.filter(department=Workstation.Department.FRONT_DESK).update(
            ip_address=self.SAVED_PROXY
        )
        # No desktop key: this is the desk that relies on its registered address
        # alone, which is the route the stale value breaks.
        response = self.client.post(
            LOGIN_URL,
            {"email": self.receptionist.email, "password": PASSWORD},
            REMOTE_ADDR=self.DESK,
        )
        self.assertEqual(response.status_code, 403, response.content)
        errors = response.json()["errors"]
        self.assertTrue(errors["configured_address_unusable"])
        self.assertEqual(errors["configured_front_desk_address"], self.SAVED_PROXY)
        # The refusal reason is the plain one: the address simply did not match.
        self.assertNotIn("reason", errors)

    def test_the_network_test_is_about_the_whole_range(self):
        from apps.core.client_ip import is_infrastructure_network

        self.assertTrue(is_infrastructure_network("100.64.0.18"))
        self.assertTrue(is_infrastructure_network("100.64.0.0/10"))
        self.assertTrue(is_infrastructure_network("192.168.1.0/24"))
        self.assertFalse(is_infrastructure_network("102.89.34.0/24"))
        self.assertFalse(is_infrastructure_network("0.0.0.0/0"), "a range that merely contains a private block is routable")
        self.assertFalse(is_infrastructure_network("not-an-address"))
