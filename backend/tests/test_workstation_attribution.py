"""Registered workstations are optional audit hints, never access-control factors."""
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User, Workstation
from apps.audit.models import AuditLog
from tests.base import BaseAPITestCase
from tests.factories import make_staff


class WorkstationAttributionApiTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_staff("workstation.admin@staff.dev", role=User.Role.ADMIN)
        self.terminal = Workstation.objects.create(
            name="Front desk reception 1", department=Workstation.Department.FRONT_DESK, location="Lobby"
        )
        self.access = str(RefreshToken.for_user(self.admin).access_token)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {self.access}",
            HTTP_X_JONE_TERMINAL=self.terminal.reference,
            HTTP_X_REQUEST_ID="audit-request-001",
            HTTP_X_FORWARDED_FOR="203.0.113.99",
        )

    def test_authenticated_requests_report_terminal_and_trace_id_in_audit(self):
        response = self.client.post("/api/admin/users/terminals/", {
            "name": "Bar tablet 1", "department": "BAR", "location": "Lounge",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response["X-Request-ID"], "audit-request-001")

        self.terminal.refresh_from_db()
        self.assertEqual(self.terminal.current_staff_id, self.admin.pk)
        self.assertIsNotNone(self.terminal.last_seen_at)
        self.assertLessEqual((timezone.now() - self.terminal.last_seen_at).total_seconds(), 5)

        audit = AuditLog.objects.get(action="WORKSTATION_REGISTERED")
        self.assertEqual(audit.terminal_id, self.terminal.pk)
        self.assertEqual(audit.request_id, "audit-request-001")
        self.assertEqual(audit.actor_role, User.Role.ADMIN)
        # Untrusted forwarded values are not recorded as client IP evidence.
        self.assertEqual(audit.ip_address, "127.0.0.1")

    def test_workstation_registry_supports_bounded_search_and_active_filters(self):
        Workstation.objects.create(
            name="Kitchen pass", department=Workstation.Department.KITCHEN, location="Service pass"
        )
        archived = Workstation.objects.create(
            name="Reception archive", department=Workstation.Department.FRONT_DESK,
            location="Lobby", is_active=False,
        )
        response = self.client.get("/api/admin/users/terminals/", {
            "active": "true", "department": "FRONT_DESK", "search": "reception",
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([row["reference"] for row in response.json()["data"]], [self.terminal.reference])
        inactive = self.client.get("/api/admin/users/terminals/", {
            "active": "false", "department": "FRONT_DESK", "search": "reception",
        })
        self.assertEqual([row["reference"] for row in inactive.json()["data"]], [archived.reference])
        invalid = self.client.get("/api/admin/users/terminals/", {"active": "sometimes"})
        self.assertEqual(invalid.status_code, 400, invalid.content)

    def test_missing_or_inactive_workstation_never_rejects_valid_staff_auth(self):
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {self.access}",
            HTTP_X_JONE_TERMINAL="wks-not-registered",
        )
        response = self.client.get("/api/auth/capabilities/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["role"], User.Role.ADMIN)

        self.terminal.is_active = False
        self.terminal.save(update_fields=["is_active", "updated_at"])
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {self.access}",
            HTTP_X_JONE_TERMINAL=self.terminal.reference,
        )
        response = self.client.get("/api/auth/capabilities/")
        self.assertEqual(response.status_code, 200, response.content)
        self.terminal.refresh_from_db()
        self.assertIsNone(self.terminal.current_staff_id)
