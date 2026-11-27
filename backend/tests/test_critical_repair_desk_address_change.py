# tests/test_critical_repair_desk_address_change.py
"""The receptionist must not be logged out when the desk's public IP changes.

Reported fault: an ISP lease renewal / router restart / failover link changed
the front desk's public address and every open shift ended at the next token
refresh.

Root cause: the ADDRESS was the workstation's only identity —

* ``resolve_receptionist_desktop`` no longer recognised the desk, so the
  refresh was treated as a request from outside the Receptionist Desktop; and
* the hotel-wide ``HotelSettings.front_desk_desktop_ip`` pin (usually one
  public address) no longer matched, so even a browser holding the issued
  desktop key was refused.

The repair gives the desk a durable identity (the hotel-issued, hashed,
revocable ``X-JONE-Desktop-Key``, issued automatically to an address-enrolled
browser at sign-in) and treats a changed address as a recorded fact rather than
a logout. Sign-in itself stays pinned to the desk; nothing about the address
handling trusts a client-supplied forwarding header any more than before.
"""
from django.core.cache import cache

from apps.accounts.desktop_policy import DESKTOP_HEADER, issue_desktop_key
from apps.accounts.models import User, Workstation
from apps.audit.models import AuditLog
from apps.hotel.models import HotelSettings

from .base import BaseAPITestCase
from .factories import hotel_settings, make_user

PASSWORD = "Str0ng!Pass"
DESK_IP = "102.89.34.7"
NEW_DESK_IP = "105.112.8.66"     # the desk after the ISP reassigned its lease
OTHER_IP = "102.89.34.99"
LOGIN_URL = "/api/auth/login/"
REFRESH_URL = "/api/auth/token/refresh/"


class DeskAddressChangeTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.receptionist = make_user(
            "desk-ip-receptionist@jone.test", password=PASSWORD, role=User.Role.RECEPTIONIST,
        )
        self.manager = make_user("desk-ip-manager@jone.test", password=PASSWORD, role=User.Role.MANAGER)
        self.desk = Workstation.objects.create(
            name="Front desk reception 1", department=Workstation.Department.FRONT_DESK,
            location="Lobby", ip_address=DESK_IP,
        )

    # ------------------------------------------------------------- helpers

    def login(self, *, ip=DESK_IP, headers=None, user=None):
        user = user or self.receptionist
        return self.client.post(
            LOGIN_URL, {"email": user.email, "password": PASSWORD},
            format="json", REMOTE_ADDR=ip, **(headers or {}),
        )

    def refresh(self, token, *, ip=DESK_IP, headers=None):
        return self.client.post(
            REFRESH_URL, {"refresh": token}, format="json", REMOTE_ADDR=ip, **(headers or {}),
        )

    def enrol(self):
        response = self.login()
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]["desk_enrolment"]["desktop_key"], response.json()["data"]["tokens"]["refresh"]

    # --------------------------------------------------------------- tests

    def test_the_first_desk_sign_in_issues_a_durable_workstation_credential(self):
        key, _ = self.enrol()
        self.assertTrue(key)
        self.desk.refresh_from_db()
        self.assertEqual(self.desk.sign_in_key_hash, __import__("hashlib").sha256(key.encode()).hexdigest())
        # The raw key is never stored and never logged.
        self.assertNotEqual(self.desk.sign_in_key_hash, key)
        entry = AuditLog.objects.filter(action="DESKTOP_KEY_AUTO_ISSUED").latest("created_at")
        self.assertEqual(entry.metadata["client_ip"], DESK_IP)
        self.assertNotIn(key, str(entry.metadata))
        self.assertNotIn(key, str(entry.changes))

    def test_the_session_survives_the_public_address_changing(self):
        key, refresh_token = self.enrol()
        # The ISP hands the desk a new address mid-shift.
        continued = self.refresh(refresh_token, ip=NEW_DESK_IP, headers={DESKTOP_HEADER: key})
        self.assertEqual(continued.status_code, 200, continued.content)
        self.assertTrue(continued.json()["data"]["tokens"]["access"])
        entry = AuditLog.objects.filter(action="STAFF_SESSION_ADDRESS_CHANGED").latest("created_at")
        self.assertEqual(entry.metadata["client_ip"], NEW_DESK_IP)
        self.assertEqual(entry.metadata["desktop_reference"], self.desk.reference)

    def test_the_session_survives_the_address_change_even_with_a_public_ip_pin(self):
        hotel_settings(front_desk_desktop_ip=DESK_IP)   # the address the desk had yesterday
        key, refresh_token = self.enrol()
        continued = self.refresh(refresh_token, ip=NEW_DESK_IP, headers={DESKTOP_HEADER: key})
        self.assertEqual(continued.status_code, 200, continued.content)
        self.assertEqual(HotelSettings.get_settings().front_desk_desktop_ip, DESK_IP)

    def test_a_session_without_a_workstation_credential_is_still_refused(self):
        _, refresh_token = self.enrol()
        refused = self.refresh(refresh_token, ip=OTHER_IP)
        self.assertEqual(refused.status_code, 403, refused.content)
        self.assertEqual(refused.json()["code"], "STAFF_SIGN_IN_RESTRICTED")

    def test_sign_in_off_the_desk_is_still_refused_while_holding_the_key(self):
        key, _ = self.enrol()
        hotel_settings(front_desk_desktop_ip=DESK_IP)
        # The stolen-key case: presenting it from elsewhere must NOT start a session.
        refused = self.login(ip=OTHER_IP, headers={DESKTOP_HEADER: key})
        self.assertEqual(refused.status_code, 403, refused.content)

    def test_rotating_the_key_ends_the_old_browser_immediately(self):
        key, refresh_token = self.enrol()
        issue_desktop_key(self.desk)
        self.desk.save(update_fields=["sign_in_key_hash", "sign_in_key_issued_at", "updated_at"])
        refused = self.refresh(refresh_token, ip=NEW_DESK_IP, headers={DESKTOP_HEADER: key})
        self.assertEqual(refused.status_code, 403, refused.content)

    def test_deactivating_the_workstation_ends_the_session(self):
        key, refresh_token = self.enrol()
        self.desk.is_active = False
        self.desk.save(update_fields=["is_active", "updated_at"])
        refused = self.refresh(refresh_token, ip=NEW_DESK_IP, headers={DESKTOP_HEADER: key})
        self.assertEqual(refused.status_code, 403, refused.content)

    def test_a_second_browser_cannot_rotate_the_desk_key_by_signing_in(self):
        key, _ = self.enrol()
        self.desk.refresh_from_db()
        digest = self.desk.sign_in_key_hash
        # Somebody signs in at the same desk from another browser with no key.
        again = self.login()
        self.assertEqual(again.status_code, 200, again.content)
        enrolment = again.json()["data"].get("desk_enrolment") or {}
        self.assertEqual(enrolment.get("desktop_key"), "")
        self.assertIn("already has a desktop key", enrolment.get("note", ""))
        self.desk.refresh_from_db()
        self.assertEqual(self.desk.sign_in_key_hash, digest)
        # The first browser keeps working.
        self.assertEqual(
            self.refresh(self.enrol_token(), ip=DESK_IP, headers={DESKTOP_HEADER: key}).status_code, 200
        )

    def enrol_token(self):
        return self.login().json()["data"]["tokens"]["refresh"]

    def test_an_enrolled_browser_is_not_re_enrolled_on_every_sign_in(self):
        key, _ = self.enrol()
        again = self.login(headers={DESKTOP_HEADER: key})
        self.assertEqual(again.status_code, 200, again.content)
        self.assertNotIn("desk_enrolment", again.json()["data"])

    def test_management_and_existing_credentials_are_left_alone(self):
        manager_login = self.login(user=self.manager)
        self.assertEqual(manager_login.status_code, 200, manager_login.content)
        self.assertNotIn("desk_enrolment", manager_login.json()["data"])
        self.desk.refresh_from_db()
        self.assertEqual(self.desk.sign_in_key_hash, "")

    def test_a_refused_sign_in_enrols_nothing(self):
        refused = self.login(ip=OTHER_IP)
        self.assertEqual(refused.status_code, 403, refused.content)
        self.desk.refresh_from_db()
        self.assertEqual(self.desk.sign_in_key_hash, "")
        self.assertFalse(AuditLog.objects.filter(action="DESKTOP_KEY_AUTO_ISSUED").exists())

    def test_manual_logout_still_blacklists_the_refresh_token(self):
        key, refresh_token = self.enrol()
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + self.login(
            headers={DESKTOP_HEADER: key},
        ).json()["data"]["tokens"]["access"])
        out = self.client.post("/api/auth/logout/", {"refresh": refresh_token}, format="json")
        self.assertEqual(out.status_code, 200, out.content)
        self.client.credentials()
        refused = self.refresh(refresh_token, ip=DESK_IP, headers={DESKTOP_HEADER: key})
        self.assertIn(refused.status_code, (401, 403), refused.content)
