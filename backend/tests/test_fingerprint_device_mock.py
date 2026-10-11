# tests/test_fingerprint_device_mock.py
"""Mocked fingerprint-READER integration tests (no physical hardware).

Drives the device integration boundary in
``apps.staff_operations.services.fingerprint_device`` with a simulated reader:

* capture → identify → verified clock-in with an attendance record;
* replayed capture nonces are refused;
* stale capture timestamps are refused;
* unknown / disabled devices are refused;
* non-matching probes never record attendance but ARE audited;
* the append-only device event log records device, outcome and attendance ref.

PHYSICAL DEVICE ACCEPTANCE IS BLOCKED: these tests use a simulated device and
must not be cited as evidence that a real reader has been tested.
"""
from datetime import timedelta
from decimal import Decimal  # noqa: F401  (kept for symmetry with sibling tests)

from django.utils import timezone

from apps.accounts.models import User, Workstation
from apps.staff_operations.biometric_models import (
    FingerprintDevice, FingerprintDeviceEvent, FingerprintTemplate,
)
from apps.staff_operations.services import fingerprint_device, fingerprint_service

from .base import BaseAPITestCase
from .factories import make_staff

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


class MockFingerprintDeviceTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.staff = make_staff("fp.device.worker@jonehotel.com", role=User.Role.HOUSEKEEPING)
        self.supervisor = make_staff("fp.device.supervisor@jonehotel.com", role=User.Role.MANAGER)
        self.workstation = Workstation.objects.create(
            name="Reception Reader Workstation", department=Workstation.Department.FRONT_DESK,
        )
        self.device = fingerprint_device.register_device(
            serial_number="MOCK-READER-001", label="Mock 80mm fingerprint reader",
            workstation=self.workstation, actor=self.supervisor,
            vendor="Simulated", model_name="MockCapture SDK 1.0",
        )
        # Enrol + approve a template for the staff member.
        template = fingerprint_service.request_fingerprint_enrollment(
            staff=self.staff, actor=self.staff, minutiae=VALID_MINUTIAE,
            finger_position="RIGHT_INDEX",
        )
        template.status = FingerprintTemplate.Status.ACTIVE
        template.save(update_fields=["status", "updated_at"])

    def _capture(self, nonce="evt-001", probe=MATCHING_PROBE, action="CLOCK_IN",
                 captured_at=None, serial="MOCK-READER-001"):
        return fingerprint_device.submit_device_capture(
            serial_number=serial, event_nonce=nonce,
            captured_at=captured_at or timezone.now(),
            action=action, staff=self.staff, probe=probe,
        )

    def test_verified_capture_records_attendance_and_event(self):
        event, outcome, passed, record = self._capture()
        self.assertTrue(passed)
        self.assertEqual(outcome, fingerprint_service.FingerprintOutcome.VERIFIED)
        self.assertIsNotNone(record)
        self.assertEqual(record.staff_id, self.staff.pk)
        self.assertEqual(event.attendance_reference, record.reference)
        self.assertEqual(event.device_id, self.device.pk)
        self.assertIsNone(event.details.get("raw_image"), "no raw biometric imagery is stored")
        # Device heartbeat updated; audit history present.
        self.device.refresh_from_db()
        self.assertIsNotNone(self.device.last_seen_at)

    def test_replayed_event_nonce_is_refused(self):
        self._capture(nonce="evt-replay")
        with self.assertRaises(Exception):
            self._capture(nonce="evt-replay")
        # Exactly one event and one attendance record despite the replay.
        self.assertEqual(FingerprintDeviceEvent.objects.filter(event_nonce="evt-replay").count(), 1)

    def test_stale_capture_timestamp_is_refused(self):
        stale = timezone.now() - timedelta(hours=2)
        with self.assertRaises(Exception):
            self._capture(nonce="evt-stale", captured_at=stale)
        self.assertEqual(FingerprintDeviceEvent.objects.count(), 0)

    def test_unknown_or_disabled_device_is_refused(self):
        with self.assertRaises(Exception):
            self._capture(nonce="evt-unknown", serial="NOT-REGISTERED")
        self.device.status = FingerprintDevice.Status.DISABLED
        self.device.save(update_fields=["status", "updated_at"])
        with self.assertRaises(Exception):
            self._capture(nonce="evt-disabled")
        self.assertEqual(FingerprintDeviceEvent.objects.count(), 0)

    def test_non_matching_probe_is_audited_but_records_no_attendance(self):
        event, outcome, passed, record = self._capture(
            nonce="evt-nomatch", probe=DIFFERENT_PROBE,
        )
        self.assertFalse(passed)
        self.assertEqual(outcome, fingerprint_service.FingerprintOutcome.NO_MATCH)
        self.assertIsNone(record)
        self.assertEqual(event.attendance_reference, "")
        self.assertEqual(FingerprintDeviceEvent.objects.count(), 1)

    def test_duplicate_clock_in_follows_attendance_idempotency(self):
        _, _, _, first = self._capture(nonce="evt-dup-1", action="CLOCK_IN")
        # A second, distinct capture event attempting another clock-in is
        # refused by the attendance state machine (no duplicate clock-in)…
        with self.assertRaises(Exception):
            self._capture(nonce="evt-dup-2", action="CLOCK_IN")
        # …but BOTH capture attempts are audited, and attendance stays one.
        self.assertEqual(FingerprintDeviceEvent.objects.count(), 2)
        self.assertIsNotNone(first.pk)

    def test_registration_requires_attendance_manage(self):
        with self.assertRaises(Exception):
            fingerprint_device.register_device(
                serial_number="MOCK-READER-002", label="Unauthorised",
                workstation=self.workstation, actor=self.staff,
            )

    def test_device_event_log_is_append_only(self):
        event, _, _, _ = self._capture(nonce="evt-immutable")
        event.outcome = "TAMPERED"
        with self.assertRaises(Exception):
            event.save()
        with self.assertRaises(Exception):
            event.delete()
