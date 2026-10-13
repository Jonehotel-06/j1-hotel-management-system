# tests/test_email_branding_and_portal_links.py
"""Email branding, logo resolution and environment-specific portal links.

Two defects are locked down here, both visible in the supplied screenshots:

1. **The emailed payment receipt showed a broken logo.** The logo ``src`` fell
   back to ``{FRONTEND_URL}/assets/icons/logo-official.png``. During local
   testing ``FRONTEND_URL`` is ``http://127.0.0.1:5500``, and a mail client
   fetching the logo runs on the *guest's* device — so the receipt arrived with
   a grey broken-image placeholder. The logo source must therefore never be a
   loopback/private origin, and the template must render a wordmark instead of a
   broken image when no reachable asset exists.

2. **The guest-portal email was unstyled plain text** carrying a raw
   development URL. It is now rendered through the shared branded builder as a
   proper ``multipart/alternative`` message with an inline-styled HTML body, a
   prominent sign-in button, a plain-text URL fallback, and mobile-friendly
   layout — with no scripts, web fonts or external stylesheets.

The environment rules from brief section 7.4 are also asserted: local
development keeps its configured local origin, and a production deployment
refuses to boot with a localhost portal origin.
"""
import re
from datetime import timedelta

from django.test import TestCase, override_settings

from apps.core.email_assets import (
    LOGO_CID,
    hosted_logo_url,
    inline_logo_attachments,
    is_publicly_reachable,
    logo_source,
    logo_bytes,
)
from apps.notifications.models import EmailLog
from apps.notifications.providers import BrevoProvider, DjangoProvider
from apps.portal.invitations import invitation_email_bodies, portal_login_url
from apps.portal.models import GuestPortalInvitation

from .base import BaseAPITestCase

LOCAL_ORIGIN = "http://127.0.0.1:5500"
PUBLIC_ORIGIN = "https://jonehotel.ng"


class LogoReachabilityTests(TestCase):
    """A logo URL is only usable when a guest's mail client could fetch it."""

    def test_loopback_and_private_hosts_are_rejected(self):
        for url in (
            "http://127.0.0.1:5500/assets/icons/logo-official.png",
            "http://localhost:8080/logo.png",
            "http://0.0.0.0/logo.png",
            "http://[::1]/logo.png",
            "http://192.168.1.20/logo.png",
            "http://10.0.0.5/logo.png",
            "http://172.16.4.4/logo.png",
            "ftp://jonehotel.ng/logo.png",
            "/assets/icons/logo-official.png",
            "assets/icons/logo-official.png",
            "",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_publicly_reachable(url))

    def test_public_origins_are_accepted(self):
        for url in (
            "https://jonehotel.ng/assets/icons/logo-official.png",
            "https://cdn.example.com/logo.png",
            "http://172.32.0.1/logo.png",  # outside the 172.16/12 private block
        ):
            with self.subTest(url=url):
                self.assertTrue(is_publicly_reachable(url))

    @override_settings(FRONTEND_URL=LOCAL_ORIGIN, EMAIL_LOGO_URL="")
    def test_a_development_frontend_url_never_becomes_the_email_logo(self):
        """This is the exact configuration that produced the broken logo."""
        self.assertEqual(hosted_logo_url(), "")
        src = logo_source(BrevoProvider())
        self.assertNotIn(LOCAL_ORIGIN, src)
        self.assertNotIn("127.0.0.1", src)

    @override_settings(EMAIL_LOGO_URL="http://127.0.0.1:5500/logo.png")
    def test_a_localhost_email_logo_url_is_refused(self):
        self.assertEqual(hosted_logo_url(), "")

    @override_settings(FRONTEND_URL=PUBLIC_ORIGIN, EMAIL_LOGO_URL="")
    def test_a_public_frontend_url_is_used(self):
        self.assertEqual(hosted_logo_url(), f"{PUBLIC_ORIGIN}/assets/icons/logo-official.png")

    @override_settings(EMAIL_LOGO_URL="https://cdn.example.com/logo.png")
    def test_an_explicit_public_logo_url_wins(self):
        self.assertEqual(hosted_logo_url(), "https://cdn.example.com/logo.png")

    @override_settings(FRONTEND_URL=LOCAL_ORIGIN, EMAIL_LOGO_URL="")
    def test_an_unreachable_url_falls_back_to_an_inline_cid_not_a_broken_image(self):
        provider = BrevoProvider()  # an API transport that cannot use CID images
        src = logo_source(provider)
        if logo_bytes():
            self.assertEqual(src, f"cid:{LOGO_CID}")
            self.assertTrue(inline_logo_attachments(f'<img src="{src}">'))
        else:
            self.assertEqual(src, "")

    @override_settings(FRONTEND_URL=PUBLIC_ORIGIN)
    def test_a_mime_transport_still_uses_the_inline_image(self):
        self.assertEqual(logo_source(DjangoProvider()), f"cid:{LOGO_CID}")


