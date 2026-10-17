"""Front Desk desktop IP: Admin settings + sign-in/refresh/attendance enforcement."""
from django.core.cache import cache

from apps.accounts.desktop_policy import DESKTOP_HEADER, issue_desktop_key
from apps.accounts.models import User, Workstation
from apps.audit.models import AuditLog
from apps.hotel.models import HotelSettings

from .base import BaseAPITestCase
from .factories import hotel_settings, make_user

PASSWORD = "Str0ng!Pass"
DESK_IP = "192.0.2.10"
OTHER_IP = "192.0.2.11"
LOGIN_URL = "/api/auth/login/"
SETTINGS_URL = "/api/admin/settings/"


class FrontDeskDesktopIpTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.admin = make_user("ip-admin@jone.test", password=PASSWORD, role=User.Role.ADMIN)
        self.manager = make_user("ip-manager@jone.test", password=PASSWORD, role=User.Role.MANAGER)
        self.receptionist = make_user("ip-reception@jone.test", password=PASSWORD, role=User.Role.RECEPTIONIST)
        self.desk = Workstation.objects.create(
            name="Front desk desktop", department=Workstation.Department.FRONT_DESK, location="Lobby",
        )
        self.desk_key = issue_desktop_key(self.desk)
        self.desk.save()

    def login(self, user, *, ip, key=None):
        headers = {DESKTOP_HEADER: key} if key is not None else {}
        return self.client.post(
            LOGIN_URL, {"email": user.email, "password": PASSWORD},
            REMOTE_ADDR=ip, **headers,
        )

    def test_admin_can_change_desktop_ip_in_settings_without_code(self):
        self.auth(self.manager)
        self.assertEqual(self.client.get(SETTINGS_URL).status_code, 403)
        self.auth(self.admin)
        # A hostname can never be registered: it would have to be resolved on
        # every sign-in, so the check would follow a DNS answer, not the desk.
        invalid = self.client.patch(
            SETTINGS_URL, {"front_desk_desktop_ip": "frontdesk.local"}, format="json",
        )
        self.assertEqual(invalid.status_code, 400, invalid.content)
        saved = self.client.patch(
            SETTINGS_URL, {"front_desk_desktop_ip": DESK_IP}, format="json",
        )
        self.assertEqual(saved.status_code, 200, saved.content)
        self.assertEqual(saved.json()["data"]["front_desk_desktop_ip"], DESK_IP)
        self.assertEqual(saved.json()["data"]["request_ip"], "127.0.0.1")
        self.assertEqual(HotelSettings.get_settings().front_desk_desktop_ip, DESK_IP)
        # Changing the desktop is just another settings update.
        changed = self.client.patch(
            SETTINGS_URL, {"front_desk_desktop_ip": "198.51.100.25"}, format="json",
        )
        self.assertEqual(changed.status_code, 200, changed.content)
        self.assertEqual(HotelSettings.get_settings().front_desk_desktop_ip, "198.51.100.25")

    def test_front_desk_login_requires_configured_ip_in_addition_to_desktop_key(self):
        hotel_settings(front_desk_desktop_ip=DESK_IP)
        allowed = self.login(self.receptionist, ip=DESK_IP, key=self.desk_key)
        self.assertEqual(allowed.status_code, 200, allowed.content)
        refused = self.login(self.receptionist, ip=OTHER_IP, key=self.desk_key)
        self.assertEqual(refused.status_code, 403, refused.content)
        self.assertEqual(refused.json()["code"], "STAFF_SIGN_IN_RESTRICTED")
        entry = AuditLog.objects.filter(action="STAFF_SIGN_IN_RESTRICTED", actor=self.receptionist).latest("id")
        self.assertEqual(entry.metadata["reason"], "front_desk_ip_mismatch")
        # Management accounts are not Front Desk desktop accounts and keep
        # their existing any-device access.
        self.assertEqual(self.login(self.manager, ip=OTHER_IP).status_code, 200)
        self.assertEqual(self.login(self.admin, ip=OTHER_IP).status_code, 200)

    def test_refresh_and_attendance_clock_require_configured_ip(self):
        hotel_settings(front_desk_desktop_ip=DESK_IP)
        login = self.login(self.receptionist, ip=DESK_IP, key=self.desk_key)
        self.assertEqual(login.status_code, 200, login.content)
        refresh = login.json()["data"]["tokens"]["refresh"]

        refused_refresh = self.client.post(
            "/api/auth/token/refresh/", {"refresh": refresh}, format="json",
            REMOTE_ADDR=OTHER_IP, **{DESKTOP_HEADER: self.desk_key},
        )
        self.assertEqual(refused_refresh.status_code, 403, refused_refresh.content)
        allowed_refresh = self.client.post(
            "/api/auth/token/refresh/", {"refresh": refresh}, format="json",
            REMOTE_ADDR=DESK_IP, **{DESKTOP_HEADER: self.desk_key},
        )
        self.assertEqual(allowed_refresh.status_code, 200, allowed_refresh.content)

        self.auth(self.receptionist)
        refused_clock = self.client.post(
            "/api/admin/staff-operations/attendance/clock/",
            {"action": "CLOCK_IN", "idempotency_key": "ip-clock-wrong"}, format="json",
            REMOTE_ADDR=OTHER_IP, **{DESKTOP_HEADER: self.desk_key},
        )
        self.assertEqual(refused_clock.status_code, 403, refused_clock.content)
        self.assertIn("configured Front Desk desktop IP", str(refused_clock.content))
        allowed_clock = self.client.post(
            "/api/admin/staff-operations/attendance/clock/",
            {"action": "CLOCK_IN", "idempotency_key": "ip-clock-right"}, format="json",
            REMOTE_ADDR=DESK_IP, **{DESKTOP_HEADER: self.desk_key},
        )
        self.assertEqual(allowed_clock.status_code, 201, allowed_clock.content)

    def test_blank_ip_keeps_existing_desktop_key_behaviour(self):
        self.assertEqual(HotelSettings.get_settings().front_desk_desktop_ip, "")
        response = self.login(self.receptionist, ip=OTHER_IP, key=self.desk_key)
        self.assertEqual(response.status_code, 200, response.content)


