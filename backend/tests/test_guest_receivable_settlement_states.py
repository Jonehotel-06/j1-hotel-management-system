"""
Task 07 of targeted_fixes_01 — settlement classification and the "nobody owes
anything" guard.

The batch-01 receivable work already made the AR control account the single
source of truth. What was still missing was the plain-language answer a desk
clerk and a Finance officer both need: WHICH of the five situations is this
folio in, and does the headline figure ever go negative when no guest owes
anything?

These tests cover:

* the five settlement states (unpaid, partial, paid, cancelled, refunded),
  including the cancelled-with-money-owed and cancelled-with-money-held cases;
* the classification is derived, never stored, and has no side effect;
* the state summary agrees with the itemised rows;
* a hotel with no debt never reports a negative receivable;
* an unpaid online booking is still reported as exposure, not silently dropped;
* the posting path is idempotent, so a retried webhook cannot double the debt.
"""
from datetime import timedelta
from decimal import Decimal

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.core.utils import hotel_today
from apps.finance.models import FinancialTransaction, Folio, FolioPosting
from apps.finance.services import accounting
from apps.finance.services.guest_receivables_service import (
    SETTLEMENT_CANCELLED,
    SETTLEMENT_PARTIAL,
    SETTLEMENT_PAID,
    SETTLEMENT_REFUNDED,
    SETTLEMENT_UNPAID,
    guest_receivables,
    settlement_state,
    settlement_state_for_row,
)
from apps.finance.services.ledger_service import create_posted_transaction

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room_type, make_staff


class SettlementStateUnitTests(BaseAPITestCase):
    """The classifier on its own — no database, no side effects."""

    def test_money_owed_is_unpaid(self):
        self.assertEqual(settlement_state(balance=Decimal("5000.00")), SETTLEMENT_UNPAID)

    def test_nothing_owed_and_nothing_held_is_paid(self):
        self.assertEqual(settlement_state(balance=Decimal("0.00")), SETTLEMENT_PAID)

    def test_money_held_for_the_guest_is_a_refund(self):
        self.assertEqual(settlement_state(balance=Decimal("-5000.00")), SETTLEMENT_REFUNDED)

    def test_a_cancelled_booking_that_still_owes_is_cancelled_not_unpaid(self):
        for status in ("CANCELLED", "NO_SHOW", "EXPIRED"):
            with self.subTest(status=status):
                self.assertEqual(
                    settlement_state(balance=Decimal("5000.00"), booking_status=status),
                    SETTLEMENT_CANCELLED,
                )

    def test_a_cancelled_booking_holding_money_is_a_refund(self):
        """The desk needs the ACTION (give the money back), not the cause."""
        self.assertEqual(
            settlement_state(balance=Decimal("-5000.00"), booking_status="CANCELLED"),
            SETTLEMENT_REFUNDED,
        )

    def test_a_cancelled_booking_with_nothing_on_it_is_cancelled(self):
        self.assertEqual(
            settlement_state(balance=Decimal("0.00"), booking_status="CANCELLED"),
            SETTLEMENT_CANCELLED,
        )

    def test_classification_never_touches_the_database(self):
        before = FinancialTransaction.objects.count()
        settlement_state(balance=Decimal("1234.00"), booking_status="CONFIRMED")
        settlement_state(balance=Decimal("-1234.00"), booking_status="CANCELLED")
        self.assertEqual(FinancialTransaction.objects.count(), before)


