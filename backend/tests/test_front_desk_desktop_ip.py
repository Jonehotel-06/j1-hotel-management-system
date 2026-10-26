"""Front Desk email/password sign-in from the configured desktop.

The behaviour under test is the one the front desk actually uses: an employee at
the registered Front Desk desktop signs in with their OWN email and password and
nothing else — no desktop key typed, no attendance/access code, no fingerprint,
no prior clock-in.

Two administrator registrations can name that desktop, and this suite pins their
relationship (see ``apps.accounts.desktop_policy`` for the documented
precedence):

* ``Workstation.ip_address`` — the address registered on a single desk; and
* ``HotelSettings.front_desk_desktop_ip`` — the hotel-wide value the Settings
  page reports ``request_ip`` for and invites an administrator to save.

Saving the address in Settings used to be restriction-only, so the very desk it
described was refused and staff fell back to the numeric access code. These
tests pin that a Settings-registered desktop AUTHORIZES sign-in, while every
existing refusal (wrong password, wrong address, inactive account, inactive or
wrong-department workstation, forged forwarding headers) still holds.

Nothing here weakens the other departments: an operational role still needs the
desk, and management still keeps its any-device exception.
"""
<<<<<<< Updated upstream
from django.core.cache import cache
from django.test import override_settings
=======
from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient
>>>>>>> Stashed changes

from apps.accounts.desktop_policy import (
    DESKTOP_BY_SETTINGS_IP,
    DESKTOP_HEADER,
<<<<<<< Updated upstream
    issue_desktop_key,
=======
    hash_desktop_key,
    issue_desktop_key,
    resolve_receptionist_desktop,
>>>>>>> Stashed changes
)
from apps.accounts.models import User, Workstation
from apps.audit.models import AuditLog
from apps.hotel.models import HotelSettings
<<<<<<< Updated upstream
from apps.staff_operations.models import AttendanceRecord
=======
from apps.staff_operations.models import AttendanceRecord, StaffProfile
from apps.staff_operations.services import fingerprint_service
>>>>>>> Stashed changes

from .base import BaseAPITestCase
from .factories import hotel_settings, make_staff, make_user
from .test_attendance_workstation_policy import MATCHING_PROBE, MINUTIAE

PASSWORD = "Str0ng!Pass"
DESK_IP = "102.89.34.7"          # the configured Front Desk desktop address
OTHER_IP = "102.89.34.99"        # some other device on the internet
DESK_NETWORK = "102.89.34.0/24"  # the same desk, registered as a network
PROXY_PEER = "10.0.0.14"         # the deployment's own proxy (private range)

LOGIN_URL = "/api/auth/login/"
REFRESH_URL = "/api/auth/token/refresh/"
LOGOUT_URL = "/api/auth/logout/"
<<<<<<< Updated upstream
SETTINGS_URL = "/api/admin/settings/"
CLOCK_URL = "/api/admin/staff-operations/attendance/clock/"
=======
PROFILE_URL = "/api/auth/profile/"
CLIENT_ADDRESS_URL = "/api/auth/client-address/"
SETTINGS_URL = "/api/admin/settings/"
CLOCK_URL = "/api/admin/staff-operations/attendance/clock/"
TERMINAL_URL = "/api/admin/staff-operations/biometrics/reception-terminal/"
>>>>>>> Stashed changes
WORKSTATIONS_URL = "/api/admin/users/terminals/"


class FrontDeskLoginTestCase(BaseAPITestCase):
    """Shared fixture: a configured desktop plus its front desk employees."""

    def setUp(self):
        super().setUp()
<<<<<<< Updated upstream
        cache.clear()
        self.admin = make_user("fd-admin@jone.test", password=PASSWORD, role=User.Role.ADMIN)
        self.receptionist = make_user("fd-one@jone.test", password=PASSWORD, role=User.Role.RECEPTIONIST)
        self.colleague = make_user("fd-two@jone.test", password=PASSWORD, role=User.Role.RECEPTIONIST)
        # The desktop as an administrator configures it in Settings, and the
        # desk record that makes the hotel's front desk a real, deactivatable
        # registration. Its own ip_address is deliberately left blank: this is
        # the deployment in which the address was only ever saved in Settings.
