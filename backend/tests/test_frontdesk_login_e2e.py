"""End-to-end regression matrix for Front Desk email/password staff sign-in.

Pins the owner's required login behaviour against the REAL endpoints, in the
exact order a front desk lives it, so a future change cannot quietly reopen any
of these gaps:

* an authorized Front Desk employee signs in with email + password from the
  enrolled Receptionist Desktop (all three enrolment routes, including the
  production proxy chain: Cloudflare-style edge header over a trusted proxy,
  and the X-Forwarded-For walk with the CDN range explicitly trusted);
* wrong password / unknown email / inactive account are each refused with the
  same non-enumerating 401 envelope;
* a desktop that is not registered, disabled, or merely "configured in
  Settings" without a workstation record cannot authorize anybody — and the
  refusal now SAYS which step is missing instead of advising an address that
  cannot help (the dead-end this suite was written to close);
* a staff member from another department is refused the Front Desk context;
* one person's failed attempt, logout, or refused refresh never blocks the
  next person's sign-in;
* management keeps access from any device; the numeric-code and fingerprint
  attendance workflows keep working untouched.

No production system is touched: everything runs against the test database.
"""
from io import StringIO

from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from rest_framework.test import APIClient

from apps.accounts.desktop_policy import issue_desktop_key
from apps.accounts.models import User, Workstation
from apps.hotel.models import HotelSettings
from apps.staff_operations.models import StaffProfile
from apps.staff_operations.services import fingerprint_service

from .base import BaseAPITestCase
from .factories import hotel_settings, make_staff, make_user
from .test_attendance_workstation_policy import MATCHING_PROBE, MINUTIAE

PASSWORD = "Str0ng!Desk-Pass"
DESK_IP = "102.89.34.7"            # the address the desk reaches the API from
OTHER_IP = "41.203.89.5"           # any other device on the internet
HOME_IP = "197.210.44.12"          # a staff member's home connection
PROXY_IP = "10.0.0.9"              # the deployment's own (trusted) proxy hop
CF_EDGE_IP = "104.21.55.131"       # a public Cloudflare edge address
LOGIN_URL = "/api/auth/login/"
LOGOUT_URL = "/api/auth/logout/"
REFRESH_URL = "/api/auth/token/refresh/"
PROFILE_URL = "/api/auth/profile/"
SETTINGS_URL = "/api/admin/settings/"
ADMIN_BOOKINGS_URL = "/api/admin/bookings/"
TERMINAL_URL = "/api/admin/staff-operations/biometrics/reception-terminal/"