class FrontDeskNetworkBindingTests(BaseAPITestCase):
    """The hotel-wide binding accepts a network, so a reassigned address still works.

    A single public address is the tightest binding, but consumer/small-office
    links are routinely renumbered (and many Nigerian ISPs put customers behind
    carrier-grade NAT). Registering the network the desk sits on is the honest
    way to keep the front desk restricted without re-editing Settings every
    morning — and it must never widen to the whole internet by accident.
    """

    def setUp(self):
        super().setUp()
        cache.clear()
        self.admin = make_user("net-admin@jone.test", password=PASSWORD, role=User.Role.ADMIN)
        self.receptionist = make_user("net-reception@jone.test", password=PASSWORD, role=User.Role.RECEPTIONIST)

    def _bind(self, value):
        self.auth(self.admin)
        response = self.client.patch(
            SETTINGS_URL, {"front_desk_desktop_ip": value}, format="json",
        )
        self.auth(None)
        return response

    def _login(self, ip):
        return self.client.post(
            LOGIN_URL, {"email": self.receptionist.email, "password": PASSWORD},
            REMOTE_ADDR=ip,
            **{DESKTOP_HEADER: self._desk_key()},
        )

    def _desk_key(self):
        desk, _ = Workstation.objects.get_or_create(
            name="Network desk", defaults={"department": Workstation.Department.FRONT_DESK},
        )
        raw = issue_desktop_key(desk)
        desk.save()
        return raw

    def test_a_network_is_accepted_and_normalised(self):
        response = self._bind("102.89.34.77/24")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["front_desk_desktop_ip"], "102.89.34.0/24")

    def test_any_address_inside_the_registered_network_is_the_desk(self):
        self._bind("102.89.34.0/24")
        allowed = self._login("102.89.34.55")
        self.assertEqual(allowed.status_code, 200, allowed.content)
        refused = self._login("102.89.35.55")
        self.assertEqual(refused.status_code, 403, refused.content)
        self.assertEqual(refused.json()["code"], "STAFF_SIGN_IN_RESTRICTED")

    def test_several_entries_can_be_registered(self):
        response = self._bind("102.89.34.7, 41.58.10.0/24")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["front_desk_desktop_ip"], "102.89.34.7, 41.58.10.0/24")
        self.assertEqual(self._login("102.89.34.7").status_code, 200)
        self.assertEqual(self._login("41.58.10.99").status_code, 200)
        self.assertEqual(self._login("41.58.11.1").status_code, 403)

    def test_a_malformed_entry_is_refused_rather_than_silently_ignored(self):
        response = self._bind("102.89.34.7, not-an-address")
        self.assertEqual(response.status_code, 400, response.content)

    def test_the_model_normalises_direct_writes_too(self):
        settings_obj = HotelSettings.get_settings()
        settings_obj.front_desk_desktop_ip = " 102.89.34.7 ,102.89.34.7 "
        settings_obj.full_clean()
        self.assertEqual(settings_obj.front_desk_desktop_ip, "102.89.34.7")

    def test_the_settings_payload_flags_a_hosting_proxy_address(self):
        """An admin must not be invited to register the platform's own proxy."""
        self.auth(self.admin)
        response = self.client.get(SETTINGS_URL, REMOTE_ADDR="100.64.0.18")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["request_ip"], "100.64.0.18")
        self.assertTrue(data["request_ip_is_infrastructure"])

    def test_the_settings_payload_does_not_flag_a_public_desk_address(self):
        self.auth(self.admin)
        response = self.client.get(SETTINGS_URL, REMOTE_ADDR="102.89.34.7")
        self.assertFalse(response.json()["data"]["request_ip_is_infrastructure"])
