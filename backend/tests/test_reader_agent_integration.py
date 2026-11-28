# tests/test_reader_agent_integration.py
"""Reader-agent integration tests (no hardware, no browser).

They exercise the real Django stack: signed agent requests, single-use
sessions, the threshold decision, the existing attendance state machine, the
enrolment queue, and every refusal that matters (replay, stale clock, bad
signature, wrong staff, duplicate clock-in, unauthorised API call).

The vendor matcher itself is *not* exercised here (that is dpfj on Windows);
the agent's measurement is simulated exactly as the agent would report it after
dpfj_identify. Hardware acceptance is a separate, still-open item -- see
JONE_FINGERPRINT_REPAIR_OUTPUT/tests/HARDWARE_ACCEPTANCE.md.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import timedelta

from django.utils import timezone

from apps.accounts.models import User, Workstation
from apps.staff_operations.models import StaffProfile
from apps.staff_operations.models import AttendanceRecord
from apps.staff_operations.reader_agent_models import (
    ReaderAgent,
    ReaderAgentEvent,
    ReaderAgentSession,
    ReaderAgentTemplate,
)
from apps.staff_operations.services import reader_agent_service as svc

from .base import BaseAPITestCase
from .factories import make_staff

BASE = "/api/admin/staff-operations/reader-agent"

# A stand-in FMD: the tests never parse it, they only check it is stored and
# never leaves the server. ~700 bytes looks like a real single-view ISO FMD.
FMD = base64.b64encode(b"\x46\x4d\x52\x00" + b"\x11" * 700).decode()
FMD2 = base64.b64encode(b"\x46\x4d\x52\x00" + b"\x22" * 690).decode()

GOOD_SCORE = 900_000          # well under the 2_147_483 pass mark
BAD_SCORE = 2_147_500         # past the 2_147_483 pass mark


class ReaderAgentTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.worker = make_staff("agent.worker@jonehotel.com", role=User.Role.HOUSEKEEPING)
        self.worker2 = make_staff("agent.worker2@jonehotel.com", role=User.Role.RECEPTIONIST)
        self.supervisor = make_staff("agent.supervisor@jonehotel.com", role=User.Role.MANAGER)
        self.workstation = Workstation.objects.create(
            name="Reception Desk PC", department=Workstation.Department.FRONT_DESK,
        )
        self.agent, self.key = svc.provision_agent(
            actor=self.supervisor, workstation=self.workstation, label="Reception Desk PC reader",
        )
        self.template = ReaderAgentTemplate.objects.create(
            staff=self.worker, status=ReaderAgentTemplate.Status.ACTIVE, fmd=FMD, fmd_size=704,
            views=3, requested_by=self.supervisor, decided_by=self.supervisor,
            decided_at=timezone.now(), expires_at=timezone.now() + timedelta(days=365),
        )

    # -- helpers -----------------------------------------------------------
    def signed_post(self, path: str, payload: dict, *, reference: str | None = None,
                    key: str | None = None, nonce: str | None = None, timestamp: int | None = None,
                    signature: str | None = None):
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        nonce = nonce or f"nonce-{timezone.now().timestamp()}-{id(payload)}"
        timestamp = int(timezone.now().timestamp()) if timestamp is None else timestamp
        reference = reference or self.agent.reference
        key = key or self.key
        canonical = "\n".join([
            "jone-reader-agent-v1", reference, nonce, str(timestamp), _kind_of(path),
            hashlib.sha256(body).hexdigest(),
        ]).encode()
        signature = signature or hmac.new(bytes.fromhex(key), canonical, hashlib.sha256).hexdigest()
        return self.client.post(
            path, data=body, content_type="application/json",
            HTTP_X_JONE_AGENT_REFERENCE=reference,
            HTTP_X_JONE_AGENT_NONCE=nonce,
            HTTP_X_JONE_AGENT_TIMESTAMP=str(timestamp),
            HTTP_X_JONE_AGENT_KIND=_kind_of(path),
            HTTP_X_JONE_AGENT_SIGNATURE=signature,
        )

    def new_session(self, *, action="CLOCK_IN", staff=None, actor=None):
        session, token = svc.create_session(
            actor=actor or self.supervisor, action=action, staff=staff,
            workstation_reference=self.workstation.reference,
        )
        return session, token

    def identify(self, token, *, staff=None, template=None, score=GOOD_SCORE, **kwargs):
        staff = staff or self.worker
        template = template or self.template
        payload = {
            "session_token": token,
            "candidates": [{"staff_id": staff.pk, "template_id": template.pk, "score": score}],
            "captured_at": timezone.now().isoformat(),
            "device_state": {"device_status": "ready"},
        }
        return self.signed_post(f"{BASE}/agent/identify/", payload, **kwargs)


def _kind_of(path: str) -> str:
    for kind, header_kind in (
        ("heartbeat", "HEARTBEAT"),
        ("gallery", "GALLERY"),
        ("identify", "IDENTIFY"),
        ("enroll", "ENROLL"),
        ("session-fail", "SESSION_FAIL"),
    ):
        if f"/{kind}/" in path:
            return header_kind
    return "HEARTBEAT"


class SignedRequestTests(ReaderAgentTestBase):
    def test_unsigned_request_is_refused(self):
        response = self.client.post(f"{BASE}/agent/gallery/", data={}, content_type="application/json")
        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.json()["success"])

    def test_wrong_signature_is_refused(self):
        response = self.signed_post(f"{BASE}/agent/gallery/", {}, key="cd" * 32)
        self.assertEqual(response.status_code, 401)
        self.assertIn("AGENT_SIGNATURE_INVALID", response.json()["code"])

    def test_stale_agent_clock_is_refused(self):
        stale = int(timezone.now().timestamp()) - 4000
        response = self.signed_post(f"{BASE}/agent/gallery/", {}, timestamp=stale)
        self.assertEqual(response.status_code, 401)
        self.assertIn("AGENT_TIMESTAMP_STALE", response.json()["code"])

    def test_replayed_request_is_refused(self):
        first = self.signed_post(f"{BASE}/agent/gallery/", {}, nonce="replay-nonce-1")
        self.assertEqual(first.status_code, 200)
        second = self.signed_post(f"{BASE}/agent/gallery/", {}, nonce="replay-nonce-1")
        self.assertEqual(second.status_code, 401)
        self.assertIn("AGENT_NONCE_REPLAY", second.json()["code"])

    def test_revoked_agent_is_refused(self):
        self.agent.status = ReaderAgent.Status.REVOKED
        self.agent.save(update_fields=["status", "updated_at"])
        response = self.signed_post(f"{BASE}/agent/gallery/", {})
        self.assertEqual(response.status_code, 401)
        self.assertIn("AGENT_UNKNOWN", response.json()["code"])

    def test_unknown_agent_reference_is_refused(self):
        response = self.signed_post(f"{BASE}/agent/gallery/", {}, reference="RAG-NOTREAL")
        self.assertEqual(response.status_code, 401)


class GalleryTests(ReaderAgentTestBase):
    def test_gallery_serves_only_active_templates(self):
        ReaderAgentTemplate.objects.create(
            staff=self.worker2, status=ReaderAgentTemplate.Status.PENDING, fmd=FMD2, fmd_size=694,
        )
        response = self.signed_post(f"{BASE}/agent/gallery/", {})
        self.assertEqual(response.status_code, 200)
        templates = response.json()["data"]["templates"]
        self.assertEqual([row["staff_id"] for row in templates], [self.worker.pk])
        self.assertTrue(templates[0]["fmd"])

    def test_heartbeat_records_the_sensor_state(self):
        response = self.signed_post(f"{BASE}/agent/heartbeat/", {"state": {
            "device_status": "driver_mode_mismatch", "driver_mode": "WBF",
            "device_serial": "SER-1", "detail": "WBF driver bound", "sdk_available": True,
        }})
        self.assertEqual(response.status_code, 200)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.last_heartbeat["device_status"], "driver_mode_mismatch")
        self.assertEqual(self.agent.driver_mode, "WBF")
        self.assertIn("WBF", self.agent.last_error)


class IdentifyTests(ReaderAgentTestBase):
    def test_verified_fingerprint_records_attendance_for_that_staff(self):
        _session, token = self.new_session()
        response = self.identify(token)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["staff_id"], self.worker.pk)
        record = AttendanceRecord.objects.get(reference=data["attendance_reference"])
        self.assertEqual(record.staff_id, self.worker.pk)
        self.assertEqual(record.status, AttendanceRecord.Status.ON_DUTY)
        session = svc.session_by_token(token)
        self.assertEqual(session.status, ReaderAgentSession.Status.DONE)
        self.assertTrue(ReaderAgentEvent.objects.filter(agent=self.agent, nonce__isnull=False,
                                                        outcome="VERIFIED").exists())

    def test_same_session_cannot_be_used_twice(self):
        _session, token = self.new_session()
        self.assertEqual(self.identify(token).status_code, 200)
        replay = self.identify(token)
        self.assertEqual(replay.status_code, 409)
        self.assertIn("SESSION_ALREADY_USED", replay.json()["code"])
        self.assertEqual(AttendanceRecord.objects.count(), 1)

    def test_expired_session_is_refused(self):
        session, token = self.new_session()
        session.expires_at = timezone.now() - timedelta(seconds=5)
        session.save(update_fields=["expires_at", "updated_at"])
        response = self.identify(token)
        self.assertEqual(response.status_code, 409)
        self.assertIn("SESSION_EXPIRED", response.json()["code"])
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_unknown_session_is_refused(self):
        response = self.identify("not-a-real-token")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_score_above_threshold_never_records_attendance(self):
        _session, token = self.new_session()
        response = self.identify(token, score=BAD_SCORE)
        self.assertEqual(response.status_code, 422)
        self.assertIn("NO_MATCH", response.json()["code"])
        self.assertFalse(AttendanceRecord.objects.exists())
        self.assertEqual(svc.session_by_token(token).status, ReaderAgentSession.Status.FAILED)

    def test_candidate_for_a_different_staff_member_is_refused(self):
        # A session bound to worker2 cannot be satisfied by worker's template.
        _session, token = self.new_session(action="VERIFY", staff=self.worker2)
        response = self.identify(token, staff=self.worker)
        self.assertEqual(response.status_code, 422)
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_agent_cannot_substitute_an_unenrolled_template(self):
        other = ReaderAgentTemplate.objects.create(
            staff=self.worker2, status=ReaderAgentTemplate.Status.REVOKED, fmd=FMD2, fmd_size=694,
        )
        _session, token = self.new_session()
        payload_staff = self.worker
        response = self.identify(token, staff=payload_staff, template=other)
        self.assertEqual(response.status_code, 422)

    def test_second_clock_in_without_clock_out_is_refused_by_the_state_machine(self):
        _session, first = self.new_session()
        self.assertEqual(self.identify(first).status_code, 200)
        _session2, second = self.new_session()
        response = self.identify(second)
        self.assertEqual(response.status_code, 409)
        self.assertIn("ATTENDANCE_REFUSED", response.json()["code"])
        self.assertEqual(AttendanceRecord.objects.count(), 1)

    def test_clock_out_after_clock_in_closes_the_record(self):
        _s1, clock_in = self.new_session()
        self.assertEqual(self.identify(clock_in).status_code, 200)
        _s2, clock_out = self.new_session(action="CLOCK_OUT")
        response = self.identify(clock_out)
        self.assertEqual(response.status_code, 200, response.content)
        record = AttendanceRecord.objects.get(staff=self.worker)
        self.assertEqual(record.status, AttendanceRecord.Status.COMPLETED)
        self.assertIsNotNone(record.clock_out_at)

    def test_stale_capture_timestamp_is_refused(self):
        _session, token = self.new_session()
        payload = {
            "session_token": token,
            "candidates": [{"staff_id": self.worker.pk, "template_id": self.template.pk, "score": GOOD_SCORE}],
            "captured_at": (timezone.now() - timedelta(minutes=30)).isoformat(),
            "device_state": {},
        }
        response = self.signed_post(f"{BASE}/agent/identify/", payload)
        self.assertEqual(response.status_code, 409)
        self.assertIn("CAPTURE_TIMESTAMP_STALE", response.json()["code"])
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_verify_action_records_no_attendance(self):
        _session, token = self.new_session(action="VERIFY")
        response = self.identify(token)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_staff_without_attendance_capability_cannot_be_clocked(self):
        guest = make_staff("agent.guest-role@jonehotel.com", role=User.Role.GUEST)
        ReaderAgentTemplate.objects.create(
            staff=guest, status=ReaderAgentTemplate.Status.ACTIVE, fmd=FMD2, fmd_size=694,
            expires_at=timezone.now() + timedelta(days=30),
        )
        _session, token = self.new_session()
        response = self.identify(token, staff=guest,
                                 template=ReaderAgentTemplate.objects.get(staff=guest))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(AttendanceRecord.objects.exists())


class EnrolmentTests(ReaderAgentTestBase):
    def _enroll_session(self, staff=None):
        staff = staff or self.worker2
        return self.new_session(action="ENROLL", staff=staff, actor=self.supervisor)

    def test_enrolment_creates_a_pending_template(self):
        _session, token = self._enroll_session()
        response = self.signed_post(f"{BASE}/agent/enroll/", {
            "session_token": token, "staff_id": self.worker2.pk, "finger_position": "LEFT_INDEX",
            "fmd": FMD2, "views": 3,
        })
        self.assertEqual(response.status_code, 200, response.content)
        template = ReaderAgentTemplate.objects.get(pk=response.json()["data"]["template_id"])
        self.assertEqual(template.staff_id, self.worker2.pk)
        self.assertEqual(template.status, ReaderAgentTemplate.Status.PENDING)
        self.assertEqual(template.views, 3)

    def test_duplicate_enrolment_is_rejected(self):
        ReaderAgentTemplate.objects.create(
            staff=self.worker2, status=ReaderAgentTemplate.Status.ACTIVE, fmd=FMD2, fmd_size=694,
        )
        _session, token = self._enroll_session()
        response = self.signed_post(f"{BASE}/agent/enroll/", {
            "session_token": token, "staff_id": self.worker2.pk, "fmd": FMD2, "views": 3,
        })
        self.assertEqual(response.status_code, 409)
        self.assertIn("ALREADY_ENROLLED", response.json()["code"])

    def test_garbage_template_is_refused(self):
        _session, token = self._enroll_session()
        response = self.signed_post(f"{BASE}/agent/enroll/", {
            "session_token": token, "staff_id": self.worker2.pk, "fmd": "not-base64!!", "views": 1,
        })
        self.assertEqual(response.status_code, 400)

    def test_enrolment_session_cannot_be_used_for_identification(self):
        _session, token = self._enroll_session()
        response = self.identify(token)
        self.assertEqual(response.status_code, 409)
        self.assertIn("SESSION_ACTION_MISMATCH", response.json()["code"])


class IssueCodeTests(ReaderAgentTestBase):
    """The 6-digit numeric code, issued from a REALLY verified finger.

    The reception console used to reach the same feature by posting synthetic
    minutiae ("captureMinutiaeProbe" in js/biometrics.js) to the legacy terminal
    endpoint, so the code was never tied to a read of the sensor. These tests pin
    the replacement: the agent-verified session is the only way in.
    """

    def test_issue_code_returns_a_bound_code_and_one_attendance_record(self):
        _session, token = self.new_session(action="ISSUE_CODE")
        response = self.identify(token)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["staff_id"], self.worker.pk)
        self.assertTrue(data["access_code"])
        self.assertEqual(len(str(data["access_code"])), 6)
        self.assertTrue(data["access_code"].isdigit())
        self.assertTrue(data["attendance_reference"])
        self.assertEqual(AttendanceRecord.objects.filter(staff=self.worker).count(), 1)
        self.assertEqual(
            AttendanceRecord.objects.get(staff=self.worker).reference, data["attendance_reference"]
        )

    def test_re_issuing_the_code_never_opens_a_second_attendance_record(self):
        _session, first_token = self.new_session(action="ISSUE_CODE")
        first = self.identify(first_token)
        self.assertEqual(first.status_code, 200, first.content)
        _session, second_token = self.new_session(action="ISSUE_CODE")
        second = self.identify(second_token)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(second.json()["data"]["attendance_created"], False)
        self.assertEqual(AttendanceRecord.objects.filter(staff=self.worker).count(), 1)
        self.assertEqual(
            second.json()["data"]["attendance_reference"],
            first.json()["data"]["attendance_reference"],
        )

    def test_the_code_is_expired_by_clock_out(self):
        from apps.staff_operations.models import TemporaryWorkstationCredential

        _session, code_token = self.new_session(action="ISSUE_CODE")
        issued = self.identify(code_token)
        self.assertEqual(issued.status_code, 200, issued.content)
        self.assertTrue(
            TemporaryWorkstationCredential.objects.filter(
                staff=self.worker, session_active=True, revoked_at__isnull=True
            ).exists()
        )
        _session, out_token = self.new_session(action="CLOCK_OUT")
        out = self.identify(out_token)
        self.assertEqual(out.status_code, 200, out.content)
        self.assertFalse(
            TemporaryWorkstationCredential.objects.filter(
                staff=self.worker, session_active=True, revoked_at__isnull=True
            ).exists()
        )

    def test_an_unmatched_finger_never_issues_a_code(self):
        _session, token = self.new_session(action="ISSUE_CODE")
        response = self.identify(token, score=BAD_SCORE)
        self.assertEqual(response.status_code, 422, response.content)
        self.assertEqual(AttendanceRecord.objects.filter(staff=self.worker).count(), 0)
        self.assertEqual(ReaderAgentSession.objects.filter(action="ISSUE_CODE").count(), 1)


class BrowserEndpointTests(ReaderAgentTestBase):
    def test_session_creation_requires_authentication(self):
        response = self.client.post(f"{BASE}/sessions/", {"action": "CLOCK_IN"}, format="json")
        self.assertIn(response.status_code, (401, 403))

    def test_operator_can_create_a_session_and_poll_it(self):
        self.auth(self.supervisor)
        response = self.client.post(f"{BASE}/sessions/", {
            "action": "CLOCK_IN", "workstation_reference": self.workstation.reference,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()["data"]
        self.assertTrue(data["session_token"])
        self.assertTrue(data["launch_uri"].startswith("jone-reader:capture?session="))
        self.assertIn("api=", data["launch_uri"])

        poll = self.client.get(f"{BASE}/sessions/{data['session_token']}/")
        self.assertEqual(poll.status_code, 200)
        self.assertEqual(poll.json()["data"]["status"], "PENDING")

    def test_launch_uri_contains_no_reader_or_biometric_data(self):
        self.auth(self.supervisor)
        data = self.client.post(f"{BASE}/sessions/", {"action": "CLOCK_IN"}, format="json").json()["data"]
        uri = data["launch_uri"]
        self.assertNotIn("fmd", uri.lower())
        self.assertNotIn("template", uri.lower())
        self.assertTrue(uri.startswith("jone-reader:"))

    def test_another_operator_cannot_poll_a_session(self):
        self.auth(self.supervisor)
        token = self.client.post(f"{BASE}/sessions/", {"action": "CLOCK_IN"}, format="json").json()["data"]["session_token"]
        self.auth(self.worker)
        poll = self.client.get(f"{BASE}/sessions/{token}/")
        self.assertEqual(poll.status_code, 403)

    def test_only_attendance_management_can_provision(self):
        self.auth(self.worker)
        response = self.client.post(f"{BASE}/agents/provision/", {}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_reprovisioning_reuses_the_existing_agent_without_rekeying_it(self):
        """Re-provisioning must NOT re-key a healthy agent (it would break the PC).

        The old behaviour rotated the key on every press, so a second operator
        pressing the same button silently invalidated the agent already running
        on the reception desk.
        """
        self.auth(self.supervisor)
        before = ReaderAgent.objects.get(pk=self.agent.pk).signing_key
        response = self.client.post(f"{BASE}/agents/provision/", {
            "workstation_reference": self.workstation.reference,
        }, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertTrue(data["already_provisioned"])
        self.assertNotIn("signing_key", data)
        self.assertEqual(data["reference"], self.agent.reference)
        self.assertEqual(ReaderAgent.objects.get(pk=self.agent.pk).signing_key, before)
        self.assertEqual(ReaderAgent.objects.filter(workstation=self.workstation).count(), 1)

    def test_provisioning_a_new_workstation_creates_one_agent_and_returns_its_key(self):
        second_desk = Workstation.objects.create(
            name="Annex Front Desk", department=Workstation.Department.FRONT_DESK,
        )
        self.auth(self.supervisor)
        response = self.client.post(f"{BASE}/agents/provision/", {
            "workstation_reference": second_desk.reference,
        }, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertFalse(data["already_provisioned"])
        self.assertTrue(data["signing_key"])
        self.assertTrue(data["install_uri"].startswith("jone-reader:provision?"))
        self.assertEqual(ReaderAgent.objects.filter(workstation=second_desk).count(), 1)

    def test_explicit_rekey_rotates_the_key_and_retires_the_old_one(self):
        self.auth(self.supervisor)
        before = ReaderAgent.objects.get(pk=self.agent.pk).signing_key
        response = self.client.post(f"{BASE}/agents/provision/", {
            "workstation_reference": self.workstation.reference, "rotate": True,
        }, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["data"]["rotated"])
        self.assertNotEqual(ReaderAgent.objects.get(pk=self.agent.pk).signing_key, before)
        # The retired key can no longer sign anything.
        old_key_gallery = self.signed_post(f"{BASE}/agent/gallery/", {}, key=before)
        self.assertIn(old_key_gallery.status_code, (401, 403))

    def test_provisioning_is_refused_for_a_receptionist_and_names_the_permission(self):
        self.auth(self.worker2)  # RECEPTIONIST
        response = self.client.post(f"{BASE}/agents/provision/", {}, format="json")
        self.assertEqual(response.status_code, 403)
        body = response.content.decode()
        self.assertIn("attendance.manage", body)
        self.assertIn("Manage attendance", body)

    def test_setup_status_tells_a_receptionist_which_permission_is_missing(self):
        self.auth(self.worker2)
        response = self.client.get(f"{BASE}/agents/setup-status/")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertFalse(data["can_provision"])
        self.assertFalse(data["setup_ready"])
        self.assertEqual(data["missing_capability"], "attendance.manage")
        self.assertEqual(data["role"], User.Role.RECEPTIONIST)
        self.assertTrue(data["reason"])
        self.assertTrue(data["grant_hint"])
        self.assertNotIn("attendance.manage", data["holds"])

    def test_setup_status_tells_a_manager_it_can_provision(self):
        self.auth(self.supervisor)
        response = self.client.get(f"{BASE}/agents/setup-status/")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertTrue(data["can_provision"])
        self.assertTrue(data["setup_ready"])
        self.assertEqual(data["workstation_reference"], self.workstation.reference)
        self.assertEqual(data["agent"]["reference"], self.agent.reference)

    def test_setup_status_reports_a_broken_role_mapping_instead_of_guessing(self):
        """A manager whose department context is invalid resolves no capabilities.

        That is the invisible failure the old screen could not describe: the page
        looks like a management page because the browser renders by role, while
        the server resolves an empty capability set and refuses every action with
        "only attendance management". The diagnosis has to name that link.
        """
        broken = make_staff("agent.broken@jonehotel.com", role=User.Role.MANAGER)
        # A conflicting stored department is exactly what department_for_user()
        # fails closed on (the role says Management, the profile says Kitchen),
        # so the account resolves no capabilities at all.
        StaffProfile.objects.create(
            user=broken, employee_code="EMP-BROKEN-1", department=Workstation.Department.KITCHEN,
            job_title="Duty Manager", employment_status="ACTIVE",
        )
        self.auth(broken)
        response = self.client.get(f"{BASE}/agents/setup-status/")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertFalse(data["department_context_valid"])
        self.assertFalse(data["can_provision"])
        self.assertEqual(data["capability_count"], 0)
        self.assertIn("department", data["reason"].lower())

    def test_enrolment_approval_keeps_the_maker_checker_rule(self):
        pending = ReaderAgentTemplate.objects.create(
            staff=self.worker2, status=ReaderAgentTemplate.Status.PENDING, fmd=FMD2, fmd_size=694,
            requested_by=self.worker2,
        )
        self.auth(self.worker2)
        own = self.client.post(f"{BASE}/templates/{pending.pk}/approve/", {}, format="json")
        self.assertIn(own.status_code, (403, 400))
        pending.refresh_from_db()
        self.assertEqual(pending.status, ReaderAgentTemplate.Status.PENDING)

    def test_supervisor_approval_activates_the_template(self):
        pending = ReaderAgentTemplate.objects.create(
            staff=self.worker2, status=ReaderAgentTemplate.Status.PENDING, fmd=FMD2, fmd_size=694,
            requested_by=self.supervisor,
        )
        self.auth(self.supervisor)
        response = self.client.post(f"{BASE}/templates/{pending.pk}/approve/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        pending.refresh_from_db()
        self.assertEqual(pending.status, ReaderAgentTemplate.Status.ACTIVE)

    def test_agent_list_shows_the_sensor_state(self):
        self.auth(self.supervisor)
        response = self.client.get(f"{BASE}/agents/")
        self.assertEqual(response.status_code, 200)
        row = response.json()["data"][0]
        self.assertEqual(row["reference"], self.agent.reference)
        self.assertFalse(row["online"])
        self.assertIn("driver_mode", row)

    def test_agent_session_fail_marks_pending_session_failed_without_overwriting_done(self):
        _session, token = self.new_session(action="CLOCK_IN")
        fail_res = self.signed_post(f"{BASE}/agent/session-fail/", {
            "session_token": token,
            "error_code": "READER_NOT_CONNECTED",
            "error_message": "Plug in the DigitalPersona reader.",
            "device_state": {"device_status": "not_connected", "detail": "USB unplugged"},
        })
        self.assertEqual(fail_res.status_code, 200, fail_res.content)
        self.assertEqual(svc.session_by_token(token).status, ReaderAgentSession.Status.FAILED)
        self.assertEqual(svc.session_by_token(token).error_code, "READER_NOT_CONNECTED")

        # Already-DONE sessions remain untouched by a late session-fail call.
        _done_sess, done_token = self.new_session(action="VERIFY")
        self.assertEqual(self.identify(done_token).status_code, 200)
        late = self.signed_post(f"{BASE}/agent/session-fail/", {
            "session_token": done_token,
            "error_code": "CAPTURE_TIMEOUT",
            "error_message": "Late error",
        })
        self.assertEqual(late.status_code, 200)
        self.assertEqual(svc.session_by_token(done_token).status, ReaderAgentSession.Status.DONE)

    def test_enroll_refusal_persists_session_failed_status_for_browser_polling(self):
        ReaderAgentTemplate.objects.create(
            staff=self.worker2, status=ReaderAgentTemplate.Status.ACTIVE, fmd=FMD2, fmd_size=694,
        )
        _session, token = self.new_session(action="ENROLL", staff=self.worker2)
        response = self.signed_post(f"{BASE}/agent/enroll/", {
            "session_token": token, "staff_id": self.worker2.pk, "fmd": FMD2, "views": 4,
        })
        self.assertEqual(response.status_code, 409)
        session = svc.session_by_token(token)
        self.assertEqual(session.status, ReaderAgentSession.Status.FAILED)
        self.assertEqual(session.error_code, "ALREADY_ENROLLED")

    def test_enroll_session_launch_uri_targets_enroll_verb(self):
        self.auth(self.supervisor)
        response = self.client.post(f"{BASE}/sessions/", {
            "action": "ENROLL",
            "staff_id": self.worker2.pk,
            "finger_position": "LEFT_THUMB",
            "workstation_reference": self.workstation.reference,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        uri = response.json()["data"]["launch_uri"]
        self.assertTrue(uri.startswith("jone-reader:enroll?"))
        self.assertIn(f"staff_id={self.worker2.pk}", uri)
        self.assertIn("finger_position=LEFT_THUMB", uri)

    def test_heartbeat_returns_gallery_etag_and_conditional_gallery_respects_it(self):
        hb = self.signed_post(f"{BASE}/agent/heartbeat/", {"state": {"device_status": "ready"}})
        self.assertEqual(hb.status_code, 200)
        etag = hb.json()["data"]["gallery_etag"]
        self.assertTrue(etag)

        cached = self.signed_post(f"{BASE}/agent/gallery/", {"etag": etag})
        self.assertEqual(cached.status_code, 200)
        self.assertTrue(cached.json()["data"]["unchanged"])
        self.assertNotIn("templates", cached.json()["data"])

    def test_template_revoke_clears_fmd_and_updates_staff_biometric_status(self):
        from apps.staff_operations.services import fingerprint_service

        self.auth(self.supervisor)
        status_before = self.client.get(
            f"/api/admin/staff-operations/fingerprint/staff/{self.worker.pk}/"
        )
        self.assertEqual(status_before.status_code, 200, status_before.content)
        self.assertEqual(status_before.json()["data"]["enrollment_status"], "ENROLLED")
        self.assertTrue(status_before.json()["data"]["enrolled"])

        rev = self.client.post(f"{BASE}/templates/{self.template.pk}/revoke/", {"note": "Re-enroll"}, format="json")
        self.assertEqual(rev.status_code, 200, rev.content)
        self.template.refresh_from_db()
        self.assertEqual(self.template.status, ReaderAgentTemplate.Status.REVOKED)
        self.assertEqual(self.template.fmd, "")
        self.assertEqual(fingerprint_service.get_staff_biometric_status(self.worker)["enrollment_status"], "REVOKED")

    def test_admin_can_revoke_stale_agent_and_hide_it_from_active_list(self):
        self.auth(self.supervisor)
        resp = self.client.post(f"{BASE}/agents/{self.agent.reference}/revoke/", {"reason": "PC replaced"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["status"], ReaderAgent.Status.REVOKED)
        listed = self.client.get(f"{BASE}/agents/").json()["data"]
        self.assertEqual(listed, [])
        self.unauth()
        hb = self.signed_post(f"{BASE}/agent/heartbeat/", {"state": {"device_status": "ready"}})
        self.assertEqual(hb.status_code, 401)

    def test_staff_profile_list_and_terminal_kpis_reflect_reader_agent_templates(self):
        StaffProfile.objects.create(
            user=self.worker, employee_code="EMP-AGENT-W1", department=Workstation.Department.HOUSEKEEPING,
            job_title="Room Attendant", employment_status="ACTIVE",
        )
        ReaderAgentTemplate.objects.create(
            staff=self.worker2, status=ReaderAgentTemplate.Status.PENDING, fmd=FMD2, fmd_size=694,
        )
        self.auth(self.supervisor)
        payload = self.client.get("/api/admin/staff-operations/profiles/?fingerprint_status=ENROLLED").json()
        profiles = payload["data"] if isinstance(payload["data"], list) else payload["data"]["results"]
        self.assertEqual([p["user"] for p in profiles], [self.worker.pk])
        self.assertEqual(profiles[0]["fingerprint_status"], "ENROLLED")

        from apps.accounts.desktop_policy import issue_desktop_key

        ws_key = issue_desktop_key(self.workstation)
        self.workstation.save(update_fields=["sign_in_key_hash"])
        self.unauth()
        summary = self.client.get(
            "/api/admin/staff-operations/biometrics/reception-terminal/",
            HTTP_X_JONE_DESKTOP_KEY=ws_key,
        )
        self.assertEqual(summary.status_code, 200, summary.content)
        kpi = summary.json()["data"]["summary"]
        self.assertEqual(kpi["enrolled_staff_count"], 1)
        self.assertEqual(kpi["pending_enrollments_count"], 1)

