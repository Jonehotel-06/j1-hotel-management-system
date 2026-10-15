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
        invalid = self.client.patch(
            SETTINGS_URL, {"front_desk_desktop_ip": "192.0.2.0/24"}, format="json",
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