=======
        cache.clear()
        self.admin = make_user("fd-admin@jone.test", password=PASSWORD, role=User.Role.ADMIN)
        self.receptionist = make_user("fd-one@jone.test", password=PASSWORD, role=User.Role.RECEPTIONIST)
        self.colleague = make_user("fd-two@jone.test", password=PASSWORD, role=User.Role.RECEPTIONIST)
        # The desktop as an administrator configures it in Settings, and the
        # desk record that makes the hotel's front desk a real, deactivatable
        # registration. Its own ip_address is deliberately left blank: this is
        # the deployment in which the address was only ever saved in Settings.
        self.desk = Workstation.objects.create(
            name="Front desk desktop", department=Workstation.Department.FRONT_DESK, location="Lobby",
        )

    # -- helpers ---------------------------------------------------------------
    def login(self, user, *, ip=DESK_IP, password=PASSWORD, key=None, extra_headers=None, client=None):
        headers = dict(extra_headers or {})
        if key is not None:
            headers[DESKTOP_HEADER] = key
        return (client or self.client).post(
            LOGIN_URL, {"email": user.email, "password": password}, REMOTE_ADDR=ip, **headers,
        )

    def bind_desk_ip(self, value):
        """Save the desktop address the way an administrator does, from Settings."""
        admin = APIClient()
        tokens = APIClient().post(
            LOGIN_URL, {"email": self.admin.email, "password": PASSWORD}, REMOTE_ADDR=DESK_IP,
        )
        self.assertEqual(tokens.status_code, 200, tokens.content)
        admin.credentials(HTTP_AUTHORIZATION="Bearer " + tokens.data["data"]["tokens"]["access"])
        response = admin.patch(SETTINGS_URL, {"front_desk_desktop_ip": value}, format="json")
        cache.clear()
        return response