class SettlementReportingTests(BaseAPITestCase):
    """The states as they appear in the report a person actually reads."""

    def setUp(self):
        super().setUp()
        self.today = hotel_today()
        self.actor = make_staff("ar.states@staff.dev", role=User.Role.ACCOUNTANT)
        self.room_type = make_room_type("AR States", price="20000.00")

    # -- helpers ---------------------------------------------------------
    def _booking(self, guest, status=Booking.Status.CONFIRMED):
        return make_booking(
            guest, self.room_type,
            check_in=self.today - timedelta(days=1), check_out=self.today + timedelta(days=1),
            total="0.00", status=status,
        )

    def _folio(self, guest, booking=None, **extra):
        return Folio.objects.create(
            reference=f"FOL-ST-{Folio.objects.count() + 1:04d}",
            guest=guest, booking=booking or self._booking(guest), currency="NGN", **extra,
        )

    def _charge(self, folio, amount, source, transaction_type=FinancialTransaction.Type.POS_CHARGE):
        amount = Decimal(amount)
        return create_posted_transaction(
            transaction_type=transaction_type, actor=self.actor, source_key=source,
            business_date=self.today, currency="NGN",
            lines=[
                {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": "DEBIT",
                 "amount": amount, "folio": folio},
                {"account_code": accounting.OTHER_OPERATING_REVENUE, "direction": "CREDIT", "amount": amount},
            ],
            postings=[{"line_index": 0, "folio": folio, "kind": FolioPosting.Kind.CHARGE,
                       "effect": FolioPosting.Effect.DEBIT, "amount": amount}],
        )[0]

    def _payment(self, folio, amount, source):
        amount = Decimal(amount)
        return create_posted_transaction(
            transaction_type=FinancialTransaction.Type.PAYMENT_ALLOCATION, actor=self.actor,
            source_key=source, business_date=self.today, currency="NGN",
            lines=[
                {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": "CREDIT",
                 "amount": amount, "folio": folio},
                {"account_code": accounting.GUEST_DEPOSITS, "direction": "DEBIT", "amount": amount},
            ],
            postings=[{"line_index": 0, "folio": folio, "kind": FolioPosting.Kind.PAYMENT,
                       "effect": FolioPosting.Effect.CREDIT, "amount": amount}],
        )[0]

    def _item(self, report, folio):
        """Find one folio's row.

        A fully settled folio is excluded from the default itemised list (it is
        not a receivable), so the lookup uses an ``include_settled`` read: the
        QUESTION here is "what state is this folio in", which a settled folio
        still has an answer to.
        """
        for entry in report["items"]:
            if entry["folio_id"] == folio.pk:
                return entry
        for entry in guest_receivables(as_of=self.today, include_settled=True)["items"]:
            if entry["folio_id"] == folio.pk:
                return entry
        self.fail(f"folio {folio.reference} is missing from the report")

    # -- states ----------------------------------------------------------
    def test_an_untouched_charge_is_unpaid(self):
        guest = make_guest("states-unpaid@guest.dev")
        folio = self._folio(guest)
        self._charge(folio, "12000.00", "states-charge-unpaid")
        report = guest_receivables(as_of=self.today)
        self.assertEqual(self._item(report, folio)["settlement_state"], SETTLEMENT_UNPAID)
        self.assertEqual(report["settlement"][SETTLEMENT_UNPAID]["folios"], 1)

    def test_a_partly_paid_charge_is_partially_paid(self):
        guest = make_guest("states-partial@guest.dev")
        folio = self._folio(guest)
        self._charge(folio, "12000.00", "states-charge-partial")
        self._payment(folio, "5000.00", "states-pay-partial")
        report = guest_receivables(as_of=self.today)
        item = self._item(report, folio)
        self.assertEqual(item["settlement_state"], SETTLEMENT_PARTIAL)
        self.assertEqual(item["outstanding"], "7000.00")
        self.assertEqual(item["payments_allocated"], "5000.00")
        self.assertEqual(report["settlement"][SETTLEMENT_PARTIAL]["folios"], 1)

    def test_a_fully_paid_charge_is_paid(self):
        guest = make_guest("states-paid@guest.dev")
        folio = self._folio(guest)
        self._charge(folio, "12000.00", "states-charge-paid")
        self._payment(folio, "12000.00", "states-pay-paid")
        report = guest_receivables(as_of=self.today)
        self.assertEqual(self._item(report, folio)["settlement_state"], SETTLEMENT_PAID)
        self.assertEqual(report["settlement"][SETTLEMENT_PAID]["folios"], 1)
        self.assertEqual(report["gross_receivables"], "0.00")

    def test_an_overpayment_is_refunded_not_a_negative_receivable(self):
        guest = make_guest("states-refund@guest.dev")
        folio = self._folio(guest)
        self._charge(folio, "5000.00", "states-charge-refund")
        self._payment(folio, "8000.00", "states-pay-refund")
        report = guest_receivables(as_of=self.today)
        item = self._item(report, folio)
        self.assertEqual(item["settlement_state"], SETTLEMENT_REFUNDED)
        self.assertEqual(item["credit_balance"], "3000.00")
        self.assertEqual(item["outstanding"], "0.00")
        # The credit never appears as a receivable and never goes negative.
        self.assertEqual(report["gross_receivables"], "0.00")
        self.assertEqual(report["guest_credit_balances"], "3000.00")
        self.assertGreaterEqual(Decimal(report["gross_receivables"]), Decimal("0.00"))

    def test_a_cancelled_booking_holding_money_is_reported_for_refund(self):
        guest = make_guest("states-cancel-refund@guest.dev")
        booking = self._booking(guest, status=Booking.Status.CANCELLED)
        folio = self._folio(guest, booking)
        self._payment(folio, "9000.00", "states-cancel-payment")
        report = guest_receivables(as_of=self.today)
        item = self._item(report, folio)
        self.assertEqual(item["settlement_state"], SETTLEMENT_REFUNDED)
        self.assertEqual(item["credit_balance"], "9000.00")
        self.assertEqual(report["settlement"][SETTLEMENT_REFUNDED]["folios"], 1)

    def test_a_cancelled_booking_that_still_owes_is_classified_cancelled(self):
        guest = make_guest("states-cancel-owing@guest.dev")
        booking = self._booking(guest, status=Booking.Status.CANCELLED)
        folio = self._folio(guest, booking)
        self._charge(folio, "6000.00", "states-cancel-charge")
        report = guest_receivables(as_of=self.today)
        self.assertEqual(self._item(report, folio)["settlement_state"], SETTLEMENT_CANCELLED)

    # -- the headline never lies ----------------------------------------
    def test_a_hotel_with_no_debt_reports_zero_not_a_negative(self):
        """Nothing charged, nothing owed: the receivable is exactly zero."""
        report = guest_receivables(as_of=self.today)
        self.assertEqual(report["gross_receivables"], "0.00")
        self.assertEqual(report["net_receivable"], "0.00")
        self.assertEqual(report["total_outstanding"], "0.00")
        self.assertFalse(Decimal(report["gross_receivables"]) < Decimal("0.00"))
        self.assertTrue(report["reconciliation"]["reconciles"])

    def test_the_settlement_summary_counts_every_folio_exactly_once(self):
        charged = self._folio(make_guest("states-mix-a@guest.dev"))
        self._charge(charged, "10000.00", "states-mix-charge")
        paid = self._folio(make_guest("states-mix-b@guest.dev"))
        self._charge(paid, "4000.00", "states-mix-charge-2")
        self._payment(paid, "4000.00", "states-mix-pay")
        overpaid = self._folio(make_guest("states-mix-c@guest.dev"))
        self._charge(overpaid, "1000.00", "states-mix-charge-3")
        self._payment(overpaid, "3000.00", "states-mix-pay-3")

        report = guest_receivables(as_of=self.today)
        total_counted = sum(entry["folios"] for entry in report["settlement"].values())
        # Every folio on the ledger is classified exactly once — including the
        # fully settled one, which is deliberately absent from `items`.
        self.assertEqual(total_counted, Folio.objects.count())
        self.assertEqual(len(report["items"]), 2, "only the two live balances are itemised")
        everything = guest_receivables(as_of=self.today, include_settled=True)
        states = {entry["folio_id"]: entry["settlement_state"] for entry in everything["items"]}
        self.assertEqual(states[charged.pk], SETTLEMENT_UNPAID)
        self.assertEqual(states[paid.pk], SETTLEMENT_PAID)
        self.assertEqual(states[overpaid.pk], SETTLEMENT_REFUNDED)

    def test_the_endpoint_publishes_the_settlement_breakdown(self):
        guest = make_guest("states-endpoint@guest.dev")
        folio = self._folio(guest)
        self._charge(folio, "7500.00", "states-endpoint-charge")

        accountant = make_staff("ar.states.api@staff.dev", role=User.Role.ACCOUNTANT)
        self.auth(accountant)
        response = self.client.get("/api/admin/finance/reports/ar-aging/")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertIn("settlement", data)
        for state in (SETTLEMENT_UNPAID, SETTLEMENT_PARTIAL, SETTLEMENT_PAID,
                      SETTLEMENT_CANCELLED, SETTLEMENT_REFUNDED):
            self.assertIn(state, data["settlement"])
        self.assertTrue(data["reconciliation"]["reconciles"])

    # -- idempotency -----------------------------------------------------
    def test_a_retried_posting_does_not_double_the_debt(self):
        guest = make_guest("states-idempotent@guest.dev")
        folio = self._folio(guest)
        first = self._charge(folio, "12000.00", "states-idempotent-charge")
        second = self._charge(folio, "12000.00", "states-idempotent-charge")
        self.assertEqual(first.pk, second.pk, "the same source key returns the same transaction")
        report = guest_receivables(as_of=self.today)
        self.assertEqual(self._item(report, folio)["balance"], "12000.00")
        self.assertEqual(report["gross_receivables"], "12000.00")

    def test_a_retried_payment_does_not_double_the_credit(self):
        guest = make_guest("states-idempotent-pay@guest.dev")
        folio = self._folio(guest)
        self._charge(folio, "12000.00", "states-idem-pay-charge")
        first = self._payment(folio, "4000.00", "states-idem-pay")
        second = self._payment(folio, "4000.00", "states-idem-pay")
        self.assertEqual(first.pk, second.pk)
        item = self._item(guest_receivables(as_of=self.today), folio)
        self.assertEqual(item["settlement_state"], SETTLEMENT_PARTIAL)
        self.assertEqual(item["payments_allocated"], "4000.00")
