"""Safe production diagnostic for the Brevo HTTPS email transport.

Run this from a managed-host shell (or locally) using the same environment as
the web service:

    python manage.py brevo_check                     # config + auth + sender
    python manage.py brevo_check --send you@example.com   # + one real delivery

It verifies, in order:

  1. BREVO_API_KEY is configured (the value is NEVER printed);
  2. DEFAULT_FROM_EMAIL parses to a usable sender address;
  3. api.brevo.com is reachable over HTTPS and the key authenticates
     (GET /v3/account);
  4. the configured sender address is registered/verified in the Brevo
     account (GET /v3/senders);
  5. optionally (--send), Brevo accepts one real transactional email and the
     provider message id is printed.

Every Brevo HTTP error is surfaced with its real status code and sanitized
provider message — a 401/400/403/429/5xx is never hidden behind a generic
"email failed". No secret is ever printed.
"""
import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.notifications.tasks import active_transport, sender_identity

API_BASE = "https://api.brevo.com/v3"


class Command(BaseCommand):
    help = "Verify Brevo API configuration, authentication, sender validity and (optionally) a real test delivery."

    def add_arguments(self, parser):
        parser.add_argument(
            "--send", metavar="RECIPIENT", default="",
            help="Also send one real test email to this address through Brevo.",
        )
        parser.add_argument("--timeout", type=float, default=15.0)

    def handle(self, *args, **options):
        timeout = max(3.0, min(options["timeout"], 60.0))
        ok = True

        self.stdout.write(self.style.MIGRATE_HEADING("Brevo transactional email check"))
        self.stdout.write(f"  settings module: {settings.SETTINGS_MODULE}")
        self.stdout.write(f"  active transport: {active_transport()}")

        # 1. Key present?
        api_key = (getattr(settings, "BREVO_API_KEY", "") or "").strip()
        if not api_key:
            raise CommandError(
                "BREVO_API_KEY: MISSING — set the BREVO_API_KEY environment variable "
                "(Brevo dashboard → SMTP & API → API keys)."
            )
        self.stdout.write("  BREVO_API_KEY: configured")

        # 2. Sender parses?
        try:
            sender_name, sender_email = sender_identity()
        except ValueError as exc:
            raise CommandError(f"DEFAULT_FROM_EMAIL: INVALID — {exc}")
        display = f"{sender_name} <{sender_email}>" if sender_name else sender_email
        self.stdout.write(f"  Configured sender: {display}")

        headers = {"api-key": api_key, "Accept": "application/json"}

        # 3. Connectivity + authentication.
        try:
            response = requests.get(f"{API_BASE}/account", headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            raise CommandError(
                f"BREVO API connectivity: FAIL — could not reach api.brevo.com "
                f"({exc.__class__.__name__})."
            )
        self.stdout.write("  BREVO API connectivity: PASS")
        if response.status_code == 401:
            raise CommandError(
                "BREVO authentication: FAIL — HTTP 401 Unauthorized. The API key "
                "is wrong, revoked, or not an API key (an SMTP key will not work here)."
            )
        if response.status_code != 200:
            raise CommandError(
                f"BREVO authentication: FAIL — HTTP {response.status_code}: "
                f"{self._safe_body(response)}"
            )
        self.stdout.write("  BREVO authentication: PASS")

        # 4. Sender registered/verified?
        try:
            response = requests.get(f"{API_BASE}/senders", headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            raise CommandError(
                f"Sender validation: FAIL — could not list senders ({exc.__class__.__name__})."
            )
        if response.status_code != 200:
            self.stdout.write(self.style.WARNING(
                f"  Sender validation: SKIPPED — HTTP {response.status_code}: "
                f"{self._safe_body(response)} (the key may lack the senders scope)."
            ))
        else:
            senders = (response.json() or {}).get("senders") or []
            match = next(
                (s for s in senders
                 if str(s.get("email", "")).lower() == sender_email.lower()),
                None,
            )
            if match is None:
                ok = False
                registered = ", ".join(sorted(str(s.get("email", "")) for s in senders)) or "none"
                self.stdout.write(self.style.ERROR(
                    f"  Sender validation: FAIL — '{sender_email}' is NOT a sender in "
                    f"this Brevo account. Registered senders: {registered}. Add and "
                    "verify it under Brevo → Senders, Domains & Dedicated IPs → Senders, "
                    "or set DEFAULT_FROM_EMAIL to a verified sender."
                ))
            elif not match.get("active", True):
                ok = False
                self.stdout.write(self.style.ERROR(
                    f"  Sender validation: FAIL — '{sender_email}' exists in Brevo but "
                    "is NOT verified/active yet. Complete the verification email Brevo sent."
                ))
            else:
                self.stdout.write("  Sender validation: PASS")

        # 5. Optional real delivery.
        recipient = (options["send"] or "").strip()
        if recipient:
            payload = {
                "sender": {"email": sender_email, **({"name": sender_name} if sender_name else {})},
                "to": [{"email": recipient}],
                "subject": "J-ONE HOTEL & LODGE — Brevo delivery test",
                "textContent": (
                    "This is a test of the J-ONE HOTEL & LODGE transactional email "
                    "transport (Brevo HTTPS API). If you received this, delivery works."
                ),
            }
            try:
                response = requests.post(
                    f"{API_BASE}/smtp/email", json=payload,
                    headers={**headers, "Content-Type": "application/json"},
                    timeout=timeout,
                )
            except requests.exceptions.RequestException as exc:
                raise CommandError(
                    f"Test delivery: FAIL — request error ({exc.__class__.__name__})."
                )
            if response.status_code not in (200, 201):
                raise CommandError(
                    f"Test delivery: FAIL — HTTP {response.status_code}: "
                    f"{self._safe_body(response)}"
                )
            message_id = ""
            try:
                message_id = str((response.json() or {}).get("messageId") or "")
            except ValueError:
                pass
            self.stdout.write("  Test delivery: PASS")
            self.stdout.write(f"  Provider message ID: {message_id or '(not returned)'}")

        if not ok:
            raise CommandError("BREVO EMAIL CHECK: FAIL (see errors above).")
        self.stdout.write(self.style.SUCCESS("BREVO EMAIL CHECK: PASS"))

    @staticmethod
    def _safe_body(response):
        """Sanitized provider error message (no keys/tokens are ever present
        in Brevo error bodies, but cap length regardless)."""
        try:
            body = response.json()
            return str(body.get("message") or body.get("code") or response.text[:200])[:200]
        except ValueError:
            return response.text[:200]
