# tests/base.py
from django.core.cache import cache
from rest_framework.test import APITestCase


class BaseAPITestCase(APITestCase):
    """Every test starts with a clean cache so throttle counters and cached
    settings/hotel content never leak between tests.

    Email delivery is fully synchronous (no queue, no threads), so no special
    email test configuration is needed: ``transaction.on_commit`` callbacks
    are flushed by Django's ``captureOnCommitCallbacks`` where a test needs
    the commit-deferred booking/payment emails to run.
    """

    def setUp(self):
        cache.clear()
        super().setUp()

    def tearDown(self):
        cache.clear()
        super().tearDown()

    def auth(self, user):
        self.client.force_authenticate(user=user)

    def unauth(self):
        self.client.force_authenticate(user=None)
