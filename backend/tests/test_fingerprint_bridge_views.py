"""The server-side bridge endpoints must never masquerade as the workstation.

Repair context (2026-11-19): the desk page used to call
``/api/admin/staff-operations/biometrics/bridge/*`` on the HOSTED API, which has
no USB reader, so the bridge reported "no reader" (or, after the URL defect, a
network error) no matter what was plugged into the reception PC. The endpoints
now answer 409 BRIDGE_NOT_COLOCATED unless the API really runs on the reader's
machine, and every authentication/capability control still runs first.
"""
from django.test import TestCase
from rest_framework.test import APIClient

from .factories import make_user

STATUS_URL = "/api/admin/staff-operations/biometrics/bridge/status/"
CAPTURE_URL = "/api/admin/staff-operations/biometrics/bridge/capture/"
INITIALIZE_URL = "/api/admin/staff-operations/biometrics/bridge/initialize/"
DEVICES_URL = "/api/admin/staff-operations/biometrics/bridge/devices/"


class BridgeColocationGuardTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_authorised_staff_get_an_honest_conflict_not_a_fake_reader_status(self):
        self.client.force_authenticate(user=make_user("bridge-admin@example.com", role="ADMIN"))
        for url, method in ((STATUS_URL, "get"), (INITIALIZE_URL, "post"),
                            (CAPTURE_URL, "post"), (DEVICES_URL, "get")):
            with self.subTest(url=url):
                response = getattr(self.client, method)(url, {}, format="json")
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.data["code"], "BRIDGE_NOT_COLOCATED")
                self.assertFalse(response.data["data"]["bridge_colocated"])
                self.assertIn("127.0.0.1", response.data["message"])
                self.assertNotIn("No fingerprint reader is available", str(response.data))

    def test_anonymous_requests_are_still_rejected_before_any_bridge_work(self):
        response = self.client.get(STATUS_URL)
        self.assertIn(response.status_code, (401, 403))
        self.assertNotEqual(getattr(response, "data", {}).get("code"), "BRIDGE_NOT_COLOCATED")

    def test_staff_without_attendance_permission_are_still_forbidden(self):
        self.client.force_authenticate(user=make_user("bridge-guest@example.com", role="GUEST"))
        response = self.client.get(STATUS_URL)
        self.assertEqual(response.status_code, 403)
