# apps/core/management/commands/check_payment_config.py
"""Report whether this deployment can actually take money online.

The failure this exists for: a guest completes a booking, presses Pay, and gets
HTTP 503 `PAYMENT_NOT_CONFIGURED` while every other part of the site works. The
cause is nearly always one deployment variable, and the only place it used to be
visible was a server log line after a guest had already failed.

    python manage.py check_payment_config            # report only
    python manage.py check_payment_config --verify    # also ask Paystack

Nothing secret is printed. The key is never shown, only whether one is present
and whether it is a live or test key. `--verify` makes one authenticated call to
Paystack's balance endpoint so a key that is present but rejected is caught
before a guest finds it.
"""
import json
import urllib.error
import urllib.request
from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.payments.services.paystack import (
    KEY_MODE_LIVE,
    KEY_MODE_TEST,
    secret_key_status,
)

BALANCE_URL = "https://api.paystack.co/balance"
TIMEOUT_SECONDS = 15


class Command(BaseCommand):
    help = "Report whether online card payments are configured, and optionally verify the key with Paystack."

    def add_arguments(self, parser):
        parser.add_argument(
            "--verify", action="store_true",
            help="Make one authenticated call to Paystack to confirm the key is accepted.",
        )

    def handle(self, *args, **options):
        configured, mode = secret_key_status()
        callback = str(getattr(settings, "PAYMENT_CALLBACK_URL", "") or "")

        self.stdout.write(self.style.MIGRATE_HEADING("Online payment configuration"))
        self.stdout.write(f"  settings module      : {settings.SETTINGS_MODULE}")
        self.stdout.write(f"  DEBUG                : {settings.DEBUG}")

        if not configured:
            # Say WHICH kind of missing this is: an empty value and a public key
            # in the secret slot both produce 503 for every guest, but they are
            # fixed by setting different things.
            raw = str(getattr(settings, "PAYSTACK_SECRET_KEY", "") or "").strip()
            if raw.startswith(("pk_", "pk_test_")):
                reason = (
                    "PAYSTACK_SECRET_KEY holds a PUBLIC key (pk_…). Server-side "
                    "charges need the SECRET key (sk_… / sk_test_…)."
                )
            elif raw:
                reason = (
                    "PAYSTACK_SECRET_KEY is set but is not a Paystack secret key "
                    "(expected sk_live_… or sk_test_…)."
                )
            else:
                reason = "PAYSTACK_SECRET_KEY is empty or not set for this service."
            self.stdout.write("  secret key           : MISSING")
            self.stdout.write(self.style.ERROR(f"  online payments      : OFF — every guest payment returns 503"))
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(reason))
            self.stdout.write(
                "Set it on the backend host (Railway: the API service → Variables), "
                "then redeploy. The key is never sent to the browser."
            )
        else:
            label = "LIVE (real cards are charged)" if mode == KEY_MODE_LIVE else "TEST (no real money moves)"
            self.stdout.write(f"  secret key           : set — {label}")
            self.stdout.write(self.style.SUCCESS("  online payments      : ON"))
            if mode == KEY_MODE_TEST:
                self.stdout.write(self.style.WARNING(
                    "  A test key is in use: checkouts succeed but no money is taken. "
                    "Use sk_live_… when the hotel is ready to accept real payments."
                ))

        parsed = urlparse(callback)
        callback_ok = parsed.scheme == "https" and bool(parsed.netloc)
        self.stdout.write(f"  callback URL         : {callback or '(empty)'}")
        if not callback_ok:
            self.stdout.write(self.style.WARNING(
                "  The payment callback URL must be a public HTTPS page; without it "
                "Paystack returns the guest to a dead end after paying."
            ))
        frontend = str(getattr(settings, "FRONTEND_URL", "") or "")
        self.stdout.write(f"  FRONTEND_URL         : {frontend or '(empty)'}")

        if options["verify"]:
            if not configured:
                self.stdout.write(self.style.WARNING("\n--verify skipped: no secret key to test."))
                return
            self.stdout.write("\nAsking Paystack whether the key is accepted…")
            status, detail = self._verify_key()
            style = self.style.SUCCESS if status == "ok" else self.style.ERROR
            self.stdout.write(style(f"  Paystack             : {detail}"))

    def _verify_key(self):
        """One authenticated GET /balance. Returns ("ok"|"failed", message)."""
        from apps.payments.services.paystack import _headers  # noqa: PLC2701 — same package

        request = urllib.request.Request(BALANCE_URL, headers=_headers(), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                body = response.read().decode("utf-8", "replace")
            payload = json.loads(body or "{}")
        except urllib.error.HTTPError as exc:
            # 401 is the answer a revoked or wrong-environment key gets.
            return "failed", f"rejected (HTTP {exc.code}) — check the key and its environment (test vs live)"
        except (urllib.error.URLError, TimeoutError, OSError):
            return "failed", "could not be reached from this host (network/DNS)"
        except ValueError:
            return "failed", "returned a response that is not JSON"
        if not payload.get("status"):
            return "failed", f"rejected: {payload.get('message') or 'no reason given'}"
        return "ok", "key accepted"