class SettingsRegisteredDesktopTests(FrontDeskLoginTestCase):
    """Problem A: the address the Settings page asks for must AUTHORIZE the desk."""

    def test_a_receptionist_signs_in_from_the_address_saved_in_settings(self):
        saved = self.bind_desk_ip(DESK_IP)
        self.assertEqual(saved.status_code, 200, saved.content)

        response = self.login(self.receptionist, ip=DESK_IP)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.data["data"]
        self.assertTrue(body["tokens"]["access"])
        self.assertTrue(body["tokens"]["refresh"])
        self.assertEqual(body["user"]["email"], self.receptionist.email)
        # The refusal/acceptance is audited with the route that proved the desk.
        entry = AuditLog.objects.filter(action="STAFF_SIGN_IN", actor=self.receptionist).latest("id")
        self.assertEqual(entry.metadata["desktop_method"], DESKTOP_BY_SETTINGS_IP)

    def test_a_network_registration_covers_a_reassigned_desk_address(self):
        """Problem B: a dynamic public address is handled by registering the network."""
        self.assertEqual(self.bind_desk_ip(DESK_NETWORK).status_code, 200)
        self.assertEqual(self.login(self.receptionist, ip="102.89.34.55").status_code, 200)
        # The same person's next shift may arrive on a different address inside
        # the network, and still needs nothing but their own password.
        self.assertEqual(self.login(self.receptionist, ip="102.89.34.201").status_code, 200)

    def test_an_address_outside_the_saved_binding_is_still_refused(self):
        self.assertEqual(self.bind_desk_ip(DESK_NETWORK).status_code, 200)
        refused = self.login(self.receptionist, ip="102.89.35.55")
        self.assertEqual(refused.status_code, 403, refused.content)
        self.assertEqual(refused.data["code"], "STAFF_SIGN_IN_RESTRICTED")
        self.assertNotIn("access", str(refused.data))

    def test_the_saved_binding_requires_a_live_front_desk_registration(self):
        """Retiring the desk retires the address an administrator once saved."""
        self.assertEqual(self.bind_desk_ip(DESK_IP).status_code, 200)
        self.assertEqual(self.login(self.receptionist, ip=DESK_IP).status_code, 200)
        Workstation.objects.filter(pk=self.desk.pk).update(is_active=False)
        refused = self.login(self.receptionist, ip=DESK_IP)
        self.assertEqual(refused.status_code, 403, refused.content)
        self.assertEqual(refused.data["code"], "STAFF_SIGN_IN_RESTRICTED")

    def test_an_empty_binding_never_authorizes_anything(self):
        self.assertEqual(HotelSettings.get_settings().front_desk_desktop_ip, "")
        refused = self.login(self.receptionist, ip=OTHER_IP)
        self.assertEqual(refused.status_code, 403, refused.content)

    def test_the_desk_routes_are_resolved_in_the_documented_order(self):
        desk_key = issue_desktop_key(self.desk)
        self.desk.save()
        self.assertEqual(self.bind_desk_ip(DESK_IP).status_code, 200)

        request = type("R", (), {})()
        from django.test import RequestFactory

        factory = RequestFactory()
        # 1. a presented key names the desk first;
        keyed = factory.post("/api/auth/login/", REMOTE_ADDR=DESK_IP, **{DESKTOP_HEADER: desk_key})
        self.assertEqual(resolve_receptionist_desktop(keyed)[1], "key")
        # 2. a workstation's own registered address next;
        Workstation.objects.filter(pk=self.desk.pk).update(ip_address="102.89.40.9")
        by_ip = factory.post("/api/auth/login/", REMOTE_ADDR="102.89.40.9")
        self.assertEqual(resolve_receptionist_desktop(by_ip)[1], "registered_ip")
        # 3. a Settings-registered address last;
        Workstation.objects.filter(pk=self.desk.pk).update(ip_address="")
        by_settings = factory.post("/api/auth/login/", REMOTE_ADDR=DESK_IP)
        self.assertEqual(resolve_receptionist_desktop(by_settings)[1], DESKTOP_BY_SETTINGS_IP)
        # 4. anything else is outside the desk.
        outside = factory.post("/api/auth/login/", REMOTE_ADDR=OTHER_IP)
        self.assertEqual(resolve_receptionist_desktop(outside)[1], "")


class SignInRefusalTests(FrontDeskLoginTestCase):
    """Every existing refusal keeps its own, distinguishable answer."""

    def test_invalid_credentials_are_401_not_a_workstation_refusal(self):
        self.assertEqual(self.bind_desk_ip(DESK_IP).status_code, 200)
        wrong = self.login(self.receptionist, ip=DESK_IP, password="Wrong!Pass1")
        self.assertEqual(wrong.status_code, 401, wrong.content)
        self.assertEqual(wrong.data["code"], "UNAUTHORIZED")
        self.assertIn("Invalid email or password", wrong.data["message"])
        # Nothing was written about the desk rule, and no session was created.
        self.assertFalse(AuditLog.objects.filter(action="STAFF_SIGN_IN_RESTRICTED").exists())

    def test_an_unknown_email_is_401_like_any_other_bad_credential(self):
        unknown = self.client.post(
            LOGIN_URL, {"email": "nobody@jone.test", "password": PASSWORD}, REMOTE_ADDR=DESK_IP,
        )
        self.assertEqual(unknown.status_code, 401, unknown.content)

    def test_an_unregistered_workstation_reports_the_address_it_saw(self):
        refused = self.login(self.receptionist, ip=OTHER_IP)
        self.assertEqual(refused.status_code, 403, refused.content)
        self.assertEqual(refused.data["code"], "STAFF_SIGN_IN_RESTRICTED")
        # The address travels back so an administrator can register it, and it
        # is the caller's own address, revealed only after the password passed.
        self.assertEqual(refused.data["errors"]["client_ip"], OTHER_IP)
        self.assertNotIn("Traceback", str(refused.data))
        entry = AuditLog.objects.filter(action="STAFF_SIGN_IN_RESTRICTED", actor=self.receptionist).latest("id")
        self.assertEqual(entry.metadata["reason"], "no_receptionist_desktop")
        self.assertEqual(entry.metadata["client_ip"], OTHER_IP)

    def test_an_inactive_account_is_refused_without_tokens(self):
        self.assertEqual(self.bind_desk_ip(DESK_IP).status_code, 200)
        self.receptionist.is_active = False
        self.receptionist.save(update_fields=["is_active"])
        refused = self.login(self.receptionist, ip=DESK_IP)
        self.assertEqual(refused.status_code, 401, refused.content)
        self.assertNotIn("access", str(refused.data))

    def test_management_keeps_any_device_access(self):
        refused = self.login(self.receptionist, ip=OTHER_IP)
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(self.login(self.admin, ip=OTHER_IP).status_code, 200)


