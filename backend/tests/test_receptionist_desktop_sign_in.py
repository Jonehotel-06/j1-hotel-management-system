# tests/test_receptionist_desktop_sign_in.py
"""Staff sign-in is restricted to the Receptionist Desktop (server-enforced)."""
import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.core.cache import cache
from django.test import override_settings

from apps.accounts.admin_site import JOneAdminLoginForm, admin_access_allowed
from apps.accounts.desktop_policy import (
    DESKTOP_HEADER, hash_desktop_key, issue_desktop_key, receptionist_desktop_for_key,
    sign_in_policy_applies,
)
from apps.accounts.models import User, Workstation
from apps.audit.models import AuditLog

from .base import BaseAPITestCase
from .factories import make_user

PASSWORD = "Str0ng!Pass"
LOGIN_URL = "/api/auth/login/"


class ReceptionistDesktopSignInTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.desk = Workstation.objects.create(
            name="Front desk reception 1", department=Workstation.Department.FRONT_DESK, location="Lobby",
        )
        self.desk_key = issue_desktop_key(self.desk)
        self.desk.save()
        self.receptionist = make_user("reception@jone.test", password=PASSWORD, role=User.Role.RECEPTIONIST)
        self.cashier = make_user("cashier@jone.test", password=PASSWORD, role=User.Role.CASHIER)
        self.manager = make_user("manager@jone.test", password=PASSWORD, role=User.Role.MANAGER)
        self.admin = make_user("admin@jone.test", password=PASSWORD, role=User.Role.ADMIN)
        self.gm = make_user("gm@jone.test", password=PASSWORD, role=User.Role.GENERAL_MANAGER)
        self.guest = make_user("guest@jone.test", password=PASSWORD, role=User.Role.GUEST)

    def login(self, email, password=PASSWORD, key=None):
        headers = {DESKTOP_HEADER: key} if key is not None else {}
        return self.client.post(LOGIN_URL, {"email": email, "password": password}, **headers)

    # --- rejection ---------------------------------------------------------
    def test_operational_staff_without_desktop_key_is_refused_with_403_and_no_tokens(self):
        response = self.login(self.receptionist.email)
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "STAFF_SIGN_IN_RESTRICTED")
        self.assertNotIn("tokens", str(body))
        self.assertNotIn("access", str(body))

    def test_cashier_is_refused_from_a_non_reception_device(self):
        response = self.login(self.cashier.email)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "STAFF_SIGN_IN_RESTRICTED")

    def test_wrong_desktop_key_is_refused(self):
        response = self.login(self.receptionist.email, key="not-a-real-key")
        self.assertEqual(response.status_code, 403)

    def test_valid_key_with_wrong_password_is_still_unauthorized(self):
        response = self.login(self.receptionist.email, password="wrong-pass", key=self.desk_key)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "UNAUTHORIZED")

    def test_key_of_a_non_reception_workstation_is_refused(self):
        shared = Workstation.objects.create(name="Shared", department=Workstation.Department.SHARED)
        shared_key = issue_desktop_key(shared)  # a key can be minted for any workstation
        shared.save()
        response = self.login(self.receptionist.email, key=shared_key)
        self.assertEqual(response.status_code, 403)

    def test_key_of_a_deactivated_desk_is_refused(self):
        self.desk.is_active = False
        self.desk.save(update_fields=["is_active"])
        response = self.login(self.receptionist.email, key=self.desk_key)
        self.assertEqual(response.status_code, 403)

    def test_rotated_key_invalidates_the_previous_key(self):
        old_key = self.desk_key
        self.desk.refresh_from_db()
        new_key = issue_desktop_key(self.desk)
        self.desk.save()
        self.assertEqual(self.login(self.receptionist.email, key=old_key).status_code, 403)
        self.assertEqual(self.login(self.receptionist.email, key=new_key).status_code, 200)

    # --- success -----------------------------------------------------------
    def test_receptionist_signs_in_from_the_enrolled_desk(self):
        response = self.login(self.receptionist.email, key=self.desk_key)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertIn("access", data["tokens"])
        self.assertEqual(data["user"]["role"], "RECEPTIONIST")

    def test_management_keeps_access_from_any_device(self):
        for user in (self.admin, self.manager, self.gm):
            with self.subTest(role=user.role):
                self.assertEqual(self.login(user.email).status_code, 200)

    def test_guest_sign_in_is_unaffected(self):
        self.assertEqual(self.login(self.guest.email).status_code, 200)

    def test_successful_staff_sign_in_is_audited_with_desk_reference_only(self):
        self.login(self.receptionist.email, key=self.desk_key)
        entry = AuditLog.objects.filter(action="STAFF_SIGN_IN", actor=self.receptionist).latest("id")
        self.assertEqual(entry.metadata["desktop_reference"], self.desk.reference)
        self.assertEqual(entry.metadata["policy"], "receptionist_desktop")
        self.assertNotIn(self.desk_key, str(entry.metadata))
        self.assertNotIn(PASSWORD, str(entry.metadata))

    def test_refused_sign_in_is_audited_and_logged_without_secrets(self):
        with self.assertLogs("apps", level=logging.INFO) as captured:
            self.login(self.cashier.email, password=PASSWORD)
        entry = AuditLog.objects.get(action="STAFF_SIGN_IN_RESTRICTED")
        self.assertEqual(entry.metadata["reason"], "no_receptionist_desktop")
        self.assertEqual(entry.metadata["role"], "CASHIER")
        combined = "\n".join(captured.output) + str(entry.metadata) + entry.summary
        self.assertNotIn(PASSWORD, combined)
        self.assertNotIn(self.cashier.email, combined)

    def test_invalid_credentials_are_logged_without_email_or_password(self):
        with self.assertLogs("apps", level=logging.INFO) as captured:
            self.login("nobody@jone.test", password="Wrong!Guess-99")
        combined = "\n".join(captured.output)
        self.assertIn("Sign-in refused", combined)
        self.assertNotIn("Wrong!Guess-99", combined)
        self.assertNotIn("nobody@jone.test", combined)

    def test_desk_key_is_not_logged_on_successful_sign_in(self):
        with self.assertLogs("apps", level=logging.INFO) as captured:
            self.login(self.receptionist.email, key=self.desk_key)
        self.assertNotIn(self.desk_key, "\n".join(captured.output))

    def test_key_header_cannot_be_used_to_reach_a_different_role(self):
        # A desk key authenticates the device, not the person: the password of
        # the account still has to match, and the role comes from the account.
        response = self.login(self.cashier.email, key=self.desk_key)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["user"]["role"], "CASHIER")


