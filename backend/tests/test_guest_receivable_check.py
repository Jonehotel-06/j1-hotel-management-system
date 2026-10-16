# tests/test_guest_receivable_check.py
"""The guest-receivable report behind the balance-sheet question.

The balance sheet can show **Guest receivables** as a credit (negative asset).
That is arithmetic, not a display fault: the account is debited by guest charges
(accommodation nights, POS sales, room-charge laundry) and credited by money
applied to guest folios, and a folio posting ledger is derived from those same
receivable lines. `manage.py guest_receivable_check` explains the balance folio
by folio and states the approved correction routes.

These tests pin three things:

1. an over-applied folio really is what drives the credit balance (the same
   collection/allocation path staff use produces folio balance -100.00);
2. the command reports that folio, the movement split by transaction type and
   both correction routes — including through ``--json``;
3. the command changes nothing: no transaction, line or folio posting is
   created, and ``--fail-on-credit`` is a report-only exit code so an operator
   can wire it into a check without any write-on-failure behaviour.
"""
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import FinancialLine, FinancialTransaction, FolioPosting
from apps.finance.services import accounting
from apps.finance.services.folio_service import (
    folio_balance_queryset,
    get_or_create_main_folio_for_stay,
)
from apps.finance.services.ledger_service import create_posted_transaction
from apps.payments.models import Payment
from apps.stays.models import Stay

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room_type, make_staff


class GuestReceivableCheckTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.actor = make_staff("receivable-desk@staff.dev", role=User.Role.ACCOUNTS_MANAGER)
        room_type = make_room_type("Receivable suite", price="25000.00")
        booking = make_booking(
            make_guest("receivable-guest@example.com"),
            room_type,
            check_in=hotel_today() - timedelta(days=3),
            check_out=hotel_today() + timedelta(days=1),
            total="100000.00",
            amount_paid="0.00",
        )
        self.stay = Stay.objects.create(
            reference=f"STY-{booking.booking_reference}",
            booking=booking,
            guest=booking.guest,
            status=Stay.Status.IN_HOUSE,
            expected_arrival=booking.check_in,
            expected_departure=booking.check_out,
        )
        self.folio, _ = get_or_create_main_folio_for_stay(stay=self.stay, actor=self.actor)

    # ------------------------------------------------------------------ setup
    def _post_charge(self, amount, source_key="receivable:charge:1"):
        return create_posted_transaction(
            transaction_type=FinancialTransaction.Type.ACCOMMODATION_CHARGE,
            source_key=source_key,
            actor=self.actor,
            source_reference=self.stay.booking.booking_reference,
            lines=[
                {"account_code": accounting.ACCOUNTS_RECEIVABLE,
                 "direction": FinancialLine.Direction.DEBIT, "amount": amount, "folio": self.folio},
                {"account_code": accounting.ACCOMMODATION_REVENUE,
                 "direction": FinancialLine.Direction.CREDIT, "amount": amount},
            ],
            # The real nightly accrual also writes the guest-visible statement
            # item; folio postings derive from the receivable line, never from
            # a separate calculation of their own.
            postings=[{
                "line_index": 0,
                "folio": self.folio,
                "kind": FolioPosting.Kind.CHARGE,
                "effect": FolioPosting.Effect.DEBIT,
                "amount": amount,
                "description": f"Accommodation charge {amount}",
                "source_reference": self.stay.booking.booking_reference,
            }],
        )

    def _collect_and_allocate(self, amount, reference="J1P-RECEIVABLE-1"):
        """Money arrives and is applied to the folio — the staff-facing path."""
        from apps.finance.services.payment_collection_service import (
            apply_payment_to_folio,
            ensure_payment_collection,
        )

        payment = Payment.objects.create(
            booking=self.stay.booking,
            user=getattr(self.stay.booking, "user", None) or self.actor,
            reference=reference,
            provider=Payment.Provider.CASH,
            amount=Decimal(amount),
            currency="NGN",
            status=Payment.Status.SUCCESS,
        )
        ensure_payment_collection(payment=payment, actor=self.actor)
        apply_payment_to_folio(payment=payment, folio=self.folio, actor=self.actor)
        return payment

    def _run(self, *args):
        out = StringIO()
        call_command("guest_receivable_check", *args, stdout=out)
        return out.getvalue()

    # ------------------------------------------------------------------ tests
    def test_a_payment_applied_beyond_the_charges_is_what_makes_the_receivable_negative(self):
        self._post_charge("50000.00")
        self._collect_and_allocate("50000.00", reference="J1P-RECEIVABLE-EVEN")
        # Even money in and out: the folio, and therefore the receivable, is clear.
        self.assertEqual(folio_balance_queryset().get(pk=self.folio.pk).balance, Decimal("0.00"))

        self._collect_and_allocate("30000.00", reference="J1P-RECEIVABLE-AHEAD")
        # A payment applied to a folio whose stay has not yet consumed the nights
        # it paid for: the folio is 30,000 in credit, which is the receivable.
        self.assertEqual(folio_balance_queryset().get(pk=self.folio.pk).balance, Decimal("-30000.00"))

    def test_the_report_names_the_credit_folios_the_movement_split_and_the_correction_routes(self):
        self._collect_and_allocate("30000.00", reference="J1P-RECEIVABLE-CREDIT")

        output = self._run()
        self.assertIn("Guest receivables", output)
        self.assertIn("CREDIT-NORMAL", output)
        self.assertIn("PAYMENT_ALLOCATION", output)
        self.assertIn(self.folio.reference, output)
        # Both approved routes are stated, and neither edits posted evidence.
        self.assertIn("refund request/approval workflow", output)
        self.assertIn(f"Credit {accounting.GUEST_DEPOSITS}", output)
        self.assertIn("never edited", output)

    def test_the_json_report_carries_the_balance_and_the_folio_detail(self):
        self._collect_and_allocate("30000.00", reference="J1P-RECEIVABLE-JSON")
        out = StringIO()
        call_command("guest_receivable_check", "--json", stdout=out)
        import json

        payload = json.loads(out.getvalue())
        self.assertEqual(payload["account_code"], accounting.ACCOUNTS_RECEIVABLE)
        self.assertTrue(payload["credit_normal"])
        self.assertEqual(Decimal(payload["balance"]), Decimal("-30000.00"))
        self.assertEqual(payload["folios_in_credit"], 1)
        self.assertEqual(payload["folios"][0]["reference"], self.folio.reference)
        self.assertEqual(payload["folio_balance_total"], payload["balance"])

    def test_a_single_folio_can_be_drilled_down_to_its_postings(self):
        self._collect_and_allocate("30000.00", reference="J1P-RECEIVABLE-DRILL")
        output = self._run("--folio", self.folio.reference)
        self.assertIn(self.folio.reference, output)
        # The allocation that over-applied the folio is named with its source
        # reference, so the debit/credit pairing can be attributed.
        self.assertIn("PAYMENT", output)
        self.assertIn("J1P-RECEIVABLE-DRILL", output)

    def test_the_command_writes_nothing(self):
        self._post_charge("20000.00")
        self._collect_and_allocate("25000.00", reference="J1P-RECEIVABLE-READONLY")
        before = (
            FinancialTransaction.objects.count(),
            FinancialLine.objects.count(),
            FolioPosting.objects.count(),
        )
        self._run()
        self._run("--json")
        self._run("--folio", self.folio.reference)
        after = (
            FinancialTransaction.objects.count(),
            FinancialLine.objects.count(),
            FolioPosting.objects.count(),
        )
        self.assertEqual(before, after)
        # And the account itself did not move.
        self.assertTrue(self._run().count("CREDIT-NORMAL") >= 1)

    def test_fail_on_credit_is_a_report_only_exit_code(self):
        self._collect_and_allocate("10000.00", reference="J1P-RECEIVABLE-FAIL")
        before = FinancialTransaction.objects.count()
        with self.assertRaises(CommandError):
            call_command("guest_receivable_check", "--fail-on-credit", stdout=StringIO())
        self.assertEqual(FinancialTransaction.objects.count(), before)

    def test_receivable_lines_in_another_currency_are_reported_not_blended(self):
        """The statements report one functional currency; a mixed account says so."""
        from apps.finance.services.ledger_service import create_posted_transaction

        self._post_charge("10000.00", source_key="receivable:charge:ngn")
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.OTHER_CHARGE,
            source_key="receivable:charge:usd",
            actor=self.actor,
            source_reference=self.stay.booking.booking_reference,
            currency="USD",
            lines=[
                {"account_code": accounting.ACCOUNTS_RECEIVABLE,
                 "direction": FinancialLine.Direction.DEBIT, "amount": "250.00"},
                {"account_code": accounting.OTHER_OPERATING_REVENUE,
                 "direction": FinancialLine.Direction.CREDIT, "amount": "250.00"},
            ],
        )
        out = StringIO()
        call_command("guest_receivable_check", "--json", stdout=out)
        import json

        payload = json.loads(out.getvalue())
        self.assertTrue(payload["mixed_currency"])
        self.assertEqual([row["currency"] for row in payload["currencies"]], ["NGN", "USD"])
        self.assertTrue(any("more than one currency" in item for item in payload["warnings"]))
        self.assertIn("WARNING", self._run())

    def test_an_unknown_folio_reference_is_refused(self):
        with self.assertRaises(CommandError):
            call_command("guest_receivable_check", "--folio", "FOL-DOES-NOT-EXIST", stdout=StringIO())

    def test_a_clean_ledger_reports_no_credit_folios(self):
        self._post_charge("40000.00", source_key="receivable:charge:clean")
        self._collect_and_allocate("40000.00", reference="J1P-RECEIVABLE-CLEAN")
        out = StringIO()
        call_command("guest_receivable_check", "--json", stdout=out)
        import json

        payload = json.loads(out.getvalue())
        self.assertFalse(payload["credit_normal"])
        self.assertEqual(Decimal(payload["balance"]), Decimal("0.00"))
        self.assertEqual(payload["folios_in_credit"], 0)
        self.assertIn("Every folio's charges", self._run())