class ClientAddressResolutionTests(FrontDeskLoginTestCase):
    """Problem B: which address counts, and which headers are only claims."""

    def test_a_forwarding_header_from_an_untrusted_peer_is_ignored(self):
        """Test H: a client cannot nominate its own address to become the desk."""
        self.assertEqual(self.bind_desk_ip(DESK_IP).status_code, 200)
        spoofed = self.login(
            self.receptionist, ip=OTHER_IP,
            extra_headers={"HTTP_X_FORWARDED_FOR": DESK_IP, "HTTP_X_REAL_IP": DESK_IP,
                           "HTTP_CF_CONNECTING_IP": DESK_IP, "HTTP_TRUE_CLIENT_IP": DESK_IP},
        )
        self.assertEqual(spoofed.status_code, 403, spoofed.content)
        self.assertEqual(spoofed.data["errors"]["client_ip"], OTHER_IP)

    def test_the_edge_header_is_used_when_the_connection_came_from_a_trusted_proxy(self):
        """Test D: behind the deployment's proxy, the desk's real address is used."""
        self.assertEqual(self.bind_desk_ip(DESK_IP).status_code, 200)
        behind_proxy = self.login(
            self.receptionist, ip=PROXY_PEER, extra_headers={"HTTP_CF_CONNECTING_IP": DESK_IP},
        )
        self.assertEqual(behind_proxy.status_code, 200, behind_proxy.content)

    @override_settings(TRUSTED_CLIENT_IP_HEADERS=[])
    def test_the_forwarding_chain_is_walked_from_the_right(self):
        """A forged prefix cannot displace the hop the trusted proxy observed."""
        self.assertEqual(self.bind_desk_ip(DESK_IP).status_code, 200)
        forged = self.login(
            self.receptionist, ip=PROXY_PEER,
            extra_headers={"HTTP_X_FORWARDED_FOR": DESK_IP + ", " + OTHER_IP},
        )
        self.assertEqual(forged.status_code, 403, forged.content)
        self.assertEqual(forged.data["errors"]["client_ip"], OTHER_IP)

    def test_a_hosting_proxy_address_is_reported_rather_than_registered(self):
        """The address shown must be usable: a platform address is flagged as not."""
        self.auth(self.admin)
        response = self.client.get(SETTINGS_URL, REMOTE_ADDR="100.64.0.18")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.data["data"]
        self.assertEqual(data["request_ip"], "100.64.0.18")
        self.assertTrue(data["request_ip_is_infrastructure"])

    def test_the_client_address_endpoint_reports_its_source_without_trusting_claims(self):
        self.auth(self.receptionist)
        own = self.client.get(CLIENT_ADDRESS_URL, REMOTE_ADDR=OTHER_IP,
                              HTTP_X_FORWARDED_FOR=DESK_IP)
        self.assertEqual(own.status_code, 200, own.content)
        self.assertEqual(own.data["data"]["client_ip"], OTHER_IP)
        self.assertEqual(own.data["data"]["source"], "peer")

        behind_proxy = self.client.get(CLIENT_ADDRESS_URL, REMOTE_ADDR=PROXY_PEER,
                                       HTTP_CF_CONNECTING_IP=OTHER_IP)
        self.assertEqual(behind_proxy.data["data"]["client_ip"], OTHER_IP)
        self.assertEqual(behind_proxy.data["data"]["source"], "cf-connecting-ip")
        self.assertTrue(behind_proxy.data["data"]["trusted_proxy_hops"])

    def test_detecting_a_new_address_never_rewrites_the_registration(self):
        """Problem B: a visit from elsewhere is a diagnostic, never a silent save."""
        self.assertEqual(self.bind_desk_ip(DESK_IP).status_code, 200)
        Workstation.objects.filter(pk=self.desk.pk).update(ip_address="102.89.41.5")
        before_settings = HotelSettings.get_settings().front_desk_desktop_ip
        before_workstation = Workstation.objects.get(pk=self.desk.pk).ip_address

        refused = self.login(self.receptionist, ip="102.89.99.99")
        self.assertEqual(refused.status_code, 403, refused.content)
        self.assertEqual(refused.data["errors"]["client_ip"], "102.89.99.99")

        self.assertEqual(HotelSettings.get_settings().front_desk_desktop_ip, before_settings)
        self.assertEqual(Workstation.objects.get(pk=self.desk.pk).ip_address, before_workstation)
        self.assertEqual(Workstation.objects.count(), 1, "no workstation may be created by a sign-in")
        self.assertEqual(User.objects.count(), 3, "no account may be created by a sign-in")


