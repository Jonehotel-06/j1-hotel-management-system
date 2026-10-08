# tests/test_audit_actions.py
from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.audit.views import humanise_action
from tests.base import BaseAPITestCase
from tests.factories import make_staff


class AuditLogActionFilterTests(BaseAPITestCase):
    """Task 22: the dashboard's action filter is server-driven, so actions
    added by new features (reschedule, guest discounts) show up without the
    hand-maintained <option> list going stale."""

    def setUp(self):
        super().setUp()
        self.admin = make_staff("audit.admin@staff.dev", role=User.Role.ADMIN)
        for action in ("BOOKING_RESCHEDULED", "GUEST_DISCOUNT_CREATED",
                       "GUEST_DISCOUNT_CREATED", "PAYMENT_RECORDED"):
            AuditLog.objects.create(actor=self.admin, action=action)

    def test_returns_distinct_recorded_actions_with_labels(self):
        self.auth(self.admin)
        response = self.client.get("/api/admin/audit-logs/actions/")
        self.assertEqual(response.status_code, 200, response.json())
        data = response.json()["data"]
        values = [row["value"] for row in data]
        self.assertEqual(values, sorted(set(values)))
        self.assertIn("BOOKING_RESCHEDULED", values)
        self.assertIn("GUEST_DISCOUNT_CREATED", values)
        labels = {row["value"]: row["label"] for row in data}
        self.assertEqual(labels["BOOKING_RESCHEDULED"], "Booking rescheduled")
        self.assertEqual(labels["GUEST_DISCOUNT_CREATED"], "Guest discount created")

    def test_filtering_the_list_by_a_returned_action_works(self):
        self.auth(self.admin)
        response = self.client.get("/api/admin/audit-logs/?action=BOOKING_RESCHEDULED")
        self.assertEqual(response.status_code, 200)
        rows = response.json()["data"]
        self.assertTrue(rows)
        self.assertTrue(all(r["action"] == "BOOKING_RESCHEDULED" for r in rows))

    def test_non_admin_staff_cannot_read_audit_actions(self):
        self.auth(make_staff("audit.desk@staff.dev", role=User.Role.RECEPTIONIST))
        self.assertEqual(self.client.get("/api/admin/audit-logs/actions/").status_code, 403)

    def test_humanise_action_handles_empty_and_single_word(self):
        self.assertEqual(humanise_action(""), "")
        self.assertEqual(humanise_action("NO_SHOW"), "No show")
