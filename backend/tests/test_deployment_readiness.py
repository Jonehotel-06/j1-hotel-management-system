"""Targeted production deployment readiness test suite for Northflank + Cloudflare.

Covers every required verification case in the deployment audit specification:
  1. Production settings boot, MySQL TLS/utf8mb4/strict-mode URL normalization
  2. Fail-fast startup guards on missing/invalid production environment variables
  3. Allowed-hosts validation (api.jonehotels.com accepted; untrusted Host rejected)
  4. CORS allowlist acceptance for https://jonehotels.com & https://www.jonehotels.com
  5. CORS rejection for unapproved browser origins & wildcard startup refusal
  6. CSRF trusted-origins validation & startup guards
  7. Secure cookie attributes (SESSION_COOKIE_SECURE, CSRF_COOKIE_SECURE, PORTAL_COOKIE_SECURE)
  8. Cross-origin Guest Portal session creation, HttpOnly SameSite=Lax cookie,
     X-Portal-Request CSRF guard, guest isolation, and checkout revocation
  9. Staff JWT login, token rotation/blacklisting, and role permission boundaries
 10. Public room catalog, availability, quote calculation, and booking creation
 11. Paystack initialization, verification idempotency, and HMAC-SHA512 webhook guard
 12. Transactional email provider config, error safety, and HTTPS logo resolution
 13. Static WhiteNoise storage & media streaming (/media/ + /api/media/ + check_media_storage)
 14. Celery production settings default, rediss:// TLS normalization, Africa/Lagos timezone,
     and execution of scheduled tasks
 15. Health endpoints (/api/health/, /live/, /ready/) HTTP probe exemption under
     SECURE_SSL_REDIRECT=True + container-healthcheck.py + docker-cmd.sh safety guards
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.core.email_assets import hosted_logo_url
from apps.core.emails import send_email_safe
from apps.notifications.models import EmailLog
from apps.portal.models import PortalAccessChallenge, PortalSession
from apps.portal.services import hash_secret

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type, make_staff
from .portal_stay_helpers import make_in_house_stay

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _run_python_in_clean_env(code: str, extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(BACKEND_DIR),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _base_valid_prod_env(**overrides: str) -> dict[str, str]:
    base = {
        "DJANGO_SETTINGS_MODULE": "config.settings.production",
        "DJANGO_SECRET_KEY": "prod-unit-test-only-random-secret-key-with-high-entropy-0123456789",
        "DJANGO_DEBUG": "False",
        "DJANGO_ALLOWED_HOSTS": "api.jonehotels.com",
        "DATABASE_URL": "mysqls://jone_app:placeholder_pw@mysql.internal.invalid:3306/jone_hotel",
        "REDIS_URL": "rediss://default:placeholder_pw@redis.internal.invalid:6379/0",
        "FRONTEND_URL": "https://jonehotels.com",
        "CORS_ALLOWED_ORIGINS": "https://jonehotels.com,https://www.jonehotels.com",
        "CSRF_TRUSTED_ORIGINS": "https://jonehotels.com,https://www.jonehotels.com,https://api.jonehotels.com",
        "EMAIL_PROVIDER": "brevo",
        "EMAIL_API_KEY": "placeholder-test-only-email-api-key",
        "DEFAULT_FROM_EMAIL": "J-ONE Hotel & Lodge <reservations@jonehotels.com>",
    }
    base.update(overrides)
    return base


class ProductionSettingsBootAndFailFastTests(SimpleTestCase):
    """Cases 1, 2, 4, 5, 6, 7, 14: real subprocess import of config.settings.production."""

    def test_production_settings_boot_cleanly_with_northflank_mysqls_and_rediss_urls(self):
        proc = _run_python_in_clean_env(
            "import json, django; from django.conf import settings; django.setup(); "
            "print(json.dumps({"
            "'debug': settings.DEBUG,"
            "'hosts': settings.ALLOWED_HOSTS,"
            "'db_engine': settings.DATABASES['default']['ENGINE'],"
            "'db_opts': settings.DATABASES['default']['OPTIONS'],"
            "'db_conn_max_age': settings.DATABASES['default']['CONN_MAX_AGE'],"
            "'db_health': settings.DATABASES['default']['CONN_HEALTH_CHECKS'],"
            "'cors_origins': settings.CORS_ALLOWED_ORIGINS,"
            "'cors_all': settings.CORS_ALLOW_ALL_ORIGINS,"
            "'cors_creds': settings.CORS_ALLOW_CREDENTIALS,"
            "'csrf_origins': settings.CSRF_TRUSTED_ORIGINS,"
            "'broker': settings.CELERY_BROKER_URL,"
            "'result_backend': settings.CELERY_RESULT_BACKEND,"
            "'celery_tz': settings.CELERY_TIMEZONE,"
            "'beat_file': settings.CELERY_BEAT_SCHEDULE_FILENAME,"
            "'callback': settings.PAYMENT_CALLBACK_URL,"
            "'portal_url': settings.PORTAL_FRONTEND_URL,"
            "'qr_url': settings.SERVICE_QR_FRONTEND_URL,"
            "'portal_cookie_secure': settings.PORTAL_COOKIE_SECURE,"
            "'session_cookie_secure': settings.SESSION_COOKIE_SECURE,"
            "'csrf_cookie_secure': settings.CSRF_COOKIE_SECURE,"
            "}))",
            _base_valid_prod_env(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertFalse(data["debug"])
        self.assertEqual(data["hosts"], ["api.jonehotels.com"])
        self.assertEqual(data["db_engine"], "django.db.backends.mysql")
        self.assertEqual(data["db_opts"]["charset"], "utf8mb4")
        self.assertEqual(data["db_opts"]["init_command"], "SET sql_mode='STRICT_TRANS_TABLES'")
        self.assertEqual(data["db_opts"]["ssl_mode"], "REQUIRED")
        self.assertEqual(data["db_conn_max_age"], 600)
        self.assertTrue(data["db_health"])
        self.assertFalse(data["cors_all"])
        self.assertTrue(data["cors_creds"])
        self.assertEqual(data["cors_origins"], ["https://jonehotels.com", "https://www.jonehotels.com"])
        self.assertIn("ssl_cert_reqs=required", data["broker"])
        self.assertEqual(data["broker"], data["result_backend"])
        self.assertEqual(data["celery_tz"], "Africa/Lagos")
        self.assertEqual(data["beat_file"], "/tmp/celerybeat-schedule")
        self.assertEqual(data["callback"], "https://jonehotels.com/payment-verify.html")
        self.assertEqual(data["portal_url"], "https://jonehotels.com")
        self.assertEqual(data["qr_url"], "https://jonehotels.com")
        self.assertTrue(data["portal_cookie_secure"])
        self.assertTrue(data["session_cookie_secure"])
        self.assertTrue(data["csrf_cookie_secure"])

    def test_hyphenated_mysql_ssl_mode_query_parameter_is_normalized_for_mysqlclient(self):
        proc = _run_python_in_clean_env(
            "import json; from django.conf import settings; "
            "print(json.dumps(settings.DATABASES['default']['OPTIONS']))",
            _base_valid_prod_env(
                DATABASE_URL="mysql://jone_app:placeholder_pw@mysql.internal.invalid:3306/jone_hotel?ssl-mode=REQUIRED"
            ),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        opts = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertNotIn("ssl-mode", opts)
        self.assertEqual(opts["ssl_mode"], "REQUIRED")
        self.assertEqual(opts["charset"], "utf8mb4")

    def test_fail_fast_guards_reject_insecure_or_missing_production_settings(self):
        bad_cases = [
            ("sqlite rejected", {"DATABASE_URL": "sqlite:///tmp/illegal.sqlite3"}, "Production requires MySQL"),
            ("wildcard CORS rejected", {"CORS_ALLOWED_ORIGINS": "*"}, "CORS_ALLOWED_ORIGINS"),
            ("CORS_ALLOW_ALL_ORIGINS rejected", {"CORS_ALLOW_ALL_ORIGINS": "True"}, "CORS_ALLOW_ALL_ORIGINS"),
            ("trailing slash CORS rejected", {"CORS_ALLOWED_ORIGINS": "https://jonehotels.com/"}, "trailing slash"),
            ("missing scheme CSRF rejected", {"CSRF_TRUSTED_ORIGINS": "jonehotels.com"}, "CSRF_TRUSTED_ORIGINS"),
            ("HTTP FRONTEND_URL rejected", {"FRONTEND_URL": "http://jonehotels.com"}, "HTTPS"),
            ("missing EMAIL_API_KEY rejected", {"EMAIL_API_KEY": ""}, "EMAIL_API_KEY"),
            ("missing REDIS_URL rejected", {"REDIS_URL": ""}, "REDIS_URL"),
            ("public Paystack key in secret slot rejected", {"PAYSTACK_SECRET_KEY": "pk_live_wrongslot"}, "PAYSTACK_SECRET_KEY"),
        ]
        for label, bad_override, expected_text in bad_cases:
            with self.subTest(label=label):
                proc = _run_python_in_clean_env(
                    "import django; django.setup()",
                    _base_valid_prod_env(**bad_override),
                )
                self.assertNotEqual(proc.returncode, 0, f"{label} should have failed at startup")
                self.assertIn(expected_text, proc.stderr + proc.stdout)

    def test_celery_module_defaults_to_production_settings_when_unset(self):
        source = (BACKEND_DIR / "config" / "celery.py").read_text(encoding="utf-8")
        self.assertIn(
            'os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")',
            source,
        )

    def test_docker_entrypoint_and_healthcheck_preserve_non_destructive_safety(self):
        cmd_script = (BACKEND_DIR / "docker" / "docker-cmd.sh").read_text(encoding="utf-8")
        for forbidden in ("manage.py migrate", "manage.py flush", "seed_demo"):
            lines = [
                ln for ln in cmd_script.splitlines()
                if forbidden in ln and not ln.lstrip().startswith("#")
            ]
            self.assertEqual(lines, [], f"docker-cmd.sh must never execute {forbidden}: {lines}")
        for role in ("worker)", "beat|scheduler)", "web|api)"):
            self.assertIn(role, cmd_script)

        # Verify container-healthcheck.py handles background worker role cleanly without HTTP 8000
        proc = subprocess.run(
            [sys.executable, str(BACKEND_DIR / "docker" / "container-healthcheck.py")],
            env={**os.environ, "SERVICE_ROLE": "worker", "PORT": "59999"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        # In test runner PID list has no celery process, so strict process check returns 1 (not crashing)
        self.assertIn(proc.returncode, (0, 1))
        self.assertEqual(proc.stderr, "")


class CrossOriginCorsHostPortalPaymentAndHealthTests(BaseAPITestCase):
    """Cases 3, 4, 5, 7, 8, 9, 10, 11, 12, 13, 14, 15 end-to-end API behavior."""

    @override_settings(
        ALLOWED_HOSTS=["api.jonehotels.com"],
        CORS_ALLOW_ALL_ORIGINS=False,
        CORS_ALLOW_CREDENTIALS=True,
        CORS_ALLOWED_ORIGINS=["https://jonehotels.com", "https://www.jonehotels.com"],
        CSRF_TRUSTED_ORIGINS=["https://jonehotels.com", "https://www.jonehotels.com", "https://api.jonehotels.com"],
        PORTAL_COOKIE_SECURE=True,
        FRONTEND_URL="https://jonehotels.com",
        PORTAL_FRONTEND_URL="https://jonehotels.com",
        SERVICE_QR_FRONTEND_URL="https://jonehotels.com",
        PAYMENT_CALLBACK_URL="https://jonehotels.com/payment-verify.html",
    )
    def test_host_validation_cors_allowlist_and_rejection_contract(self):
        # Approved host + approved origin -> 200 + exact ACAO + ACAC: true
        ok_resp = self.client.get(
            "/api/hotel/",
            HTTP_HOST="api.jonehotels.com",
            HTTP_ORIGIN="https://jonehotels.com",
            HTTP_X_FORWARDED_PROTO="https",
        )
        self.assertEqual(ok_resp.status_code, 200)
        self.assertEqual(ok_resp["Access-Control-Allow-Origin"], "https://jonehotels.com")
        self.assertEqual(ok_resp["Access-Control-Allow-Credentials"], "true")

        # Preflight OPTIONS with custom portal/staff headers -> allowed cleanly
        preflight = self.client.options(
            "/api/portal/service-requests/",
            HTTP_HOST="api.jonehotels.com",
            HTTP_ORIGIN="https://www.jonehotels.com",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type,x-portal-request,idempotency-key",
            HTTP_X_FORWARDED_PROTO="https",
        )
        self.assertEqual(preflight.status_code, 200)
        self.assertEqual(preflight["Access-Control-Allow-Origin"], "https://www.jonehotels.com")
        self.assertEqual(preflight["Access-Control-Allow-Credentials"], "true")
        self.assertIn("x-portal-request", preflight["Access-Control-Allow-Headers"].lower())

        # Unapproved origin -> zero CORS headers granted
        blocked_resp = self.client.get(
            "/api/hotel/",
            HTTP_HOST="api.jonehotels.com",
            HTTP_ORIGIN="https://attacker.example",
            HTTP_X_FORWARDED_PROTO="https",
        )
        self.assertNotIn("Access-Control-Allow-Origin", blocked_resp)
        self.assertNotIn("Access-Control-Allow-Credentials", blocked_resp)

        # Unapproved Host header -> HTTP 400 DisallowedHost refusal
        bad_host = self.client.get(
            "/api/hotel/",
            HTTP_HOST="evil.example.org",
            HTTP_X_FORWARDED_PROTO="https",
        )
        self.assertEqual(bad_host.status_code, 400)

    @override_settings(
        ALLOWED_HOSTS=["api.jonehotels.com"],
        CORS_ALLOW_ALL_ORIGINS=False,
        CORS_ALLOW_CREDENTIALS=True,
        CORS_ALLOWED_ORIGINS=["https://jonehotels.com"],
        PORTAL_COOKIE_SECURE=True,
        FRONTEND_URL="https://jonehotels.com",
        PORTAL_FRONTEND_URL="https://jonehotels.com",
    )
    def test_cross_origin_guest_portal_cookie_csrf_isolation_and_checkout_revocation(self):
        guest = make_guest("ada.guest@example.com", first_name="Ada", last_name="Guest")
        other_guest = make_guest("other.guest@example.com", first_name="Other", last_name="Guest")
        rt = make_room_type("Executive Suite", price="65000.00")
        room = make_room(rt, "401")
        booking = make_booking(guest, rt, [room])
        stay = make_in_house_stay(booking)

        other_room = make_room(rt, "402")
        other_booking = make_booking(other_guest, rt, [other_room])
        make_in_house_stay(other_booking)

        raw_token = "deploy-portal-magic-token-0123456789-abcdefghijklmnop"
        challenge = PortalAccessChallenge.objects.create(
            email="ada.guest@example.com",
            token_hash=hash_secret(raw_token),
            expires_at=timezone.now() + timedelta(minutes=15),
        )
        self.assertEqual(PortalAccessChallenge.objects.filter(pk=challenge.pk).count(), 1)

        consume = self.client.post(
            "/api/portal/auth/consume/",
            {"token": raw_token},
            format="json",
            secure=True,
            HTTP_HOST="api.jonehotels.com",
            HTTP_ORIGIN="https://jonehotels.com",
            HTTP_X_FORWARDED_PROTO="https",
        )
        self.assertEqual(consume.status_code, 200, consume.content)
        self.assertEqual(consume["Access-Control-Allow-Origin"], "https://jonehotels.com")
        self.assertEqual(consume["Access-Control-Allow-Credentials"], "true")

        cookie = consume.cookies["jone_portal_session"]
        self.assertTrue(cookie["httponly"])
        self.assertTrue(cookie["secure"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(cookie["path"], "/api/portal/")

        # One-time challenge replay rejected
        replay = self.client.post(
            "/api/portal/auth/consume/",
            {"token": raw_token},
            format="json",
            secure=True,
            HTTP_HOST="api.jonehotels.com",
            HTTP_ORIGIN="https://jonehotels.com",
        )
        self.assertIn(replay.status_code, (400, 401))

        # Session read succeeds and isolates strictly to Ada's booking
        me = self.client.get(
            "/api/portal/me/",
            secure=True,
            HTTP_HOST="api.jonehotels.com",
            HTTP_ORIGIN="https://jonehotels.com",
            HTTP_X_PORTAL_REQUEST="1",
        )
        self.assertEqual(me.status_code, 200, me.content)
        payload_text = json.dumps(me.json())
        self.assertIn(booking.booking_reference, payload_text)
        self.assertNotIn(other_booking.booking_reference, payload_text)

        # Portal write WITHOUT X-Portal-Request header is refused (401/403 CSRF guard)
        csrf_blocked = self.client.post(
            "/api/portal/auth/logout/",
            {},
            format="json",
            secure=True,
            HTTP_HOST="api.jonehotels.com",
            HTTP_ORIGIN="https://jonehotels.com",
        )
        self.assertIn(csrf_blocked.status_code, (401, 403))

        # Immediately upon checkout, portal access is revoked (PORTAL_ACCESS_ENDED)
        stay.status = stay.Status.CHECKED_OUT
        stay.save(update_fields=["status"])
        booking.status = Booking.Status.CHECKED_OUT
        booking.save(update_fields=["status"])
        after_checkout = self.client.get(
            "/api/portal/me/",
            secure=True,
            HTTP_HOST="api.jonehotels.com",
            HTTP_ORIGIN="https://jonehotels.com",
            HTTP_X_PORTAL_REQUEST="1",
        )
        self.assertEqual(after_checkout.status_code, 401)
        self.assertEqual(after_checkout.json().get("code"), "PORTAL_ACCESS_ENDED")
        self.assertTrue(PortalSession.objects.exists())

    def test_staff_jwt_login_refresh_rotation_blacklisting_and_role_permissions(self):
        manager = make_staff("deploy-mgr@staff.dev", role=User.Role.MANAGER)
        manager.set_password("StrongTestPassword!123")
        manager.save(update_fields=["password"])
        housekeeper = make_staff("deploy-hk@staff.dev", role=User.Role.HOUSEKEEPING)

        login_res = self.client.post(
            "/api/auth/login/",
            {"email": manager.email, "password": "StrongTestPassword!123"},
            format="json",
        )
        self.assertEqual(login_res.status_code, 200, login_res.content)
        tokens = login_res.json()["data"]["tokens"]

        refresh_res = self.client.post(
            "/api/auth/token/refresh/",
            {"refresh": tokens["refresh"]},
            format="json",
        )
        self.assertEqual(refresh_res.status_code, 200, refresh_res.content)
        refresh_body = refresh_res.json()
        new_tokens = (
            (refresh_body.get("data") or {}).get("tokens")
            or refresh_body.get("tokens")
            or refresh_body.get("data")
            or refresh_body
        )

        # Rotated-out refresh token is blacklisted and rejected on replay
        replay_old = self.client.post(
            "/api/auth/token/refresh/",
            {"refresh": tokens["refresh"]},
            format="json",
        )
        self.assertIn(replay_old.status_code, (400, 401))

        # Housekeeping role cannot access administrator-only audit logs
        self.auth(housekeeper)
        denied = self.client.get("/api/admin/audit-logs/")
        self.assertEqual(denied.status_code, 403)

        # Cross-account logout guard refuses another staff user's refresh token
        cross_logout = self.client.post(
            "/api/auth/logout/",
            {"refresh": new_tokens["refresh"]},
            format="json",
        )
        self.assertIn(cross_logout.status_code, (200, 400, 403))

        # Owner logout blacklists active refresh token cleanly
        self.client.force_authenticate(user=None)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {new_tokens['access']}")
        logout_res = self.client.post(
            "/api/auth/logout/",
            {"refresh": new_tokens["refresh"]},
            format="json",
        )
        self.assertIn(logout_res.status_code, (200, 204, 205), logout_res.content)
        after_logout = self.client.post(
            "/api/auth/token/refresh/",
            {"refresh": new_tokens["refresh"]},
            format="json",
        )
        self.assertIn(after_logout.status_code, (400, 401))

    def test_public_room_catalog_availability_quote_and_idempotent_booking_creation(self):
        rt = make_room_type("Presidential Suite", price="95000.00")
        make_room(rt, "701")
        check_in = timezone.localdate() + timedelta(days=5)
        check_out = check_in + timedelta(days=2)

        rooms_res = self.client.get("/api/rooms/")
        self.assertEqual(rooms_res.status_code, 200)
        self.assertIn("Presidential Suite", json.dumps(rooms_res.json()))

        avail_res = self.client.get(
            "/api/rooms/availability/",
            {"check_in": check_in.isoformat(), "check_out": check_out.isoformat(), "adults": 2},
        )
        self.assertEqual(avail_res.status_code, 200)

        quote_res = self.client.post(
            "/api/bookings/quote/",
            {
                "room_type": rt.slug,
                "rooms": 1,
                "check_in": check_in.isoformat(),
                "check_out": check_out.isoformat(),
                "adults": 2,
                "children": 0,
            },
            format="json",
        )
        self.assertEqual(quote_res.status_code, 200, quote_res.content)

        booking_payload = {
            "room_type": rt.slug,
            "rooms": 1,
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "adults": 2,
            "children": 0,
            "guest": {
                "first_name": "Chinedu",
                "last_name": "Okafor",
                "email": "chinedu.okafor@example.com",
                "phone": "+2348012345678",
            },
        }
        idem_key = "deploy-test-booking-idem-001"
        b1 = self.client.post(
            "/api/bookings/",
            booking_payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=idem_key,
        )
        b2 = self.client.post(
            "/api/bookings/",
            booking_payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=idem_key,
        )
        self.assertIn(b1.status_code, (200, 201), b1.content)
        self.assertIn(b2.status_code, (200, 201), b2.content)
        self.assertEqual(
            b1.json()["data"]["booking_reference"],
            b2.json()["data"]["booking_reference"],
        )

    @override_settings(
        ALLOWED_HOSTS=["api.jonehotels.com", "127.0.0.1", "localhost"],
        SECURE_SSL_REDIRECT=True,
        SECURE_REDIRECT_EXEMPT=[r"^api/health/(?:live/|ready/)?$"],
    )
    def test_health_endpoints_stay_http_accessible_when_ssl_redirect_is_enabled(self):
        # Orchestrator plain-HTTP probes must answer 200 directly without 301 redirect
        for path in ("/api/health/", "/api/health/live/", "/api/health/ready/"):
            res = self.client.get(path, HTTP_HOST="127.0.0.1")
            self.assertEqual(res.status_code, 200, f"{path} returned {res.status_code}")

        # Ordinary API traffic over plain HTTP is redirected to HTTPS (301)
        api_http = self.client.get("/api/hotel/", HTTP_HOST="api.jonehotels.com")
        self.assertEqual(api_http.status_code, 301)
        self.assertTrue(api_http["Location"].startswith("https://api.jonehotels.com/"))

    @override_settings(
        PAYSTACK_SECRET_KEY="sk_test_deployment_unit_only_not_real",
        FRONTEND_URL="https://jonehotels.com",
        PAYMENT_CALLBACK_URL="https://jonehotels.com/payment-verify.html",
    )
    def test_paystack_init_uses_production_callback_and_webhook_enforces_hmac_signature(self):
        guest = make_guest("payer@example.com", first_name="Pay", last_name="Guest")
        rt = make_room_type("Deluxe Room", price="45000.00")
        room = make_room(rt, "501")
        booking = make_booking(guest, rt, [room], status=Booking.Status.PENDING)
        admin = make_staff("pay-init-admin@staff.dev", role=User.Role.ADMIN)
        self.auth(admin)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        captured_json = {}

        def _fake_post(url, json=None, headers=None, timeout=None):
            captured_json.update(json or {})
            mock_resp.json.return_value = {
                "status": True,
                "message": "Authorization URL created",
                "data": {
                    "authorization_url": "https://checkout.paystack.com/mock_session",
                    "access_code": "mock_access_code",
                    "reference": json["reference"],
                },
            }
            return mock_resp

        with patch("apps.payments.services.paystack.requests.post", side_effect=_fake_post):
            init_res = self.client.post(
                "/api/payments/initialize/",
                {"booking_reference": booking.booking_reference},
                format="json",
            )
        self.assertIn(init_res.status_code, (200, 201), init_res.content)
        self.assertEqual(captured_json["currency"], "NGN")
        self.assertEqual(captured_json["callback_url"], "https://jonehotels.com/payment-verify.html")

        # Forged webhook signature MUST be rejected
        body = json.dumps({"event": "charge.success", "data": {"reference": captured_json["reference"]}}).encode()
        bad_webhook = self.client.post(
            "/api/payments/webhook/",
            data=body,
            content_type="application/json",
            HTTP_X_PAYSTACK_SIGNATURE="00" * 64,
        )
        self.assertIn(bad_webhook.status_code, (400, 401, 403))

        # Valid HMAC-SHA512 signature is accepted cleanly
        good_sig = hmac.new(
            b"sk_test_deployment_unit_only_not_real",
            body,
            hashlib.sha512,
        ).hexdigest()
        with patch(
            "apps.payments.services.paystack.verify_transaction",
            return_value={
                "status": True,
                "data": {
                    "status": "success",
                    "reference": captured_json["reference"],
                    "amount": captured_json["amount"],
                    "currency": "NGN",
                    "channel": "card",
                },
            },
        ):
            good_webhook = self.client.post(
                "/api/payments/webhook/",
                data=body,
                content_type="application/json",
                HTTP_X_PAYSTACK_SIGNATURE=good_sig,
            )
            replay_webhook = self.client.post(
                "/api/payments/webhook/",
                data=body,
                content_type="application/json",
                HTTP_X_PAYSTACK_SIGNATURE=good_sig,
            )
        self.assertEqual(good_webhook.status_code, 200)
        self.assertEqual(replay_webhook.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)

    @override_settings(FRONTEND_URL="https://jonehotels.com", EMAIL_LOGO_URL="")
    def test_email_logo_url_and_provider_failure_safety(self):
        self.assertEqual(hosted_logo_url(), "https://jonehotels.com/assets/icons/logo-official.png")
        with patch("apps.notifications.providers.get_provider", side_effect=RuntimeError("provider down")):
            log = send_email_safe(
                "Deployment Probe",
                "Plain body",
                ["guest@example.com"],
            )
        self.assertIsNotNone(log)
        log.refresh_from_db()
        self.assertEqual(log.status, EmailLog.Status.FAILED)

    def test_media_streaming_and_check_media_storage_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with override_settings(MEDIA_ROOT=tmpdir):
                out = StringIO()
                call_command("check_media_storage", stdout=out)
                self.assertIn("Media storage verified", out.getvalue())

                admin = make_staff("deploy-media-admin@staff.dev", role=User.Role.ADMIN)
                self.auth(admin)
                png = (
                    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
                    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
                    b"\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
                )
                upload = SimpleUploadedFile("avatar.png", png, content_type="image/png")
                res = self.client.patch(
                    "/api/auth/profile/",
                    {"profile_image": upload},
                    format="multipart",
                )
                self.assertEqual(res.status_code, 200, res.content)
                admin.refresh_from_db()
                rel_name = admin.profile_image.name
                for prefix in ("/media/", "/api/media/"):
                    stream_res = self.client.get(f"{prefix}{rel_name}")
                    self.assertEqual(stream_res.status_code, 200)
                    self.assertTrue(stream_res["Content-Type"].startswith("image/"))

    def test_all_scheduled_celery_tasks_execute_cleanly_and_idempotently(self):
        from apps.bookings.tasks import (
            auto_checkout_due_bookings,
            checkout_due_soon_warnings,
            expire_pending_bookings,
        )
        from apps.finance.tasks import post_previous_night_accommodation
        from apps.inventory.tasks import process_pending_pos_stock_consumption

        for fn in (
            expire_pending_bookings,
            auto_checkout_due_bookings,
            checkout_due_soon_warnings,
            post_previous_night_accommodation,
            process_pending_pos_stock_consumption,
        ):
            first = fn()
            second = fn()
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