class LoginSequenceTests(FrontDeskLoginTestCase):
    """Problem C: one person's failed sign-in must not break the next sign-in."""

    def test_a_failed_receptionist_attempt_does_not_block_the_administrator(self):
        """Test E."""
        failed = self.login(self.receptionist, ip=OTHER_IP)
        self.assertEqual(failed.status_code, 403, failed.content)

        admin = self.login(self.admin, ip=OTHER_IP)
        self.assertEqual(admin.status_code, 200, admin.content)
        self.assertTrue(admin.data["data"]["tokens"]["access"])

    def test_repeated_failed_attempts_do_not_block_a_later_valid_attempt(self):
        """Test F / Test J at the API level."""
        for _ in range(3):
            self.assertEqual(self.login(self.receptionist, ip=OTHER_IP).status_code, 403)
        self.assertEqual(self.bind_desk_ip(DESK_IP).status_code, 200)
        self.assertEqual(self.login(self.receptionist, ip=DESK_IP).status_code, 200)

    def test_a_failed_staff_attempt_does_not_touch_an_administrator_session(self):
        """Test K."""
        admin_client = APIClient()
        session = self.login(self.admin, ip=OTHER_IP, client=admin_client)
        self.assertEqual(session.status_code, 200, session.content)
        tokens = session.data["data"]["tokens"]
        admin_client.credentials(HTTP_AUTHORIZATION="Bearer " + tokens["access"])

        self.assertEqual(self.login(self.receptionist, ip=OTHER_IP).status_code, 403)

        self.assertEqual(admin_client.get(PROFILE_URL, REMOTE_ADDR=OTHER_IP).status_code, 200)
        refreshed = admin_client.post(REFRESH_URL, {"refresh": tokens["refresh"]}, format="json",
                                      REMOTE_ADDR=OTHER_IP)
        self.assertEqual(refreshed.status_code, 200, refreshed.content)

    def test_an_administrator_can_sign_in_from_a_separate_browser_session(self):
        """Test G."""
        first = APIClient()
        second = APIClient()
        self.assertEqual(self.login(self.receptionist, ip=OTHER_IP, client=first).status_code, 403)
        self.assertEqual(self.login(self.admin, ip=OTHER_IP, client=second).status_code, 200)
        self.assertEqual(self.login(self.admin, ip=PROXY_PEER, client=first).status_code, 200)

    def test_a_server_error_on_one_attempt_does_not_poison_the_next(self):
        """Test I (500): the envelope is safe, and the next request still works."""
        with mock.patch(
            "apps.accounts.serializers.log_staff_sign_in", side_effect=RuntimeError("boom"),
        ):
            broken = self.login(self.admin, ip=OTHER_IP)
        self.assertEqual(broken.status_code, 500, broken.content)
        self.assertEqual(broken.data["code"], "SERVER_ERROR")
        self.assertNotIn("boom", str(broken.data))
        self.assertNotIn("Traceback", str(broken.data))
        self.assertEqual(self.login(self.admin, ip=OTHER_IP).status_code, 200)

    def test_the_login_throttle_still_refuses_a_burst(self):
        """Test I (429): legitimate rate limiting is preserved, not disabled."""
        for _ in range(5):
            self.login(self.receptionist, ip=OTHER_IP, password="Wrong!Pass1")
        limited = self.login(self.admin, ip=OTHER_IP)
        self.assertEqual(limited.status_code, 429, limited.content)
        self.assertEqual(limited.data["code"], "RATE_LIMITED")
        self.assertIn("Retry-After", limited.headers)
        # A different workstation address is a different throttle bucket.
        cache.clear()
        self.assertEqual(self.login(self.admin, ip=OTHER_IP).status_code, 200)


