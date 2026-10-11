# tests/test_auth.py
"""AUTHENTICATION tests (spec §93): registration, login, refresh, logout,
passwords, permissions, role-escalation prevention."""
from datetime import timedelta

from django.contrib.auth.tokens import default_token_generator
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User

from .base import BaseAPITestCase
from .factories import make_staff, make_user


class AuthTests(BaseAPITestCase):
    def test_register_creates_guest_account_and_tokens(self):
        response = self.client.post("/api/auth/register/", {
            "email": "new@guest.dev", "first_name": "Ngozi", "last_name": "Eze",
            "phone": "08030001122", "password": "Str0ng!Pass", "password_confirm": "Str0ng!Pass",
        })
        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertIn("access", payload["data"]["tokens"])
        self.assertEqual(payload["data"]["user"]["role"], "GUEST")
        self.assertEqual(User.objects.get(email="new@guest.dev").role, "GUEST")

    def test_register_rejects_duplicate_email(self):
        make_user("dup@guest.dev")
        response = self.client.post("/api/auth/register/", {
            "email": "dup@guest.dev", "first_name": "A", "last_name": "B",
            "password": "Str0ng!Pass", "password_confirm": "Str0ng!Pass",
        })
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "VALIDATION_ERROR")
        self.assertIn("email", body["errors"])

    def test_login_returns_tokens_and_user(self):
        make_user("login@guest.dev", password="Str0ng!Pass")
        response = self.client.post("/api/auth/login/", {
            "email": "login@guest.dev", "password": "Str0ng!Pass",
        })
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertIn("access", data["tokens"])
        self.assertEqual(data["user"]["email"], "login@guest.dev")

    def test_login_wrong_password_returns_401(self):
        make_user("x@guest.dev", password="Str0ng!Pass")
        response = self.client.post("/api/auth/login/", {"email": "x@guest.dev", "password": "wrong-pass"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "UNAUTHORIZED")

    def test_token_refresh_rotates_and_old_refresh_is_blacklisted(self):
        user = make_user("rotate@guest.dev", password="Str0ng!Pass")
        login = self.client.post("/api/auth/login/", {"email": "rotate@guest.dev", "password": "Str0ng!Pass"}).json()
        refresh = login["data"]["tokens"]["refresh"]
        first = self.client.post("/api/auth/token/refresh/", {"refresh": refresh})
        self.assertEqual(first.status_code, 200)
        # ROTATE_REFRESH_TOKENS + blacklist: the old refresh token is now dead.
        second = self.client.post("/api/auth/token/refresh/", {"refresh": refresh})
        self.assertEqual(second.status_code, 401)

    def test_logout_blacklists_refresh_token(self):
        user = make_user("out@guest.dev", password="Str0ng!Pass")
        login = self.client.post("/api/auth/login/", {"email": "out@guest.dev", "password": "Str0ng!Pass"}).json()
        tokens = login["data"]["tokens"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        response = self.client.post("/api/auth/logout/", {"refresh": tokens["refresh"]})
        self.assertEqual(response.status_code, 200)
        reuse = self.client.post("/api/auth/token/refresh/", {"refresh": tokens["refresh"]})
        self.assertEqual(reuse.status_code, 401)

    def test_profile_requires_auth(self):
        response = self.client.get("/api/auth/profile/")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "UNAUTHORIZED")

    def test_profile_patch_updates_contact_fields(self):
        user = make_user("prof@guest.dev", password="Str0ng!Pass")
        self.auth(user)
        response = self.client.patch("/api/auth/profile/", {"first_name": "Changed", "phone": "08099998888"})
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.first_name, "Changed")
        self.assertEqual(user.phone, "08099998888")

    def test_guest_cannot_escalate_own_role_via_profile(self):
        user = make_user("sneaky@guest.dev", password="Str0ng!Pass")
        self.auth(user)
        response = self.client.patch("/api/auth/profile/", {"role": "ADMIN", "is_staff": True})
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.role, "GUEST")
        self.assertFalse(user.is_staff)

    def test_password_change_flow(self):
        user = make_user("chg@guest.dev", password="Str0ng!Pass")
        self.auth(user)
        response = self.client.post("/api/auth/password/change/", {
            "current_password": "Str0ng!Pass",
            "new_password": "N3w!Password",
            "new_password_confirm": "N3w!Password",
        })
        self.assertEqual(response.status_code, 200)
        self.unauth()
        login = self.client.post("/api/auth/login/", {"email": "chg@guest.dev", "password": "N3w!Password"})
        self.assertEqual(login.status_code, 200)

    def test_password_reset_does_not_leak_account_existence(self):
        response_missing = self.client.post("/api/auth/password/reset/", {"email": "nobody@guest.dev"})
        self.assertEqual(response_missing.status_code, 200)
        self.assertTrue(response_missing.json()["success"])

    def test_password_reset_confirm_sets_new_password(self):
        user = make_user("reset@guest.dev", password="Str0ng!Pass")
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        response = self.client.post("/api/auth/password/reset/confirm/", {
            "uid": uid, "token": token,
            "new_password": "Reset!Pass123", "new_password_confirm": "Reset!Pass123",
        })
        self.assertEqual(response.status_code, 200)
        login = self.client.post("/api/auth/login/", {"email": "reset@guest.dev", "password": "Reset!Pass123"})
        self.assertEqual(login.status_code, 200)

    def test_password_reset_rejects_bad_token(self):
        user = make_user("badreset@guest.dev")
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        response = self.client.post("/api/auth/password/reset/confirm/", {
            "uid": uid, "token": "bogus-token",
            "new_password": "Reset!Pass123", "new_password_confirm": "Reset!Pass123",
        })
        self.assertEqual(response.status_code, 400)

    def test_staff_only_namespace_rejects_guests(self):
        guest = make_user("staff-nope@guest.dev", password="Str0ng!Pass")
        self.auth(guest)
        for url in ["/api/admin/dashboard/", "/api/admin/bookings/", "/api/admin/users/",
                    "/api/admin/reports/revenue/"]:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 403, url)
            self.assertEqual(response.json()["code"], "FORBIDDEN")


