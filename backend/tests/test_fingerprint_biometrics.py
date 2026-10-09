"""Fingerprint biometrics: ISO minutiae verification, clock gate, and temporary workstation credentials."""
from datetime import timedelta
import json
from apps.accounts.models import User, Workstation
from apps.accounts.desktop_policy import issue_desktop_key
from apps.staff_operations.biometric_models import (
    FingerprintTemplate,
    FingerprintVerificationAttempt,
    TemporaryWorkstationCredential,
)
from apps.staff_operations.services import fingerprint_service
from .base import BaseAPITestCase
from .factories import make_staff

# Sample ISO minutiae points
VALID_MINUTIAE = [
    {"x": 100 + i * 5, "y": 150 + i * 4, "angle": (30 + i * 15) % 360, "type": "ending", "quality": 90}
    for i in range(15)
]

MATCHING_PROBE = [
    {"x": 100 + i * 5 + 1.0, "y": 150 + i * 4 + 1.0, "angle": (30 + i * 15) % 360, "type": "ending", "quality": 85}
    for i in range(15)
]

DIFFERENT_PROBE = [
    {"x": 500 + i * 10, "y": 800 + i * 10, "angle": (180 + i * 20) % 360, "type": "bifurcation", "quality": 75}
    for i in range(15)
]


class FingerprintBiometricsTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.worker = make_staff("fp.worker@jonehotel.com", role=User.Role.HOUSEKEEPING)
        self.supervisor = make_staff("fp.supervisor@jonehotel.com", role=User.Role.MANAGER)
        self.workstation = Workstation.objects.create(
            name="Front Desk Terminal 1", department=Workstation.Department.FRONT_DESK
        )
        self.desk_key = issue_desktop_key(self.workstation)
        self.workstation.save()

    def test_fingerprint_enrollment_workflow(self):
        # 1. Worker requests enrollment
        self.auth(self.worker)
        res = self.client.post("/api/admin/staff-operations/fingerprint/enrollment/", {
            "minutiae": VALID_MINUTIAE,
            "finger_position": "RIGHT_INDEX",
        }, format="json")
        self.assertEqual(res.status_code, 201)
        tpl_id = res.json()["data"]["id"]
        tpl = FingerprintTemplate.objects.get(pk=tpl_id)
        self.assertEqual(tpl.status, FingerprintTemplate.Status.PENDING)

        # 2. Supervisor approves enrollment
        self.auth(self.supervisor)
        res_approve = self.client.post(f"/api/admin/staff-operations/fingerprint/enrollments/{tpl_id}/approve/")
        self.assertEqual(res_approve.status_code, 200)
        tpl.refresh_from_db()
        self.assertEqual(tpl.status, FingerprintTemplate.Status.ACTIVE)

    def test_fingerprint_clock_verification(self):
        # Enroll and approve
        tpl = fingerprint_service.request_fingerprint_enrollment(
            staff=self.worker, actor=self.worker, minutiae=VALID_MINUTIAE
        )
        fingerprint_service.approve_fingerprint_enrollment(template=tpl, approver=self.supervisor)

        # 1. Clock with non-matching probe fails
        self.auth(self.worker)
        res_fail = self.client.post("/api/admin/staff-operations/attendance/clock/", {
            "action": "CLOCK_IN",
            "idempotency_key": "fp-clock-1",
            "fingerprint_probe": DIFFERENT_PROBE,
        }, format="json")
        self.assertEqual(res_fail.status_code, 422)

        # 2. Clock with matching probe succeeds
        res_ok = self.client.post("/api/admin/staff-operations/attendance/clock/", {
            "action": "CLOCK_IN",
            "idempotency_key": "fp-clock-2",
            "fingerprint_probe": MATCHING_PROBE,
        }, format="json")
        self.assertIn(res_ok.status_code, (200, 201))

    def test_temporary_workstation_credential_issuance_and_login(self):
        # Enroll and approve worker
        tpl = fingerprint_service.request_fingerprint_enrollment(
            staff=self.worker, actor=self.worker, minutiae=VALID_MINUTIAE
        )
        fingerprint_service.approve_fingerprint_enrollment(template=tpl, approver=self.supervisor)

        # 1. Issue temporary credential from approved workstation
        self.unauth()
        issue_res = self.client.post(
            "/api/admin/staff-operations/biometrics/temporary-credential/",
            {"email": self.worker.email, "fingerprint_probe": MATCHING_PROBE},
            format="json",
            HTTP_X_JONE_DESKTOP_KEY=self.desk_key,
        )
        self.assertEqual(issue_res.status_code, 200)
        temp_token = issue_res.json()["data"]["temporary_token"]
        self.assertTrue(temp_token.startswith("jone-temp-"))

        # 2. Sign in with the temporary token
        login_res = self.client.post("/api/auth/login/", {"temporary_token": temp_token}, format="json")
        self.assertEqual(login_res.status_code, 200)
        data = login_res.json()["data"]
        self.assertEqual(data["user"]["email"], self.worker.email)
        self.assertIn("access", data["tokens"])

        # 3. Re-using the consumed token fails
        reuse_res = self.client.post("/api/auth/login/", {"temporary_token": temp_token}, format="json")
        self.assertEqual(reuse_res.status_code, 401)