class AttendanceAndSessionContinuityTests(FrontDeskLoginTestCase):
    """The rest of the front desk's day keeps working from the same registration."""

    def test_the_session_refreshes_from_the_settings_registered_desk(self):
        self.assertEqual(self.bind_desk_ip(DESK_IP).status_code, 200)
        login = self.login(self.receptionist, ip=DESK_IP)
        self.assertEqual(login.status_code, 200, login.content)
        refresh = login.data["data"]["tokens"]["refresh"]

        # A refused refresh is checked BEFORE simplejwt rotates the token, so the
        # same session still refreshes afterwards from the desk.
        refused = self.client.post(REFRESH_URL, {"refresh": refresh}, format="json",
                                   REMOTE_ADDR=OTHER_IP)
        self.assertEqual(refused.status_code, 403, refused.content)
        self.assertEqual(refused.data["code"], "STAFF_SIGN_IN_RESTRICTED")
        self.assertEqual(
            self.client.post(REFRESH_URL, {"refresh": refresh}, format="json",
                             REMOTE_ADDR=DESK_IP).status_code, 200,
        )

    def test_attendance_clocks_in_from_the_settings_registered_desk(self):
        self.assertEqual(self.bind_desk_ip(DESK_IP).status_code, 200)
        self.auth(self.receptionist)
        allowed = self.client.post(
            CLOCK_URL, {"action": "CLOCK_IN", "idempotency_key": "desk-ip-clock"},
            format="json", REMOTE_ADDR=DESK_IP,
        )
        self.assertEqual(allowed.status_code, 201, allowed.content)
        self.assertTrue(AttendanceRecord.objects.filter(staff=self.receptionist).exists())

    def test_the_six_digit_access_code_sign_in_is_unchanged(self):
        """Test L: the numeric code path is untouched by the desktop-IP rule."""
        worker = make_staff("fd-code@staff.dev", role=User.Role.CHEF)
        StaffProfile.objects.update_or_create(
            user=worker,
            defaults={"employee_code": "STF-FDCODE", "department": "Kitchen",
                      "employment_status": StaffProfile.EmploymentStatus.ACTIVE},
        )
        manager = make_user("fd-code-manager@jone.test", password=PASSWORD, role=User.Role.MANAGER)
        template = fingerprint_service.request_fingerprint_enrollment(
            staff=worker, actor=worker, minutiae=MINUTIAE,
        )
        fingerprint_service.approve_fingerprint_enrollment(template=template, approver=manager)

        desk_key = issue_desktop_key(self.desk)
        self.desk.save(update_fields=["sign_in_key_hash", "sign_in_key_issued_at"])
        self.unauth()
        clocked = self.client.post(
            TERMINAL_URL,
            {"action": "CLOCK_IN", "email": worker.email, "fingerprint_probe": MATCHING_PROBE},
            format="json", REMOTE_ADDR=DESK_IP, **{DESKTOP_HEADER: desk_key},
        )
        self.assertEqual(clocked.status_code, 200, clocked.content)
        code = clocked.data["data"]["access_code"]

        cache.clear()
        signed_in = APIClient().post(LOGIN_URL, {"access_code": code}, format="json")
        self.assertEqual(signed_in.status_code, 200, signed_in.content)
        self.assertTrue(signed_in.data["data"]["tokens"]["access"])
        self.assertEqual(signed_in.data["data"]["user"]["email"], worker.email)