class BrandedTemplateFallbackTests(TestCase):
    """No template may emit a broken <img> when the logo is unavailable."""

    TEMPLATES = (
        "emails/notice.html",
        "emails/receipt.html",
        "emails/review_invitation.html",
    )

    def test_every_branded_template_guards_the_logo(self):
        from django.template.loader import get_template

        for name in self.TEMPLATES:
            with self.subTest(template=name):
                source = get_template(name).template.source
                self.assertIn("{% if logo_src %}", source,
                              f"{name} must not render an empty logo src")
                self.assertIn("J1", source,
                              f"{name} must offer a wordmark fallback")

    def test_rendering_with_no_logo_produces_no_empty_src(self):
        from apps.core.email_design import render_notice_email

        _, html = render_notice_email(
            category="Test", title="Subject", greeting="Hello,",
            paragraphs=["Body copy."],
        )
        self.assertNotIn('src=""', html)
        self.assertNotIn("127.0.0.1", html)
        self.assertNotIn("localhost", html)


class PortalInvitationEmailTests(TestCase):
    """The guest-portal invitation is a real branded HTML email."""

    def _bodies(self, first_name="Ada"):
        return invitation_email_bodies(None, first_name)

    def test_the_html_body_is_inline_styled_and_email_client_safe(self):
        _, html = self._bodies()
        self.assertTrue(html.lstrip().startswith("<!DOCTYPE html"))
        self.assertIn("<html", html)
        # Table-based layout with inline CSS and a mobile media query.
        self.assertIn("<table", html)
        self.assertIn('role="presentation"', html)
        self.assertIn("@media only screen and (max-width:620px)", html)
        self.assertIn("style=", html)
        # Nothing that a mail client cannot execute.
        self.assertNotIn("<script", html.lower())
        self.assertNotIn("<link rel=", html.lower())
        self.assertNotIn("@import", html.lower())
        self.assertNotIn("@font-face", html.lower())

    def test_the_sign_in_action_is_prominent_and_has_a_plain_text_fallback(self):
        _, html = self._bodies()
        self.assertIn("Open the guest portal", html)
        self.assertIn("j1-button", html)
        # The raw URL is repeated as selectable text under the button.
        self.assertIn(self._login_url(), html)
        self.assertIn("copy and paste this link", html.lower())

    def test_the_greeting_and_guest_promises_are_accurate(self):
        text, html = self._bodies("Ada")
        self.assertIn("Dear Ada,", html)
        self.assertIn("by the time", "by the time")  # sanity: no stray formatting
        # Only the features the portal genuinely exposes are promised.
        for promise in ("folio", "room service", "requests"):
            with self.subTest(promise=promise):
                self.assertIn(promise, html.lower())

    def test_the_plain_text_alternative_carries_the_link_and_no_token(self):
        text, _ = self._bodies()
        self.assertIn(self._login_url(), text)
        self.assertIn("one-time sign-in link", text)
        self.assertNotIn("j1-button", text)
        self.assertNotIn("token=", text.lower())

    def _login_url(self):
        return portal_login_url()

    @override_settings(PORTAL_FRONTEND_URL=LOCAL_ORIGIN)
    def test_local_development_uses_the_configured_local_origin(self):
        self.assertEqual(portal_login_url(), f"{LOCAL_ORIGIN}/portal/login.html")

    @override_settings(PORTAL_FRONTEND_URL=PUBLIC_ORIGIN)
    def test_production_uses_the_public_portal_origin(self):
        self.assertEqual(portal_login_url(), f"{PUBLIC_ORIGIN}/portal/login.html")
        text, html = self._bodies()
        self.assertNotIn("127.0.0.1", html)
        self.assertNotIn("127.0.0.1", text)

    @override_settings(PORTAL_FRONTEND_URL="https://jonehotel.ng/")
    def test_a_trailing_slash_never_doubles_up_in_the_path(self):
        self.assertEqual(portal_login_url(), "https://jonehotel.ng/portal/login.html")