class LogoutContractTests(BaseAPITestCase):
    """The audit's role sweep could not confirm three of 23 logout responses.

    Nothing was broken: logout is deliberately idempotent. A refresh token that
    is already blacklisted, expired or malformed still answers 200, because the
    session is dead server-side either way and a user must never be trapped in a
    session they are trying to leave. Only a missing token is a client error.
    These tests pin all of those answer paths, for every staff role, so the
    contract is evidence rather than an assumption.

    Tokens are minted directly instead of by logging in, because the login
    endpoint is throttled to 5/min by design (covered in ``test_security_guards``)
    and a role sweep must not be defeated by the rate limiter.
    """

    def _staff_roles(self):
        return [role for role in User.Role.values if role != User.Role.GUEST]

    def test_every_staff_role_can_log_out_and_its_refresh_token_dies(self):
        for index, role in enumerate(self._staff_roles()):
            with self.subTest(role=role):
                user = make_staff(f"logout.role{index}@staff.dev", role=role)
                refresh = str(RefreshToken.for_user(user))
                self.auth(user)
                response = self.client.post("/api/auth/logout/", {"refresh": refresh}, format="json")
                self.assertEqual(response.status_code, 200, response.content)
                self.assertTrue(response.json()["success"])

                self.unauth()
                reuse = self.client.post("/api/auth/token/refresh/", {"refresh": refresh}, format="json")
                self.assertEqual(reuse.status_code, 401, "the refresh token survived logout")

    def test_logout_is_idempotent_when_the_same_token_is_replayed(self):
        user = make_staff("logout.replay@staff.dev")
        refresh = str(RefreshToken.for_user(user))
        self.auth(user)
        first = self.client.post("/api/auth/logout/", {"refresh": refresh}, format="json")
        second = self.client.post("/api/auth/logout/", {"refresh": refresh}, format="json")
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(
            second.status_code, 200,
            "a user retrying logout must not be refused by a token that logout already revoked",
        )

    def test_logout_tolerates_a_malformed_or_expired_token(self):
        user = make_staff("logout.stale@staff.dev")
        self.auth(user)

        malformed = self.client.post("/api/auth/logout/", {"refresh": "not-a-jwt"}, format="json")
        self.assertEqual(malformed.status_code, 200, malformed.content)

        expired_token = RefreshToken.for_user(user)
        expired_token.set_exp(from_time=timezone.now() - timedelta(days=30))
        expired = self.client.post(
            "/api/auth/logout/", {"refresh": str(expired_token)}, format="json"
        )
        self.assertEqual(expired.status_code, 200, expired.content)

    def test_logout_without_a_refresh_token_reports_a_client_error(self):
        user = make_staff("logout.missing@staff.dev")
        self.auth(user)
        response = self.client.post("/api/auth/logout/", {}, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        body = response.json()
        self.assertEqual(body["code"], "VALIDATION_ERROR")
        self.assertIn("refresh", body["errors"])

    def test_logout_requires_an_authenticated_caller(self):
        self.unauth()
        response = self.client.post("/api/auth/logout/", {"refresh": "anything"}, format="json")
        self.assertEqual(response.status_code, 401, response.content)

    def test_logout_does_not_revoke_another_accounts_session(self):
        """A logged-out account's token cannot be used to end someone else's."""
        victim = make_staff("logout.victim@staff.dev", role=User.Role.MANAGER)
        attacker = make_staff("logout.attacker@staff.dev", role=User.Role.RECEPTIONIST)
        victim_refresh = str(RefreshToken.for_user(victim))

        self.auth(attacker)
        response = self.client.post("/api/auth/logout/", {"refresh": victim_refresh}, format="json")
        # Idempotent logout answers 200 either way: what matters is whether the
        # victim's token still works.
        self.assertEqual(response.status_code, 200, response.content)
        self.unauth()
        still_valid = self.client.post(
            "/api/auth/token/refresh/", {"refresh": victim_refresh}, format="json"
        )
        self.assertEqual(
            still_valid.status_code, 200,
            "logout must only be able to blacklist the caller's own refresh token",
        )
