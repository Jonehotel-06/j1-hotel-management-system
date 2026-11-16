"""Regression tests for the DEFAULT_FROM_EMAIL sender address.

Pinned after the Northflank Docker build failure: the default
DEFAULT_FROM_EMAIL contained an extra closing angle bracket

    "J-one hotel & lodge<support@jonehotels.com>>"

so ``email.utils.parseaddr()`` returned an empty address and production
settings raised ``ImproperlyConfigured`` at boot, breaking
``python manage.py collectstatic --noinput`` (and every other management
command). The default must parse to a real sender address.
"""
from email.utils import parseaddr

from django.test import TestCase


class DefaultSenderAddressTests(TestCase):
    """The default sender address must parse to a valid email address."""

    def test_default_sender_address_parses_to_a_valid_email(self):
        """parseaddr(DEFAULT_FROM_EMAIL) must yield a non-empty address containing "@"."""
        from django.conf import settings

        _sender_name, sender_email = parseaddr(settings.DEFAULT_FROM_EMAIL)
        self.assertTrue(sender_email, "DEFAULT_FROM_EMAIL parsed to an empty address")
        self.assertIn("@", sender_email, "DEFAULT_FROM_EMAIL must parse to an email address")

    def test_default_sender_address_has_no_double_angle_bracket(self):
        """Guard against the malformed default that broke production boot."""
        from django.conf import settings

        self.assertFalse(
            settings.DEFAULT_FROM_EMAIL.endswith(">>"),
            "DEFAULT_FROM_EMAIL default must not end with a double angle bracket",
        )

    def test_parseaddr_rejects_the_original_malformed_default(self):
        """The pre-fix literal must not be considered a valid sender address."""
        malformed = "J-one hotel & lodge<support@jonehotels.com>>"
        _sender_name, sender_email = parseaddr(malformed)
        self.assertFalse(sender_email, "the malformed default must parse to an empty address")

    def test_parseaddr_accepts_the_corrected_default(self):
        """The corrected default must parse to support@jonehotels.com."""
        corrected = "J-one hotel & lodge <support@jonehotels.com>"
        _sender_name, sender_email = parseaddr(corrected)
        self.assertEqual(sender_email, "support@jonehotels.com")