class ReceptionistDesktopPolicyUnitTests(BaseAPITestCase):
    def test_policy_applies_to_operational_roles_except_management(self):
        self.assertTrue(sign_in_policy_applies(make_user("a@jone.test", role=User.Role.RECEPTIONIST)))
        self.assertTrue(sign_in_policy_applies(make_user("b@jone.test", role=User.Role.HOUSEKEEPING)))
        self.assertFalse(sign_in_policy_applies(make_user("c@jone.test", role=User.Role.MANAGER)))
        self.assertFalse(sign_in_policy_applies(make_user("d@jone.test", role=User.Role.ADMIN)))
        self.assertFalse(sign_in_policy_applies(make_user("e@jone.test", role=User.Role.GUEST)))

    def test_only_a_digest_is_stored(self):
        desk = Workstation.objects.create(name="Desk", department=Workstation.Department.FRONT_DESK)
        raw = issue_desktop_key(desk)
        desk.save()
        desk.refresh_from_db()
        self.assertEqual(desk.sign_in_key_hash, hash_desktop_key(raw))
        self.assertNotIn(raw, desk.sign_in_key_hash)
        self.assertEqual(receptionist_desktop_for_key(raw).pk, desk.pk)
        self.assertIsNone(receptionist_desktop_for_key(""))
        self.assertIsNone(receptionist_desktop_for_key("x" * 500))


class WorkstationDesktopKeyManagementTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_user("admin2@jone.test", password=PASSWORD, role=User.Role.ADMIN)
        self.receptionist = make_user("rec2@jone.test", password=PASSWORD, role=User.Role.RECEPTIONIST)
        self.auth(self.admin)

    def test_creating_a_front_desk_workstation_returns_the_key_once(self):
        response = self.client.post("/api/admin/users/terminals/", {
            "name": "Front desk reception 2", "department": "FRONT_DESK", "location": "Lobby",
        }, format="json")
        self.assertEqual(response.status_code, 201)
        body = response.json()["data"]
        key = body["receptionist_desktop_key"]
        self.assertTrue(body["receptionist_key_configured"])
        listing = self.client.get("/api/admin/users/terminals/").json()["data"]
        self.assertNotIn("receptionist_desktop_key", str(listing))
        self.assertNotIn(key, str(listing))
        self.assertTrue(receptionist_desktop_for_key(key))

    def test_creating_a_non_reception_workstation_issues_no_key(self):
        response = self.client.post("/api/admin/users/terminals/", {
            "name": "Kitchen pass", "department": "KITCHEN",
        }, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertNotIn("receptionist_desktop_key", response.json()["data"])
        self.assertFalse(response.json()["data"]["receptionist_key_configured"])

    def test_rotate_endpoint_revokes_old_key_and_returns_new_once(self):
        desk = Workstation.objects.create(name="Desk", department=Workstation.Department.FRONT_DESK)
        old = issue_desktop_key(desk)
        desk.save()
        response = self.client.post(f"/api/admin/users/terminals/{desk.pk}/desktop-key/")
        self.assertEqual(response.status_code, 200)
        new = response.json()["data"]["receptionist_desktop_key"]
        self.assertNotEqual(old, new)
        self.assertIsNone(receptionist_desktop_for_key(old))
        self.assertIsNotNone(receptionist_desktop_for_key(new))
        self.assertTrue(AuditLog.objects.filter(action="RECEPTIONIST_DESKTOP_KEY_ISSUED").exists())
        self.assertNotIn(new, str(AuditLog.objects.filter(action="RECEPTIONIST_DESKTOP_KEY_ISSUED").values()))

    def test_rotate_rejects_non_front_desk_and_inactive_workstations(self):
        kitchen = Workstation.objects.create(name="Kitchen", department=Workstation.Department.KITCHEN)
        self.assertEqual(self.client.post(f"/api/admin/users/terminals/{kitchen.pk}/desktop-key/").status_code, 400)
        inactive = Workstation.objects.create(
            name="Old desk", department=Workstation.Department.FRONT_DESK, is_active=False,
        )
        self.assertEqual(self.client.post(f"/api/admin/users/terminals/{inactive.pk}/desktop-key/").status_code, 400)

    def test_moving_a_desk_out_of_front_desk_clears_its_key(self):
        desk = Workstation.objects.create(name="Desk", department=Workstation.Department.FRONT_DESK)
        raw = issue_desktop_key(desk)
        desk.save()
        response = self.client.patch(f"/api/admin/users/terminals/{desk.pk}/", {"department": "SHARED"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(receptionist_desktop_for_key(raw))
        self.assertFalse(response.json()["data"]["receptionist_key_configured"])

    def test_receptionist_cannot_rotate_desktop_keys(self):
        desk = Workstation.objects.create(name="Desk", department=Workstation.Department.FRONT_DESK)
        self.auth(self.receptionist)
        self.assertEqual(self.client.post(f"/api/admin/users/terminals/{desk.pk}/desktop-key/").status_code, 403)


class DjangoAdminSignInRestrictionTests(BaseAPITestCase):
    def test_admin_site_uses_the_restricted_site(self):
        from django.contrib import admin
        from apps.accounts.admin_site import JOneAdminSite
        self.assertIsInstance(admin.site, JOneAdminSite)

    def test_only_management_and_superusers_may_use_django_admin(self):
        admin_user = make_user("dj-admin@jone.test", role=User.Role.ADMIN, is_staff=True)
        manager = make_user("dj-mgr@jone.test", role=User.Role.MANAGER, is_staff=True)
        cashier = make_user("dj-cash@jone.test", role=User.Role.CASHIER, is_staff=True)
        superuser = get_user_model().objects.create_superuser(email="root@jone.test", password=PASSWORD)
        self.assertTrue(admin_access_allowed(admin_user))
        self.assertTrue(admin_access_allowed(manager))
        self.assertTrue(admin_access_allowed(superuser))
        self.assertFalse(admin_access_allowed(cashier))

    def test_refused_operational_account_gets_no_django_admin_session(self):
        make_user("dj-cash2@jone.test", password=PASSWORD, role=User.Role.CASHIER, is_staff=True)
        # The login form rejects before Django creates a session.
        form = JOneAdminLoginForm(request=None, data={"username": "dj-cash2@jone.test", "password": PASSWORD})
        self.assertFalse(form.is_valid())
        self.assertIn("Please enter the correct email and password", str(form.errors))

    def test_admin_login_form_accepts_management(self):
        make_user("dj-mgr2@jone.test", password=PASSWORD, role=User.Role.MANAGER, is_staff=True)
        form = JOneAdminLoginForm(request=None, data={"username": "dj-mgr2@jone.test", "password": PASSWORD})
        self.assertTrue(form.is_valid(), form.errors)


class StaffRefreshTokenRestrictionTests(BaseAPITestCase):
    """Refresh tokens issued before the desk rule must not keep staff signed in elsewhere."""

    def setUp(self):
        super().setUp()
        self.desk = Workstation.objects.create(name="Desk R", department=Workstation.Department.FRONT_DESK)
        self.key = issue_desktop_key(self.desk)
        self.desk.save()
        self.receptionist = make_user("refresh-rec@jone.test", password=PASSWORD, role=User.Role.RECEPTIONIST)
        self.manager = make_user("refresh-mgr@jone.test", password=PASSWORD, role=User.Role.MANAGER)

    def _refresh_token_for(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken
        return str(RefreshToken.for_user(user))

    def test_refresh_is_refused_for_staff_without_desk_key_and_token_is_not_rotated(self):
        refresh = self._refresh_token_for(self.receptionist)
        response = self.client.post("/api/auth/token/refresh/", {"refresh": refresh}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "STAFF_SIGN_IN_RESTRICTED")
        # The same refresh token still works once the desk key is presented.
        ok = self.client.post(
            "/api/auth/token/refresh/", {"refresh": refresh}, format="json", **{DESKTOP_HEADER: self.key},
        )
        self.assertEqual(ok.status_code, 200)

    def test_refresh_is_allowed_for_management_without_desk_key(self):
        refresh = self._refresh_token_for(self.manager)
        response = self.client.post("/api/auth/token/refresh/", {"refresh": refresh}, format="json")
        self.assertEqual(response.status_code, 200)

    def test_malformed_refresh_token_keeps_standard_unauthorized_response(self):
        response = self.client.post("/api/auth/token/refresh/", {"refresh": "garbage"}, format="json")
        self.assertEqual(response.status_code, 401)
