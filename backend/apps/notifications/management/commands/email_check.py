# apps/notifications/management/commands/email_check.py
"""Provider-neutral transactional email diagnostic.

Verifies the email configuration for WHICHEVER provider is active (SMTP or
any of the HTTPS API vendors) and optionally sends one real message:

    python manage.py email_check                      # configuration only
    python manage.py email_check --send you@mail.com  # + one real delivery

SECURITY: API keys and SMTP passwords are never printed. Only whether a
credential is present is reported.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.core.email_assets import logo_source
from apps.notifications.providers import (
    API_PROVIDERS,
    EmailConfigurationError,
    EmailProviderError,
    OutgoingEmail,
    active_provider_name,
    get_provider,
    sender_identity,
)


class Command(BaseCommand):
    help = "Check the active email provider's configuration and optionally send a test message."

    def add_arguments(self, parser):
        parser.add_argument(
            "--send", default="", metavar="EMAIL",
            help="Also deliver one real test message to this address.",
        )

    def handle(self, *args, **options):
        provider_name = active_provider_name()
        provider = get_provider(provider_name)

        self.stdout.write(self.style.MIGRATE_HEADING("Transactional email check"))
        self.stdout.write(f"  active provider: {provider_name}")
        self.stdout.write(f"  transport: {'HTTPS API' if provider_name in API_PROVIDERS else 'SMTP / Django backend'}")
        self.stdout.write(f"  inline (cid:) images supported: {provider.supports_inline_images}")

        try:
            name, email = sender_identity()
        except EmailConfigurationError as exc:
            raise CommandError(f"Sender: FAIL — {exc}")
        self.stdout.write(f"  sender: {f'{name} <{email}>' if name else email}")

        # The logo reference must resolve for this provider, or every branded
        # email ships with a broken image.
        source = logo_source(provider)
        if not source:
            self.stdout.write(self.style.WARNING(
                "  logo: NO SOURCE — this provider cannot inline images and neither "
                "EMAIL_LOGO_URL nor FRONTEND_URL is set, so emails will render without a logo."
            ))
        else:
            self.stdout.write(f"  logo reference: {source}")

        recipient = (options["send"] or "").strip()
        if not recipient:
            self.stdout.write(self.style.SUCCESS("EMAIL CHECK: configuration OK (no message sent)."))
            return

        message = OutgoingEmail(
            subject="J-ONE HOTEL & LODGE — email delivery test",
            text_body=(
                "This is a test of the J-ONE HOTEL & LODGE transactional email "
                f"transport ({provider_name}). If you received this, delivery works."
            ),
            html_body="",
            to_email=recipient,
        )
        try:
            message_id = provider.send(message)
        except (EmailProviderError, EmailConfigurationError) as exc:
            raise CommandError(f"Test delivery: FAIL — {exc}")
        except Exception as exc:  # transport-level failure (DNS, TLS, socket)
            raise CommandError(f"Test delivery: FAIL — {exc.__class__.__name__}: {exc}")

        self.stdout.write("  test delivery: PASS")
        self.stdout.write(f"  provider message id: {message_id or '(not returned)'}")
        self.stdout.write(self.style.SUCCESS("EMAIL CHECK: PASS"))
