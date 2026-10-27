"""Verify declared Django runtime can deliver HTML mail without inline images."""
from django.core import mail
from django.test import TestCase, override_settings

from apps.notifications.providers import DjangoProvider, OutgoingEmail


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class NoInlineEmailRegression(TestCase):
    def test_html_with_no_inline_image_reaches_locmem_backend(self):
        for attachments in ([], [('synthetic.pdf', b'%PDF-1.4\n', 'application/pdf')]):
            with self.subTest(has_attachment=bool(attachments)):
                before = len(mail.outbox)
                DjangoProvider().send(OutgoingEmail(
                    subject='Synthetic notice', text_body='Test only.', html_body='<p>Test only.</p>',
                    to_email='synthetic@example.invalid', attachments=attachments,
                ))
                self.assertEqual(len(mail.outbox), before + 1)
                self.assertEqual(mail.outbox[-1].message().get_content_type(),
                                 'multipart/mixed' if attachments else 'multipart/alternative')