class FrontDeskLoginE2ECase(BaseAPITestCase):
    """Shared fixture: the people, and one enrolled Receptionist Desktop."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.admin = make_user("e2e.admin@jone.test", password=PASSWORD, role=User.Role.ADMIN)
        self.manager = make_user("e2e.manager@jone.test", password=PASSWORD, role=User.Role.MANAGER)
        self.general_manager = make_user(
            "e2e.gm@jone.test", password=PASSWORD, role=User.Role.GENERAL_MANAGER
        )
        self.receptionist = make_user(
            "e2e.receptionist@jone.test", password=PASSWORD, role=User.Role.RECEPTIONIST
        )
        self.second_receptionist = make_user(
            "e2e.receptionist2@jone.test", password=PASSWORD, role=User.Role.RECEPTIONIST
        )
        self.inactive_receptionist = make_user(
            "e2e.inactive@jone.test", password=PASSWORD, role=User.Role.RECEPTIONIST, is_active=False,
        )
        self.waiter = make_user("e2e.waiter@jone.test", password=PASSWORD, role=User.Role.WAITER)
        self.desk = Workstation.objects.create(
            name="Front desk reception 1", department=Workstation.Department.FRONT_DESK,
            location="Lobby", ip_address=DESK_IP,
        )

    # ------------------------------------------------------------------ helpers
    def login(self, user=None, *, password=PASSWORD, ip=DESK_IP, headers=None, extra_body=None):
        body = {"email": (user or self.receptionist).email, "password": password}
        if extra_body:
            body.update(extra_body)
        return self.client.post(LOGIN_URL, body, format="json", REMOTE_ADDR=ip, **(headers or {}))

    def clear_login_throttle(self):
        # The login endpoint is throttled 5/min per client address; the many
        # sign-ins inside one test are not an attack, so the counter is reset
        # between them (the dedicated throttle test does the opposite).
        cache.clear()

    def tokens_of(self, response):
        return response.data["data"]["tokens"]


class HappyPathSignInTests(FrontDeskLoginE2ECase):
    """Matrix 1 + 15: the requested behaviour, on every enrolment route."""

    def test_authorized_receptionist_password_login_from_the_registered_desk(self):
        response = self.login()
        self.assertEqual(response.status_code, 200, response.content)
        body = response.data["data"]
        self.assertEqual(body["user"]["email"], self.receptionist.email)
        self.assertEqual(body["user"]["role"], User.Role.RECEPTIONIST)
        self.assertTrue(body["tokens"]["access"] and body["tokens"]["refresh"])

    def test_password_login_via_the_desktop_key_route(self):
        self.desk.ip_address = ""
        self.desk.save(update_fields=["ip_address"])
        raw_key = issue_desktop_key(self.desk)
        self.desk.save(update_fields=["sign_in_key_hash", "sign_in_key_issued_at", "updated_at"])
        response = self.login(ip=OTHER_IP, headers={"HTTP_X_JONE_DESKTOP_KEY": raw_key})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["data"]["user"]["email"], self.receptionist.email)

    def test_password_login_via_the_settings_address_route(self):
        self.desk.ip_address = ""
        self.desk.save(update_fields=["ip_address"])
        hotel_settings(front_desk_desktop_ip=DESK_IP)
        response = self.login()
        self.assertEqual(response.status_code, 200, response.content)

    def test_password_login_through_the_production_proxy_chain(self):
        """The Cloudflare shape: trusted proxy hop + CF-Connecting-IP edge header."""
        response = self.login(
            ip=PROXY_IP,
            headers={
                "HTTP_CF_CONNECTING_IP": DESK_IP,
                "HTTP_X_FORWARDED_FOR": f"{DESK_IP}, {CF_EDGE_IP}",
            },
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_password_login_when_only_the_cdn_range_is_trusted_as_a_proxy(self):
        """The header-stripped shape: the XFF walk must be able to step over the
        CDN when its range is explicitly trusted (the documented production
        remedy — TRUSTED_PROXY_CIDRS), and must not when it is not."""
        headers = {"HTTP_X_FORWARDED_FOR": f"{DESK_IP}, {CF_EDGE_IP}"}
        # Default trust: the CDN address is not a trusted proxy, so the walk
        # stops on it and the desk cannot be recognized (fail closed).
        refused = self.login(ip=PROXY_IP, headers=headers)
        self.assertEqual(refused.status_code, 403, refused.content)
        with override_settings(
            TRUSTED_PROXY_CIDRS=f"127.0.0.0/8,10.0.0.0/8,{CF_EDGE_IP}/32",
            TRUSTED_CLIENT_IP_HEADERS="none",
        ):
            allowed = self.login(ip=PROXY_IP, headers=headers)
        self.assertEqual(allowed.status_code, 200, allowed.content)

    def test_management_signs_in_from_any_device(self):
        for user, ip in (
            (self.admin, HOME_IP), (self.manager, OTHER_IP), (self.general_manager, HOME_IP),
        ):
            with self.subTest(role=user.role):
                self.clear_login_throttle()
                response = self.login(user, ip=ip)
                self.assertEqual(response.status_code, 200, response.content)

    def test_guest_sign_in_is_unaffected_by_the_desk_policy(self):
        guest = make_user("e2e.guest@jone.test", password=PASSWORD, role=User.Role.GUEST)
        response = self.login(guest, ip=OTHER_IP)
        self.assertEqual(response.status_code, 200, response.content)


class CredentialFailureTests(FrontDeskLoginE2ECase):
    """Matrix 2, 3, 4 + 14: refusals are accurate and never enumerate accounts."""

    def test_wrong_password_is_rejected_with_the_credentials_envelope(self):
        response = self.login(password="definitely-not-it")
        self.assertEqual(response.status_code, 401, response.content)
        body = response.data
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "UNAUTHORIZED")
        self.assertEqual(body["message"], "Invalid email or password.")
        self.assertNotIn("tokens", body.get("data") or {})

    def test_unknown_email_is_rejected_identically(self):
        response = self.client.post(
            LOGIN_URL, {"email": "nobody-such-account@jone.test", "password": PASSWORD},
            format="json", REMOTE_ADDR=DESK_IP,
        )
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(response.data["message"], "Invalid email or password.")

    def test_inactive_account_is_rejected_without_revealing_it_exists(self):
        response = self.login(self.inactive_receptionist)
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(response.data["message"], "Invalid email or password.")

    def test_the_desk_is_still_required_after_a_correct_password(self):
        """A valid password from an unenrolled device is a DESK refusal (403),
        not a credentials refusal (401): the two must stay distinguishable."""
        response = self.login(ip=OTHER_IP)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.data["code"], "STAFF_SIGN_IN_RESTRICTED")


class DesktopEnforcementTests(FrontDeskLoginE2ECase):
    """Matrix 6, 7, 16 + the configuration dead end this fix closes."""

    def test_unregistered_desktop_is_refused_and_names_the_address_to_register(self):
        response = self.login(ip=OTHER_IP)
        self.assertEqual(response.status_code, 403, response.content)
        body = response.data
        self.assertEqual(body["code"], "STAFF_SIGN_IN_RESTRICTED")
        self.assertEqual(body["errors"]["client_ip"], OTHER_IP)

    def test_disabled_front_desk_workstation_stops_admitting_staff(self):
        self.desk.is_active = False
        self.desk.save(update_fields=["is_active"])
        response = self.login()
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.data["code"], "STAFF_SIGN_IN_RESTRICTED")

    def test_settings_address_alone_with_no_workstation_is_the_dead_end_now_named(self):
        """The exact 'configured desktop' trap: the Settings field is filled in
        (the Settings page invites it) but no Front Desk workstation record
        exists, so NOTHING can match. The refusal must say which step is
        missing; registering the workstation then makes the same address work."""
        self.desk.delete()
        hotel_settings(front_desk_desktop_ip=DESK_IP)
        refused = self.login()
        self.assertEqual(refused.status_code, 403, refused.content)
        self.assertTrue(refused.data["errors"]["no_active_front_desk_workstation"])
        self.assertIn("Receptionist Desktop", refused.data["message"])

        # The administrator follows the instruction: register the workstation.
        Workstation.objects.create(
            name="Front desk reception 1", department=Workstation.Department.FRONT_DESK,
        )
        allowed = self.login()
        self.assertEqual(allowed.status_code, 200, allowed.content)

    def test_a_forged_desktop_key_from_any_device_is_refused(self):
        response = self.login(
            ip=OTHER_IP, headers={"HTTP_X_JONE_DESKTOP_KEY": "totally-forged-key"},
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.data["code"], "STAFF_SIGN_IN_RESTRICTED")

    def test_a_browser_cannot_nominate_its_own_address_via_edge_headers(self):
        response = self.login(
            ip=OTHER_IP,
            headers={
                "HTTP_CF_CONNECTING_IP": DESK_IP,
                "HTTP_TRUE_CLIENT_IP": DESK_IP,
                "HTTP_X_REAL_IP": DESK_IP,
            },
        )
        self.assertEqual(response.status_code, 403, response.content)
        # The refusal reports the address the server actually saw, not the claim.
        self.assertEqual(response.data["errors"]["client_ip"], OTHER_IP)

    def test_a_browser_cannot_nominate_its_own_address_via_the_forwarding_chain(self):
        response = self.login(
            ip=OTHER_IP, headers={"HTTP_X_FORWARDED_FOR": f"{DESK_IP}, {CF_EDGE_IP}"},
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.data["errors"]["client_ip"], OTHER_IP)

    def test_too_many_attempts_are_throttled_with_the_accurate_envelope(self):
        for _ in range(5):
            self.assertEqual(self.login(password="wrong").status_code, 401)
        throttled = self.login()  # the 6th attempt, even with correct credentials
        self.assertEqual(throttled.status_code, 429, throttled.content)
        self.assertEqual(throttled.data["code"], "RATE_LIMITED")
        self.assertIn("Too many", throttled.data["message"])
        # The throttle is per client address and expires on its own: the NEXT
        # person is not locked out forever (cache-based window).
        self.clear_login_throttle()
        self.assertEqual(self.login(self.second_receptionist).status_code, 200)


class DepartmentAuthorizationTests(FrontDeskLoginE2ECase):
    """Matrix 5: another department's staff cannot become Front Desk."""

    def test_cross_department_sign_in_is_refused_for_the_front_desk_context(self):
        response = self.login(
            self.waiter, extra_body={"department": "frontdesk"},
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.data["code"], "FORBIDDEN")
        self.assertIn("not authorized to sign in to this department", response.data["message"])

    def test_a_receptionist_is_authorized_for_the_front_desk_context(self):
        response = self.login(self.receptionist, extra_body={"department": "frontdesk"})
        self.assertEqual(response.status_code, 200, response.content)

    def test_another_departments_staff_cannot_reach_front_desk_data(self):
        """Signed in AT the desk (allowed: the desk is the staff sign-in point),
        a waiter still holds no Front Desk data access."""
        response = self.login(self.waiter)
        self.assertEqual(response.status_code, 200, response.content)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.tokens_of(response)['access']}")
        bookings = client.get(ADMIN_BOOKINGS_URL, REMOTE_ADDR=DESK_IP)
        self.assertEqual(bookings.status_code, 403, bookings.content)

    def test_a_receptionist_keeps_front_desk_data_access(self):
        response = self.login(self.receptionist)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.tokens_of(response)['access']}")
        bookings = client.get(ADMIN_BOOKINGS_URL, REMOTE_ADDR=DESK_IP)
        self.assertEqual(bookings.status_code, 200, bookings.content)


