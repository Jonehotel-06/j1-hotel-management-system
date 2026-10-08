# tests/test_email_providers.py
# backend/tests/test_email_providers.py
"""The email layer is provider-neutral and honest about what was delivered.

Callers never name a vendor: they call ``send_email_safe`` and the transport is
resolved from settings. These tests pin the selection rules, the per-provider
request shape, the inline-image capability that decides how the logo is
referenced, and the rule that an EmailLog only reaches SENT when the provider
actually accepted the message.
"""
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings

from apps.core import email_assets
from apps.core.emails import send_email_safe
from apps.notifications import providers
from apps.notifications.email_models import EmailLog

FROM = "J-ONE Hotel & Lodge <no-reply@j1.test>"
KEY = "test-key-not-a-real-secret"


class FakeResponse:
    def __init__(self, status_code=201, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload


def fake_post(status_code=201, payload=None, headers=None):
    return mock.patch("requests.post",
                      return_value=FakeResponse(status_code, payload, headers=headers))


class ProviderSelectionTests(TestCase):
    """EMAIL_PROVIDER is the switch; nothing else picks a vendor."""

    def assert_provider(self, expected, **settings_kwargs):
        with override_settings(**settings_kwargs):
            self.assertEqual(providers.active_provider_name(), expected)
            self.assertEqual(providers.get_provider().name, expected)

    def test_each_api_provider_can_be_selected_by_name(self):
        for name in ("brevo", "sendgrid", "mailgun", "postmark", "resend"):
            with self.subTest(provider=name):
                self.assert_provider(name, EMAIL_PROVIDER=name, EMAIL_API_KEY=KEY)

    def test_every_smtp_alias_resolves_to_the_django_backend(self):
        for alias in providers.SMTP_ALIASES:
            with self.subTest(alias=alias):
                self.assert_provider("django", EMAIL_PROVIDER=alias, EMAIL_API_KEY="")

    def test_unknown_provider_name_falls_back_to_the_django_backend(self):
        self.assert_provider("django", EMAIL_PROVIDER="pigeon", EMAIL_API_KEY="")

    def test_explicit_smtp_wins_over_a_configured_api_key(self):
        self.assert_provider("django", EMAIL_PROVIDER="smtp", EMAIL_API_KEY=KEY)

    def test_no_provider_and_no_key_uses_the_django_backend(self):
        self.assert_provider("django", EMAIL_PROVIDER="", EMAIL_API_KEY="",
                             BREVO_API_KEY="")


class SenderIdentityTests(TestCase):
    @override_settings(DEFAULT_FROM_EMAIL=FROM)
    def test_display_name_and_address_are_parsed(self):
        name, email = providers.sender_identity()
        self.assertEqual(name, "J-ONE Hotel & Lodge")
        self.assertEqual(email, "no-reply@j1.test")

    @override_settings(DEFAULT_FROM_EMAIL='"J-ONE <no-reply@j1.test>"')
    def test_quote_wrapped_env_value_is_tolerated(self):
        """Hosting dashboards keep the quotes a user typed around the value."""
        self.assertEqual(providers.sender_identity()[1], "no-reply@j1.test")

    @override_settings(DEFAULT_FROM_EMAIL="not-an-address")
    def test_unusable_sender_fails_loudly_naming_the_setting(self):
        with self.assertRaises(providers.EmailConfigurationError) as ctx:
            providers.sender_identity()
        self.assertIn("DEFAULT_FROM_EMAIL", str(ctx.exception))


@override_settings(DEFAULT_FROM_EMAIL=FROM, EMAIL_API_KEY=KEY)
class ApiProviderRequestTests(TestCase):
    """Each adapter posts the shape its vendor documents."""

    def message(self, **kwargs):
        defaults = dict(subject="Receipt", text_body="Plain", html_body="<p>Rich</p>",
                        to_email="guest@example.com")
        defaults.update(kwargs)
        return providers.OutgoingEmail(**defaults)

    def test_brevo_posts_sender_recipient_and_both_bodies(self):
        with fake_post(201, {"messageId": "brevo-1"}) as post:
            self.assertEqual(providers.BrevoProvider().send(self.message()), "brevo-1")
        _, kwargs = post.call_args
        body = kwargs["json"]
        self.assertEqual(body["sender"]["email"], "no-reply@j1.test")
        self.assertEqual(body["to"], [{"email": "guest@example.com"}])
        self.assertEqual(body["textContent"], "Plain")
        self.assertEqual(body["htmlContent"], "<p>Rich</p>")
        self.assertEqual(kwargs["headers"]["api-key"], KEY)

    def test_sendgrid_posts_personalizations_with_a_bearer_token(self):
        with fake_post(202, {}, headers={"X-Message-Id": "sg-1"}) as post:
            providers.SendGridProvider().send(self.message())
        _, kwargs = post.call_args
        self.assertEqual(kwargs["json"]["personalizations"][0]["to"],
                         [{"email": "guest@example.com"}])
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {KEY}")

    @override_settings(EMAIL_API_DOMAIN="mg.j1.test")
    def test_mailgun_posts_to_the_configured_domain(self):
        with fake_post(200, {"id": "<mg-1>"}) as post:
            providers.MailgunProvider().send(self.message())
        args, kwargs = post.call_args
        self.assertIn("mg.j1.test", args[0])
        self.assertEqual(kwargs["auth"], ("api", KEY))

    def test_postmark_uses_its_own_token_header(self):
        with fake_post(200, {"MessageID": "pm-1"}) as post:
            providers.PostmarkProvider().send(self.message())
        _, kwargs = post.call_args
        self.assertEqual(kwargs["headers"]["X-Postmark-Server-Token"], KEY)

    def test_resend_posts_from_to_and_html(self):
        with fake_post(200, {"id": "re-1"}) as post:
            providers.ResendProvider().send(self.message())
        _, kwargs = post.call_args
        self.assertEqual(kwargs["json"]["to"], ["guest@example.com"])
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {KEY}")

    def test_plaintext_only_message_carries_no_html_field(self):
        with fake_post(201, {"messageId": "x"}) as post:
            providers.BrevoProvider().send(self.message(html_body=""))
        self.assertNotIn("htmlContent", post.call_args.kwargs["json"])

    def test_pdf_attachment_is_base64_encoded(self):
        message = self.message(attachments=[("receipt.pdf", b"%PDF-1.4", "application/pdf")])
        with fake_post(201, {"messageId": "x"}) as post:
            providers.BrevoProvider().send(message)
        attachment = post.call_args.kwargs["json"]["attachment"][0]
        self.assertEqual(attachment["name"], "receipt.pdf")
        self.assertEqual(attachment["content"], "JVBERi0xLjQ=")

    def test_a_missing_api_key_is_a_configuration_error_not_a_send(self):
        with override_settings(EMAIL_API_KEY="", BREVO_API_KEY=""):
            with self.assertRaises(providers.EmailConfigurationError):
                providers.BrevoProvider().send(self.message())


@override_settings(DEFAULT_FROM_EMAIL=FROM, EMAIL_API_KEY=KEY)
class ProviderErrorTranslationTests(TestCase):
    """Rejections surface a usable reason and never leak the credential."""

    def message(self):
        return providers.OutgoingEmail(subject="s", text_body="t", html_body="",
                                       to_email="guest@example.com")

    def test_http_error_keeps_status_and_reason(self):
        with fake_post(401, {"message": "Key not found"}):
            with self.assertRaises(providers.EmailProviderError) as ctx:
                providers.BrevoProvider().send(self.message())
        self.assertEqual(ctx.exception.http_status, 401)
        self.assertIn("Key not found", ctx.exception.safe_message)
        self.assertNotIn(KEY, str(ctx.exception))

    def test_acceptance_without_a_message_id_is_not_reported_as_sent(self):
        with fake_post(201, {}):
            with self.assertRaises(providers.EmailProviderError):
                providers.BrevoProvider().send(self.message())

    def test_timeout_and_connection_failures_are_translated(self):
        import requests

        for exc, expected in ((requests.exceptions.Timeout(), TimeoutError),
                              (requests.exceptions.ConnectionError(), ConnectionError)):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch("requests.post", side_effect=exc):
                    with self.assertRaises(expected):
                        providers.BrevoProvider().send(self.message())


class LogoReferenceTests(TestCase):
    """How the logo is referenced follows the provider's real capability."""

    def test_inline_capability_matches_each_transport(self):
        expected = {"django": True, "sendgrid": True, "mailgun": True,
                    "postmark": True, "brevo": False, "resend": False}
        for name, supported in expected.items():
            with self.subTest(provider=name):
                self.assertEqual(providers.get_provider(name).supports_inline_images,
                                 supported)

    def test_inline_capable_providers_get_a_cid_reference(self):
        for name in ("django", "sendgrid", "mailgun", "postmark"):
            with self.subTest(provider=name):
                self.assertEqual(email_assets.logo_source(providers.get_provider(name)),
                                 f"cid:{email_assets.LOGO_CID}")

    @override_settings(FRONTEND_URL="https://jone.test")
    def test_api_only_providers_get_a_hosted_https_url(self):
        """Brevo's API drops CID parts and Gmail strips data: URIs."""
        for name in ("brevo", "resend"):
            with self.subTest(provider=name):
                src = email_assets.logo_source(providers.get_provider(name))
                self.assertTrue(src.startswith("https://"))
                self.assertNotIn("cid:", src)
                self.assertNotIn("data:", src)

    @override_settings(EMAIL_LOGO_URL="https://cdn.example.com/logo.png")
    def test_hosted_logo_url_is_overridable(self):
        self.assertEqual(email_assets.hosted_logo_url(),
                         "https://cdn.example.com/logo.png")

    def test_logo_dimensions_preserve_the_real_aspect_ratio(self):
        """The mark is 507x900 portrait; a square pair would distort it."""
        ratio = email_assets.LOGO_WIDTH / email_assets.LOGO_HEIGHT
        self.assertAlmostEqual(ratio, 507 / 900, places=2)

    def test_inline_attachment_is_only_produced_for_cid_html(self):
        cid_html = f'<img src="cid:{email_assets.LOGO_CID}">'
        self.assertEqual(len(email_assets.inline_logo_attachments(cid_html)), 1)
        self.assertEqual(email_assets.inline_logo_attachments('<img src="https://x/l.png">'), [])


class DeliveryTruthTests(TestCase):
    """EmailLog reflects what the provider actually did."""

    @override_settings(EMAIL_PROVIDER="smtp")
    def test_accepted_message_is_sent(self):
        log = send_email_safe("Subject", "Body", ["guest@example.com"], kind="GENERIC")
        self.assertEqual(log.status, EmailLog.Status.SENT)
        self.assertIsNotNone(log.sent_at)
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(EMAIL_PROVIDER="brevo", EMAIL_API_KEY=KEY, DEFAULT_FROM_EMAIL=FROM)
    def test_rejected_message_is_failed_with_a_credential_free_reason(self):
        with fake_post(422, {"message": "Sender not verified"}):
            log = send_email_safe("Subject", "Body", ["guest@example.com"])
        self.assertEqual(log.status, EmailLog.Status.FAILED)
        self.assertEqual(log.failure_stage, EmailLog.FailureStage.PROVIDER)
        self.assertIn("Sender not verified", log.error_message)
        self.assertNotIn(KEY, log.error_message)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(EMAIL_PROVIDER="brevo", EMAIL_API_KEY=KEY, DEFAULT_FROM_EMAIL=FROM)
    def test_api_delivery_records_the_provider_message_id(self):
        with fake_post(201, {"messageId": "<brevo-42@smtp>"}):
            log = send_email_safe("Subject", "Body", ["guest@example.com"])
        self.assertEqual(log.status, EmailLog.Status.SENT)
        self.assertEqual(log.provider_message_id, "<brevo-42@smtp>")

    def test_only_four_statuses_exist(self):
        """Queue-era states are gone: no QUEUED, no RETRYING."""
        self.assertEqual(set(EmailLog.Status.values),
                         {"PENDING", "SENDING", "SENT", "FAILED"})

    def test_no_queue_era_fields_remain_on_the_model(self):
        names = {field.name for field in EmailLog._meta.get_fields()}
        self.assertTrue(names.isdisjoint(
            {"task_id", "queued_at", "retry_count", "max_retries"}))
