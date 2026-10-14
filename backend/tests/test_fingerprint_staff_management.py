"""Staff-profile fingerprint management: the permissions the page actually calls.

Regression coverage for the reported defect: staff profile → "Enroll / Re-Enroll
Fingerprint" answered 403 "You do not have the required permission for this
action." for EVERY role, including ADMIN, because the view was gated on a
capability code that existed nowhere in the catalogue and therefore could not be
held by any account (see apps/accounts/migrations/0021_staff_records_capabilities.py).

The suite pins, in one place:
  * every management role that the staff pages admit can read the biometric
    status AND enrol / revoke a fingerprint;
  * ordinary operational roles are still refused (no permission was widened);
  * enrolment + auto-approval produces an ACTIVE template and an audit trail;
  * a malformed minutiae payload is refused with a field-level 400 and leaves no
    template behind;
  * every capability code referenced anywhere in the API exists in the
    catalogue, so a phantom gate cannot silently re-appear.
"""
import re
from pathlib import Path

from apps.accounts.capabilities import ALL_CAPABILITY_CODES
from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.staff_operations.biometric_models import FingerprintTemplate

from .base import BaseAPITestCase
from .factories import make_staff

VALID_MINUTIAE = [
    {"x": 100 + i * 5, "y": 150 + i * 4, "angle": (30 + i * 15) % 360, "type": "ending", "quality": 90}
    for i in range(15)
]


def staff_status_url(staff_id):
    return f"/api/admin/staff-operations/fingerprint/staff/{staff_id}/"


class StaffFingerprintPermissionTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.target = make_staff("fp-perm.target@staff.dev", role=User.Role.HOUSEKEEPING)
        self.admin = make_staff("fp-perm.admin@staff.dev", role=User.Role.ADMIN)
        self.manager = make_staff("fp-perm.manager@staff.dev", role=User.Role.MANAGER)
        self.general_manager = make_staff("fp-perm.gm@staff.dev", role=User.Role.GENERAL_MANAGER)
        self.hr_manager = make_staff("fp-perm.hr@staff.dev", role=User.Role.HR_MANAGER)
        self.worker = make_staff("fp-perm.worker@staff.dev", role=User.Role.HOUSEKEEPING)
        self.chef = make_staff("fp-perm.chef@staff.dev", role=User.Role.CHEF)

    def test_management_roles_can_read_biometric_status(self):
        for label, user in [
            ("ADMIN", self.admin),
            ("MANAGER", self.manager),
            ("GENERAL_MANAGER", self.general_manager),
            ("HR_MANAGER", self.hr_manager),
        ]:
            with self.subTest(role=label):
                self.auth(user)
                response = self.client.get(staff_status_url(self.target.pk))
                self.assertEqual(response.status_code, 200, response.content)
                self.assertIn("enrollment_status", response.data["data"])

    def test_management_roles_can_enroll_and_revoke_a_fingerprint(self):
        self.auth(self.manager)
        enrolled = self.client.post(staff_status_url(self.target.pk), {
            "action": "enroll",
            "minutiae": VALID_MINUTIAE,
            "finger_position": "RIGHT_INDEX",
            "auto_approve": True,
        }, format="json")
        self.assertEqual(enrolled.status_code, 201, enrolled.content)
        self.assertEqual(enrolled.data["data"]["enrollment_status"], "ENROLLED")
        template = FingerprintTemplate.objects.get(staff=self.target)
        self.assertEqual(template.status, FingerprintTemplate.Status.ACTIVE)
        self.assertTrue(template.minutiae_data)
        self.assertTrue(
            AuditLog.objects.filter(action="FINGERPRINT_ENROLLMENT_APPROVED").exists(),
            "enrolment approval must be audited",
        )

        revoked = self.client.post(staff_status_url(self.target.pk), {"action": "revoke"}, format="json")
        self.assertEqual(revoked.status_code, 200, revoked.content)
        self.assertEqual(revoked.data["data"]["enrollment_status"], "REVOKED")
        template.refresh_from_db()
        self.assertEqual(template.status, FingerprintTemplate.Status.REVOKED)
        self.assertFalse(template.minutiae_data, "revocation must clear stored minutiae")

    def test_ordinary_operational_roles_are_still_refused(self):
        for label, user in [("HOUSEKEEPING", self.worker), ("CHEF", self.chef)]:
            with self.subTest(role=label):
                self.auth(user)
                self.assertEqual(self.client.get(staff_status_url(self.target.pk)).status_code, 403)
                self.assertEqual(
                    self.client.post(staff_status_url(self.target.pk), {
                        "action": "enroll", "minutiae": VALID_MINUTIAE,
                    }, format="json").status_code,
                    403,
                )
                self.assertFalse(FingerprintTemplate.objects.filter(staff=self.target).exists())

    def test_unauthenticated_callers_are_refused(self):
        self.unauth()
        self.assertIn(self.client.get(staff_status_url(self.target.pk)).status_code, (401, 403))

    def test_malformed_minutiae_is_refused_without_leaving_a_template(self):
        self.auth(self.admin)
        response = self.client.post(staff_status_url(self.target.pk), {
            "action": "enroll",
            "minutiae": [{"x": 1, "y": 2}],       # too few points / missing fields
        }, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(FingerprintTemplate.objects.filter(staff=self.target, status="ACTIVE").exists())

    def test_unsupported_action_is_refused(self):
        self.auth(self.admin)
        response = self.client.post(staff_status_url(self.target.pk), {"action": "destroy"}, format="json")
        self.assertEqual(response.status_code, 400, response.content)


class CapabilityCatalogueConsistencyTests(BaseAPITestCase):
    """Every capability code the API enforces must exist in the catalogue.

    A code that is referenced but never defined can never be held by any
    account (not even ADMIN, whose grants are resolved from persisted
    capability rows), so the endpoint silently 403s for everyone — the root
    cause of the staff-profile fingerprint defect.
    """

    CODE_PATTERN = re.compile(r"""["']([a-z][a-z0-9_]*\.[a-z0-9_.]+)["']""")

    def _referenced_codes(self):
        root = Path(__file__).resolve().parent.parent / "apps"
        codes = set()
        for path in root.rglob("*.py"):
            if "migrations" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if "has_capability(" not in text and "required_capabilit" not in text and "capabilities = " not in text:
                continue
            for match in self.CODE_PATTERN.finditer(text):
                codes.add(match.group(1))
        return codes

    def test_no_phantom_capability_codes_are_enforced(self):
        referenced = self._referenced_codes()
        # Only codes that look like capability identifiers and are used by the
        # capability helpers are checked; other dotted strings (model fields,
        # settings, actions) are ignored by intersecting with the naming shape
        # of the catalogue itself.
        shaped = {code for code in referenced if "." in code and code.split(".")[0] in {
            "account", "booking", "stay", "folio", "finance", "payment", "cash_session", "expense",
            "pos", "restaurant", "bar", "kitchen", "guest_request", "housekeeping", "maintenance",
            "inventory", "procurement", "staff", "shift", "attendance", "leave", "payroll", "reports",
            "audit", "terminal", "service_qr", "settings",
        }}
        missing = sorted(code for code in shaped if code not in ALL_CAPABILITY_CODES)
        self.assertEqual(missing, [], f"capability codes enforced but not defined: {missing}")