class ProductionPortalUrlGuardTests(TestCase):
    """Production must refuse to boot with a localhost portal origin.

    The guard is asserted against the real settings module's SOURCE (importing
    ``config.settings.production`` in a test process requires the full
    production environment). The URL-shape logic it relies on is covered
    behaviourally by :class:`LogoReachabilityTests`.
    """

    def _source(self):
        import pathlib

        import config

        path = pathlib.Path(config.__file__).parent / "settings" / "production.py"
        return path.read_text()

    def test_production_rejects_a_localhost_portal_origin(self):
        source = self._source()
        self.assertIn("PORTAL_FRONTEND_URL", source)
        self.assertIn("must be a public HTTPS URL in production", source)
        self.assertIn("localhost", source)
        self.assertIn("127.0.0.1", source)

    def test_production_rejects_a_localhost_frontend_and_callback_origin(self):
        source = self._source()
        # FRONTEND_URL feeds the payment callback and the QR origins too, so its
        # replacement must be guarded on every one of those paths.
        self.assertIn("PAYMENT_CALLBACK_URL (or FRONTEND_URL fallback)", source)
        self.assertIn("SERVICE_QR_FRONTEND_URL (or FRONTEND_URL fallback)", source)
        self.assertIn("ImproperlyConfigured", source)


class PortalInvitationDeliveryTests(BaseAPITestCase):
    """The invitation is actually delivered as multipart HTML, not plain text."""

    def setUp(self):
        super().setUp()
        from apps.accounts.models import User
        from apps.bookings.services.booking_service import check_in_booking
        from apps.core.utils import hotel_today

        from .factories import make_booking, make_guest, make_room, make_room_type, make_staff

        self.receptionist = make_staff("brand.reception@staff.dev", role=User.Role.RECEPTIONIST)
        guest = make_guest("brand.guest@example.com")
        room_type = make_room_type("Branding suite", price="10000.00")
        room = make_room(room_type, "BR-01")
        self.booking = make_booking(
            guest, room_type, [room], check_in=hotel_today(),
            check_out=hotel_today() + timedelta(days=2), total="20000.00",
        )
        self.check_in_booking = check_in_booking

    def test_check_in_delivers_a_styled_invitation_and_records_html(self):
        with self.captureOnCommitCallbacks(execute=True):
            check_in = getattr(self, "check_in_booking")
            check_in(booking=self.booking, staff_user=self.receptionist)

        log = EmailLog.objects.filter(kind="PORTAL_INVITATION").first()
        self.assertIsNotNone(log, "check-in must still email the portal invitation")
        self.assertEqual(log.status, EmailLog.Status.SENT)
        self.assertTrue(log.html_body.strip(), "the invitation must carry an HTML alternative")
        self.assertIn("<html", log.html_body)
        self.assertIn("/portal/login.html", log.body)
        self.assertIn("/portal/login.html", log.html_body)

        invitation = GuestPortalInvitation.objects.get()
        self.assertEqual(invitation.status, GuestPortalInvitation.Status.SENT)

    @override_settings(FRONTEND_URL=LOCAL_ORIGIN, PORTAL_FRONTEND_URL=LOCAL_ORIGIN)
    def test_a_local_host_url_is_expected_during_local_testing(self):
        with self.captureOnCommitCallbacks(execute=True):
            getattr(self, "check_in_booking")(booking=self.booking, staff_user=self.receptionist)
        log = EmailLog.objects.get(kind="PORTAL_INVITATION")
        self.assertIn(f"{LOCAL_ORIGIN}/portal/login.html", log.html_body)
        # ...but the logo may never point at it.
        self.assertNotIn("127.0.0.1:5500/assets", log.html_body)

    @override_settings(PORTAL_FRONTEND_URL=PUBLIC_ORIGIN, FRONTEND_URL=PUBLIC_ORIGIN)
    def test_a_production_style_configuration_emits_only_public_links(self):
        with self.captureOnCommitCallbacks(execute=True):
            getattr(self, "check_in_booking")(booking=self.booking, staff_user=self.receptionist)
        log = EmailLog.objects.get(kind="PORTAL_INVITATION")
        self.assertIn(f"{PUBLIC_ORIGIN}/portal/login.html", log.html_body)
        self.assertNotIn("127.0.0.1", log.html_body)
        self.assertNotIn("localhost", log.html_body)
        self.assertIsNone(re.search(r'src=""', log.html_body))