class DesktopConfigurationApiTests(FrontDeskLoginTestCase):
    """The administrator-managed registration stays administrator-managed."""

    def test_only_an_administrator_may_change_the_desktop_address(self):
        self.auth(None)
        self.assertEqual(self.client.get(SETTINGS_URL).status_code, 401)
        self.auth(self.receptionist)
        self.assertEqual(self.client.get(SETTINGS_URL).status_code, 403)
        self.assertEqual(
            self.client.patch(SETTINGS_URL, {"front_desk_desktop_ip": DESK_IP}, format="json").status_code,
            403,
        )
        self.assertEqual(HotelSettings.get_settings().front_desk_desktop_ip, "")

    def test_a_hostname_is_never_accepted_as_a_desktop_address(self):
        self.auth(self.admin)
        response = self.client.patch(SETTINGS_URL, {"front_desk_desktop_ip": "frontdesk.local"}, format="json")
        self.assertEqual(response.status_code, 400, response.content)

    def test_the_desk_address_can_be_changed_without_a_code_change(self):
        self.assertEqual(self.bind_desk_ip(DESK_IP).status_code, 200)
        self.assertEqual(HotelSettings.get_settings().front_desk_desktop_ip, DESK_IP)
        self.assertEqual(self.bind_desk_ip("198.51.100.25").status_code, 200)
        self.assertEqual(HotelSettings.get_settings().front_desk_desktop_ip, "198.51.100.25")
        self.assertEqual(self.login(self.receptionist, ip=DESK_IP).status_code, 403)
        self.assertEqual(self.login(self.receptionist, ip="198.51.100.25").status_code, 200)

    def test_a_workstation_address_is_normalised_and_validated(self):
        self.auth(self.admin)
        saved = self.client.patch(
            f"/api/admin/users/terminals/{self.desk.pk}/", {"ip_address": "102.89.34.7/24"}, format="json",
        )
        self.assertEqual(saved.status_code, 200, saved.content)
        self.assertEqual(saved.data["data"]["ip_address"], "102.89.34.0/24")
        bad = self.client.patch(
            f"/api/admin/users/terminals/{self.desk.pk}/", {"ip_address": "not-an-address"}, format="json",
        )
        self.assertEqual(bad.status_code, 400, bad.content)

    def test_a_desk_key_is_still_hashed_and_never_echoed(self):
        self.auth(self.admin)
        response = self.client.post(
            f"/api/admin/users/terminals/{self.desk.pk}/desktop-key/", {}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        raw = response.data["data"]["receptionist_desktop_key"]
        self.assertTrue(raw, "the raw key is returned exactly once")
        self.desk.refresh_from_db()
        self.assertEqual(self.desk.sign_in_key_hash, hash_desktop_key(raw))
        self.assertNotIn(raw, self.desk.sign_in_key_hash)
        # A rotation replaces the previous key: the old one stops working.
        second = self.client.post(
            f"/api/admin/users/terminals/{self.desk.pk}/desktop-key/", {}, format="json",
        )
        self.assertEqual(second.status_code, 200, second.content)
        stale = self.login(self.receptionist, ip=OTHER_IP, key=raw)
        self.assertEqual(stale.status_code, 403, stale.content)
        current = self.login(self.receptionist, ip=OTHER_IP, key=second.data["data"]["receptionist_desktop_key"])
        self.assertEqual(current.status_code, 200, current.content)
>>>>>>> Stashed changes