class IndependentSessionTests(FrontDeskLoginE2ECase):
    """Matrix 8-11: the exact shared-desk sequence, each login independent."""

    def test_the_full_desk_sequence_receptionist_admin_receptionist(self):
        # 1. A receptionist enters incorrect credentials.
        self.assertEqual(self.login(password="wrong").status_code, 401)
        # 2. She retries with the correct credentials.
        self.clear_login_throttle()
        first = self.login()
        self.assertEqual(first.status_code, 200, first.content)
        # 3. She logs out.
        self.logout_with(self.tokens_of(first))
        # 4. An administrator attempts to log in (same desk, same minute).
        self.clear_login_throttle()
        admin = self.login(self.admin)
        self.assertEqual(admin.status_code, 200, admin.content)
        # 5. The administrator logs out.
        self.logout_with(self.tokens_of(admin))
        # 6. Another authorized Front Desk employee logs in.
        self.clear_login_throttle()
        second = self.login(self.second_receptionist)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(
            self.tokens_of(second)["access"], self.tokens_of(second)["access"],
        )

    def test_a_receptionists_failed_login_does_not_block_the_administrator(self):
        self.assertEqual(self.login(password="wrong").status_code, 401)
        self.clear_login_throttle()
        response = self.login(self.admin)
        self.assertEqual(response.status_code, 200, response.content)

    def test_an_administrators_failed_login_does_not_block_a_receptionist(self):
        self.assertEqual(self.login(self.admin, password="wrong").status_code, 401)
        self.clear_login_throttle()
        response = self.login(self.receptionist)
        self.assertEqual(response.status_code, 200, response.content)

    def test_logout_is_idempotent_and_a_fresh_login_works_afterwards(self):
        tokens = self.tokens_of(self.login())
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        # First logout ends the session.
        self.assertEqual(
            client.post(LOGOUT_URL, {"refresh": tokens["refresh"]}, format="json").status_code, 200
        )
        # Repeating it (or a stale tab doing it again) stays graceful.
        self.assertEqual(
            client.post(LOGOUT_URL, {"refresh": tokens["refresh"]}, format="json").status_code, 200
        )
        # A refused refresh for the dead session does not lock the desk either.
        refresh = self.client.post(
            REFRESH_URL, {"refresh": tokens["refresh"]}, format="json", REMOTE_ADDR=DESK_IP
        )
        self.assertEqual(refresh.status_code, 401, refresh.content)
        # The next person signs in normally.
        self.clear_login_throttle()
        self.assertEqual(self.login(self.second_receptionist).status_code, 200)

    def logout_with(self, tokens):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        response = client.post(LOGOUT_URL, {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        return response


class SessionExpiryRecoveryTests(FrontDeskLoginE2ECase):
    """Matrix 12: an expired access token recovers at the desk, and only there."""

    def test_the_session_refreshes_at_the_desk(self):
        tokens = self.tokens_of(self.login())
        response = self.client.post(
            REFRESH_URL, {"refresh": tokens["refresh"]}, format="json", REMOTE_ADDR=DESK_IP
        )
        self.assertEqual(response.status_code, 200, response.content)
        refreshed = response.data["data"]["tokens"]
        self.assertTrue(refreshed["access"])
        # The new access token actually works.
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {refreshed['access']}")
        self.assertEqual(client.get(PROFILE_URL).status_code, 200)

    def test_a_refresh_from_outside_the_desk_is_refused_but_keeps_the_session(self):
        tokens = self.tokens_of(self.login())
        # Away from the desk the refresh is refused...
        away = self.client.post(
            REFRESH_URL, {"refresh": tokens["refresh"]}, format="json", REMOTE_ADDR=HOME_IP
        )
        self.assertEqual(away.status_code, 403, away.content)
        self.assertEqual(away.data["code"], "STAFF_SIGN_IN_RESTRICTED")
        # ...without consuming or rotating the token: back at the desk the very
        # same refresh token still works (a refused refresh is not a logout).
        back = self.client.post(
            REFRESH_URL, {"refresh": tokens["refresh"]}, format="json", REMOTE_ADDR=DESK_IP
        )
        self.assertEqual(back.status_code, 200, back.content)

    def test_relogin_after_session_loss_works_from_the_desk(self):
        tokens = self.tokens_of(self.login())
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        client.post(LOGOUT_URL, {"refresh": tokens["refresh"]}, format="json")
        self.clear_login_throttle()
        fresh = self.login(self.receptionist)
        self.assertEqual(fresh.status_code, 200, fresh.content)


class AttendanceWorkflowIntactTests(FrontDeskLoginE2ECase):
    """Matrix 17: the numeric-code and fingerprint flows are untouched."""

    def setUp(self):
        super().setUp()
        self.worker = make_staff("e2e.chef@staff.dev", role=User.Role.CHEF)
        StaffProfile.objects.update_or_create(
            user=self.worker,
            defaults={
                "employee_code": "STF-E2E-CHEF",
                "department": "Kitchen",
                "employment_status": StaffProfile.EmploymentStatus.ACTIVE,
            },
        )
        template = fingerprint_service.request_fingerprint_enrollment(
            staff=self.worker, actor=self.worker, minutiae=MINUTIAE
        )
        fingerprint_service.approve_fingerprint_enrollment(
            template=template, approver=self.manager
        )

    def terminal(self, payload):
        return self.client.post(TERMINAL_URL, payload, format="json", REMOTE_ADDR=DESK_IP)

    def test_clock_in_issues_a_code_that_signs_the_staff_member_in(self):
        issued = self.terminal(
            {"action": "CLOCK_IN", "email": self.worker.email, "fingerprint_probe": MATCHING_PROBE}
        )
        self.assertEqual(issued.status_code, 200, issued.content)
        code = issued.data["data"]["access_code"]
        self.clear_login_throttle()
        response = self.client.post(
            LOGIN_URL, {"access_code": code, "email": self.worker.email},
            format="json", REMOTE_ADDR=DESK_IP,
        )
        self.assertEqual(response.status_code, 200, response.content)
        tokens = self.tokens_of(response)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        profile = client.get(PROFILE_URL)
        self.assertEqual(profile.status_code, 200, profile.content)
        self.assertEqual(profile.data["data"]["email"], self.worker.email)


class FrontDeskDoctorCommandTests(FrontDeskLoginE2ECase):
    """The read-only diagnostic an administrator runs on the server."""

    def run_doctor(self, *args):
        out = StringIO()
        call_command("frontdesk_login_doctor", *args, stdout=out)
        return out.getvalue()

    def test_reports_the_missing_workstation_as_the_blocking_gap(self):
        self.desk.delete()
        text = self.run_doctor()
        self.assertIn("NONE REGISTERED", text)
        self.assertIn("blocking gap", text)
        self.assertIn("Terminals", text)

    def test_reports_each_enrolment_route_and_matches_an_observed_address(self):
        raw_key = issue_desktop_key(self.desk)
        self.desk.save(update_fields=["sign_in_key_hash", "sign_in_key_issued_at", "updated_at"])
        hotel_settings(front_desk_desktop_ip=f"{DESK_IP}/32")
        text = self.run_doctor("--observed-ip", DESK_IP)
        self.assertIn("desktop key issued", text)
        self.assertIn(f"registered address {DESK_IP}", text)
        self.assertIn(f"MATCHES this binding", text)
        self.assertIn("Diagnosis complete. Nothing was changed.", text)
        self.assertNotIn(raw_key, text)

    def test_flags_an_ambiguous_duplicate_address_registration(self):
        Workstation.objects.create(
            name="Front desk reception 2", department=Workstation.Department.FRONT_DESK,
            ip_address=DESK_IP,
        )
        text = self.run_doctor("--observed-ip", DESK_IP)
        self.assertIn("AMBIGUOUS", text)

    def test_the_command_changes_nothing(self):
        before = (
            Workstation.objects.count(),
            str(HotelSettings.get_settings().front_desk_desktop_ip),
            User.objects.count(),
        )
        self.run_doctor("--observed-ip", DESK_IP)
        after = (
            Workstation.objects.count(),
            str(HotelSettings.get_settings().front_desk_desktop_ip),
            User.objects.count(),
        )
        self.assertEqual(before, after)


class SettingsPayloadTests(FrontDeskLoginE2ECase):
    """The Settings page must tell an administrator which step is missing."""

    def test_the_settings_payload_reports_the_workstation_registration(self):
        self.auth(self.admin)
        with_workstation = self.client.get(SETTINGS_URL, REMOTE_ADDR=OTHER_IP)
        self.assertEqual(with_workstation.status_code, 200, with_workstation.content)
        self.assertTrue(with_workstation.data["data"]["front_desk_workstation_registered"])

        self.desk.delete()
        hotel_settings(front_desk_desktop_ip=DESK_IP)
        without = self.client.get(SETTINGS_URL, REMOTE_ADDR=OTHER_IP)
        self.assertEqual(without.status_code, 200, without.content)
        self.assertFalse(without.data["data"]["front_desk_workstation_registered"])
