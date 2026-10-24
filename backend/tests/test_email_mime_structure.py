"""The outbound MIME tree for a styled email with an inline logo and a PDF.

Django 6 removed the undocumented ``mixed_subtype`` attribute that the SMTP
provider used to set in order to nest an inline ``cid:`` image inside the HTML
part. The result was a loud, total failure: ``EmailMessage.message()`` raised
``AttributeError`` as soon as anything was attached, every receipt email became
a 502 ``EMAIL_DELIVERY_FAILED``, and guest receipts stopped going out.

These tests pin the structure mail clients actually need, so a future Django
upgrade or refactor cannot silently break delivery again:

    multipart/mixed                (only when a real attachment exists)
    └── multipart/related
        ├── multipart/alternative
        │   ├── text/plain
        │   └── text/html
        └── image/*  Content-ID: <logo>, disposition inline
"""
from django.core import mail

from apps.notifications.providers import _InlineImageMessage

from .base import BaseAPITestCase

HTML = '<html><body><img src="cid:jone-logo" alt="J-ONE"><p>Your receipt</p></body></html>'
LOGO = (b"\x89PNG\r\n\x1a\n" + b"0" * 32, "image/png")


class OutboundMimeStructureTests(BaseAPITestCase):
    def _build(self, *, attachments, inline_images):
        message = _InlineImageMessage(
            subject="J-ONE receipt",
            body="Your receipt is attached.",
            from_email="J-ONE Hotel <no-reply@jone.test>",
            to=["guest@example.com"],
            attachments=attachments,
            inline_images=inline_images,
        )
        message.attach_alternative(HTML, "text/html")
        return message.message()

    def test_inline_logo_and_receipt_attachment_build_a_related_tree(self):
        msg = self._build(
            attachments=[("receipt-J1.pdf", b"%PDF-1.4 receipt", "application/pdf")],
            inline_images=[("jone-logo", "logo.png", *LOGO)],
        )
        self.assertEqual(msg.get_content_type(), "multipart/mixed")
        parts = msg.get_payload()
        self.assertEqual(parts[0].get_content_type(), "multipart/related")
        related = parts[0].get_payload()
        self.assertEqual(related[0].get_content_type(), "multipart/alternative")
        alternatives = [part.get_content_type() for part in related[0].get_payload()]
        self.assertEqual(alternatives, ["text/plain", "text/html"])
        self.assertEqual(related[1].get_content_type(), "image/png")
        self.assertEqual(related[1]["Content-ID"], "<jone-logo>")
        self.assertEqual(related[1].get_content_disposition(), "inline")
        self.assertEqual(parts[1].get_content_disposition(), "attachment")
        self.assertEqual(parts[1].get_filename(), "receipt-J1.pdf")

    def test_inline_logo_without_an_attachment_stays_a_related_tree(self):
        msg = self._build(attachments=[], inline_images=[("jone-logo", "logo.png", *LOGO)])
        self.assertEqual(msg.get_content_type(), "multipart/related")
        self.assertEqual(msg.get_payload()[1]["Content-ID"], "<jone-logo>")

    def test_a_plain_message_without_images_or_attachments_is_unchanged(self):
        message = _InlineImageMessage(
            subject="Plain notice",
            body="Everything is fine.",
            from_email="J-ONE Hotel <no-reply@jone.test>",
            to=["guest@example.com"],
        )
        msg = message.message()
        self.assertEqual(msg.get_content_type(), "text/plain")
        self.assertEqual(msg["Subject"], "Plain notice")

    def test_an_attachment_without_inline_images_no_longer_raises(self):
        # This is the exact shape that used to raise AttributeError: an attached
        # receipt and no inline image to trigger the old branch.
        message = _InlineImageMessage(
            subject="Receipt only",
            body="See attached.",
            from_email="J-ONE Hotel <no-reply@jone.test>",
            to=["guest@example.com"],
            attachments=[("receipt.pdf", b"%PDF-1.4 x", "application/pdf")],
        )
        msg = message.message()
        self.assertEqual(msg.get_content_type(), "multipart/mixed")
        attachment = msg.get_payload()[-1]
        self.assertEqual(attachment.get_content_disposition(), "attachment")
        self.assertEqual(attachment.get_filename(), "receipt.pdf")

    def test_headers_cannot_be_injected_through_the_subject(self):
        message = _InlineImageMessage(
            subject="Receipt\r\nBcc: attacker@example.com",
            body="body",
            from_email="J-ONE Hotel <no-reply@jone.test>",
            to=["guest@example.com"],
            inline_images=[("jone-logo", "logo.png", *LOGO)],
        )
        msg = message.message()
        self.assertNotIn("attacker@example.com", str(msg.get("Bcc") or ""))
        self.assertNotIn("\n", msg["Subject"])

    def test_the_locmem_backend_accepts_the_message(self):
        # The end-to-end proof: Django's own backend must accept (and parse) it.
        outbox_before = len(mail.outbox)
        message = _InlineImageMessage(
            subject="J-ONE receipt",
            body="Your receipt is attached.",
            from_email="J-ONE Hotel <no-reply@jone.test>",
            to=["guest@example.com"],
            attachments=[("receipt-J1.pdf", b"%PDF-1.4 receipt", "application/pdf")],
            inline_images=[("jone-logo", "logo.png", *LOGO)],
        )
        message.attach_alternative(HTML, "text/html")
        self.assertEqual(message.send(fail_silently=False), 1)
        self.assertEqual(len(mail.outbox), outbox_before + 1)
        self.assertEqual(mail.outbox[-1].to, ["guest@example.com"])
