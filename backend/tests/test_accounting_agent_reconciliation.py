"""Accounting agent: the "unresolved room or billing checks" number and its records.

The Accounting screen said "5 unresolved room or billing checks" and offered a
Review link that scrolled to a bullet list of `reference · detail` lines. These
tests pin the repaired behaviour:

1. the count is split by subject — a cash-drawer variance is a cash-drawer
   check, not a room or billing check;
2. the count is derived from real records and each record carries the object,
   the amount, why it was flagged, its status and the recommended next action;
3. checks that were never discrepancies are not raised as ones (a guest who has
   not arrived yet; a folio that is on hold rather than missing);
4. a decision is recorded, requires a reason, is append-only, and never edits
   the financial record it is about;
5. the count follows the underlying status: when the condition clears the check
   resolves instead of lingering, and a dismissal is reported as dismissed
   rather than silently deleted;
6. permissions: a reader without financial visibility cannot read the pack, and
   a user without expense authority cannot record a review.
"""
from datetime import timedelta
from decimal import Decimal

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.core.utils import hotel_today
from apps.finance.models import CashSession, Folio, ReconciliationCheckReview
from apps.finance.services.cash_session_service import close_cash_session, open_cash_session
from apps.finance.services.folio_service import get_or_create_main_folio_for_stay
from apps.stays.models import Stay, StayRoom

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type, make_staff

CHECKS_URL = "/api/admin/finance/reconciliation/checks/"


class ReconciliationCheckTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("recon-manager@staff.dev", role=User.Role.MANAGER)
        self.accountant = make_staff("recon-accountant@staff.dev", role=User.Role.ACCOUNTANT)
        self.cashier = make_staff("recon-cashier@staff.dev", role=User.Role.CASHIER)
        self.today = hotel_today()
        self.room_type = make_room_type("Recon suite", price="20000.00")

    # ------------------------------------------------------------------ helpers
    def _in_house_stay(self, number="801", *, folio_status=Folio.Status.OPEN, booking_status=None):
        room = make_room(self.room_type, number)
        booking = make_booking(
            make_guest(f"{number}@example.com"), self.room_type, rooms=[room],
            check_in=self.today - timedelta(days=1), check_out=self.today + timedelta(days=1),
            total="20000.00",
        )
        if booking_status:
            booking.status = booking_status
            booking.save(update_fields=["status", "updated_at"])
        stay = Stay.objects.create(
            reference=f"STY-{booking.booking_reference}", booking=booking, guest=booking.guest,
            status=Stay.Status.IN_HOUSE, expected_arrival=booking.check_in,
            expected_departure=booking.check_out,
        )
        StayRoom.objects.create(stay=stay, room=room, assigned_by=self.manager)
        folio, _ = get_or_create_main_folio_for_stay(stay=stay, actor=self.manager)
        if folio_status != Folio.Status.OPEN:
            folio.status = folio_status
            folio.save(update_fields=["status", "updated_at"])
        return stay, folio, room

    def _checks(self, **params):
        query = f"?date={self.today.isoformat()}"
        for key, value in params.items():
            query += f"&{key}={value}"
        return self.client.get(CHECKS_URL + query)

    def _by_type(self, payload, check_type):
        return [row for row in payload["checks"] if row["type"] == check_type]

    # ---------------------------------------------------- count and retrieval
    def test_the_count_is_split_by_subject_and_cash_is_not_called_room_or_billing(self):
        session = open_cash_session(cashier=self.cashier, actor=self.cashier, opening_float="500.00")
        close_cash_session(
            cash_session=session, cashier=self.cashier, counted_cash=Decimal("450.00"),
        )
        # A room held by a booking the system says is already checked in, with no
        # stay: the genuine room discrepancy.
        room = make_room(self.room_type, "802")
        make_booking(
            make_guest("checked-in-no-stay@example.com"), self.room_type, rooms=[room],
            check_in=self.today - timedelta(days=1), check_out=self.today + timedelta(days=1),
            status="CHECKED_IN", total="20000.00",
        )
        self.auth(self.manager)
        response = self._checks()
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()["data"]
        summary = payload["summary"]
        # One drawer variance, one room check. The room/billing figure the
        # dashboard shows must never include the drawer.
        self.assertEqual(summary["unresolved_cash_drawer"], 1)
        self.assertEqual(summary["unresolved_room_and_billing"], 1)
        self.assertEqual(summary["unresolved"], 2)
        self.assertEqual(
            summary["unresolved_room_and_billing"],
            len([c for c in payload["checks"] if c["category"] in {"ROOM", "BILLING"} and c["severity"] == "ERROR"]),
        )
        drawer = self._by_type(payload, "CASH_VARIANCE")[0]
        self.assertEqual(drawer["category"], "CASH_DRAWER")
        self.assertEqual(drawer["amount"], "-50.00")
        self.assertTrue(drawer["why_flagged"])
        self.assertTrue(drawer["recommended_action"])
        self.assertEqual(drawer["status"], "OPEN")
        room_check = self._by_type(payload, "ROOM_OCCUPIED_NOT_IN_HOUSE")[0]
        self.assertEqual(room_check["room"], "802")
        self.assertIn("no in-house stay", room_check["why_flagged"])

    def test_a_pending_arrival_and_a_folio_on_hold_are_not_reported_as_discrepancies(self):
        # A confirmed booking arriving tonight whose guest has not reached the
        # desk: the old code flagged this as OCCUPIED_NOT_IN_HOUSE and inflated
        # the "unresolved" number every afternoon.
        room = make_room(self.room_type, "803")
        make_booking(
            make_guest("arriving@example.com"), self.room_type, rooms=[room],
            check_in=self.today, check_out=self.today + timedelta(days=2), total="40000.00",
        )
        # An in-house stay whose folio is ON_HOLD still HAS a folio; the old
        # query only looked for OPEN folios and produced IN_HOUSE_NO_FOLIO.
        self._in_house_stay("804", folio_status=Folio.Status.ON_HOLD)
        self.auth(self.manager)
        payload = self._checks().json()["data"]
        self.assertEqual(self._by_type(payload, "ROOM_OCCUPIED_NOT_IN_HOUSE"), [])
        self.assertEqual(self._by_type(payload, "IN_HOUSE_NO_FOLIO"), [])
        arrival = self._by_type(payload, "ROOM_ARRIVAL_NOT_CHECKED_IN")
        self.assertEqual(len(arrival), 1)
        self.assertEqual(arrival[0]["severity"], "INFO")
        self.assertEqual(payload["summary"]["unresolved"], 0)
        self.assertEqual(payload["summary"]["informational"], 1)
        # The on-hold folio is still surfaced, as its own billing condition.
        self.assertEqual(len(self._by_type(payload, "FOLIO_ON_HOLD")), 1)

    def test_night_audit_keeps_its_shape_and_gains_the_drilldown(self):
        session = open_cash_session(cashier=self.cashier, actor=self.cashier, opening_float="500.00")
        close_cash_session(cash_session=session, cashier=self.cashier, counted_cash=Decimal("490.00"))
        self._in_house_stay("805")
        self.auth(self.manager)
        data = self.client.get(f"/api/admin/finance/reports/night-audit/?date={self.today.isoformat()}").json()["data"]
        # The historic keys every caller already reads still exist...
        self.assertIn("discrepancies", data)
        self.assertIn("cash_drawers", data)
        self.assertIn("unbilled_in_house_folios", data)
        self.assertTrue(any(row["type"] == "CASH_VARIANCE" for row in data["discrepancies"]))
        # ...and the pack now names the subjects separately.
        self.assertEqual(data["check_summary"]["unresolved_cash_drawer"], 1)
        self.assertEqual(data["check_summary"]["unresolved_room_and_billing"], 0)
        self.assertEqual(data["in_house_count"], 1)

    # ------------------------------------------------------------- permissions
    def test_a_cashier_cannot_read_the_pack(self):
        self.auth(self.cashier)
        self.assertEqual(self._checks().status_code, 403)

    def test_a_reader_without_financial_visibility_cannot_record_a_review(self):
        self._in_house_stay("806", folio_status=Folio.Status.ON_HOLD)
        self.auth(self.manager)
        check = self._by_type(self._checks().json()["data"], "FOLIO_ON_HOLD")[0]
        self.auth(make_staff("recon-housekeeper@staff.dev", role=User.Role.HOUSEKEEPER))
        response = self.client.post(
            f"/api/admin/finance/reconciliation/checks/{check['check_id']}/review/",
            {"decision": "ACKNOWLEDGED", "note": "Looked at the folio.", "business_date": self.today.isoformat()},
            format="json",
        )
        self.assertEqual(response.status_code, 403, response.content)

    # ---------------------------------------------------------------- decisions
    def test_acknowledging_keeps_the_check_open_and_records_who_looked(self):
        self._in_house_stay("807", folio_status=Folio.Status.ON_HOLD)
        self.auth(self.accountant)
        check = self._by_type(self._checks().json()["data"], "FOLIO_ON_HOLD")[0]
        response = self.client.post(
            f"/api/admin/finance/reconciliation/checks/{check['check_id']}/review/",
            {"decision": "ACKNOWLEDGED", "note": "Guest negotiated a late settlement; watching it.",
             "business_date": self.today.isoformat()},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        payload = response.json()["data"]
        self.assertEqual(payload["check"]["status"], "ACKNOWLEDGED")
        self.assertEqual(payload["check"]["reviewed_by"], self.accountant.email)
        self.assertEqual(payload["summary"]["acknowledged"], 1)
        # Still an open condition: acknowledging is not resolving.
        self.assertEqual(payload["summary"]["unresolved"], 0)  # warning, not an ERROR
        review = ReconciliationCheckReview.objects.get(check_id=check["check_id"])
        self.assertEqual(review.decision, "ACKNOWLEDGED")
        self.assertTrue(AuditLog.objects.filter(action="RECONCILIATION_CHECK_REVIEWED").exists())

    def test_dismissing_requires_a_reason_and_is_reversible(self):
        self._in_house_stay("808", folio_status=Folio.Status.ON_HOLD)
        self.auth(self.manager)
        check = self._by_type(self._checks().json()["data"], "FOLIO_ON_HOLD")[0]
        url = f"/api/admin/finance/reconciliation/checks/{check['check_id']}/review/"
        short = self.client.post(url, {"decision": "DISMISSED", "note": "ok"}, format="json")
        self.assertEqual(short.status_code, 400, short.content)
        self.assertEqual(ReconciliationCheckReview.objects.count(), 0)

        dismissed = self.client.post(
            url, {"decision": "DISMISSED", "note": "Folio held deliberately until the corporate PO clears.",
                  "business_date": self.today.isoformat()},
            format="json",
        )
        self.assertEqual(dismissed.status_code, 201, dismissed.content)
        self.assertEqual(dismissed.json()["data"]["check"]["status"], "DISMISSED")
        self.assertEqual(dismissed.json()["data"]["summary"]["dismissed"], 1)

        # A later acknowledgement re-opens it; the dismissal is still on record.
        reopened = self.client.post(
            url, {"decision": "ACKNOWLEDGED", "note": "The PO cleared but the folio is still on hold.",
                  "business_date": self.today.isoformat()},
            format="json",
        )
        self.assertEqual(reopened.status_code, 201, reopened.content)
        self.assertEqual(reopened.json()["data"]["check"]["status"], "ACKNOWLEDGED")
        self.assertEqual(ReconciliationCheckReview.objects.filter(check_id=check["check_id"]).count(), 2)

    def test_a_review_never_edits_the_records_it_is_about(self):
        session = open_cash_session(cashier=self.cashier, actor=self.cashier, opening_float="500.00")
        close_cash_session(cash_session=session, cashier=self.cashier, counted_cash=Decimal("450.00"))
        self.auth(self.manager)
        check = self._by_type(self._checks().json()["data"], "CASH_VARIANCE")[0]
        before = CashSession.objects.get(pk=session.pk)
        self.client.post(
            f"/api/admin/finance/reconciliation/checks/{check['check_id']}/review/",
            {"decision": "ESCALATED", "note": "Handing to the accounts manager with the till slips.",
             "business_date": self.today.isoformat()},
            format="json",
        )
        after = CashSession.objects.get(pk=session.pk)
        self.assertEqual(after.counted_cash, before.counted_cash)
        self.assertEqual(after.variance, before.variance)
        self.assertEqual(after.status, before.status)

    def test_the_count_follows_the_underlying_status(self):
        stay, folio, room = self._in_house_stay("809", folio_status=Folio.Status.ON_HOLD)
        self.auth(self.manager)
        payload = self._checks().json()["data"]
        check = self._by_type(payload, "FOLIO_ON_HOLD")[0]
        self.client.post(
            f"/api/admin/finance/reconciliation/checks/{check['check_id']}/review/",
            {"decision": "ACKNOWLEDGED", "note": "Releasing the hold at the desk now.",
             "business_date": self.today.isoformat()},
            format="json",
        )
        # The desk releases the hold: the condition no longer holds.
        folio.status = Folio.Status.OPEN
        folio.save(update_fields=["status", "updated_at"])
        payload = self._checks().json()["data"]
        self.assertEqual(self._by_type(payload, "FOLIO_ON_HOLD"), [])
        self.assertEqual(payload["summary"]["acknowledged"], 0)
        resolved = [row for row in payload["resolved_reviews"] if row["check_id"] == check["check_id"]]
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["status"], "RESOLVED")
        self.assertEqual(payload["summary"]["resolved_since_review"], 1)
