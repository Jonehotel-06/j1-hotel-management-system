"""Guest receivable regression suite (batch 01, task 7).

Each test is a situation the old calculation got wrong. The common thread is
the identity the report now proves on every call::

    ASSET.GUEST_RECEIVABLE balance
        = Σ folio balances + receivable lines with no folio
    Σ folio balances
        = gross receivables − guest credit balances

If any scenario broke that identity the report would say so, so every test
asserts it as well as the figure under examination.
"""
from datetime import timedelta
from decimal import Decimal

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.core.utils import hotel_today
from apps.finance.models import FinancialLine, FinancialTransaction, Folio, FolioPosting
from apps.finance.services import accounting
from apps.finance.services.guest_receivables_service import (
    control_account_balance,
    folio_receivable_detail,
    guest_receivables,
    unapplied_guest_deposits,
)
from apps.finance.services.ledger_service import create_posted_transaction

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type, make_staff


class GuestReceivableScenarioTests(BaseAPITestCase):
    """Realistic guest situations, each proved against the control account."""

    def setUp(self):
        super().setUp()
        self.today = hotel_today()
        self.actor = make_staff("ar.batch01@staff.dev", role=User.Role.ACCOUNTANT)
        self.room_type = make_room_type("AR Suite", price="20000.00")

    # -- helpers ---------------------------------------------------------
    def _folio(self, guest, booking=None, reference=None, currency="NGN", **extra):
        # A folio must belong to a stay or a booking (DB check constraint), so
        # a scenario that does not care about the reservation still gets one.
        if booking is None:
            booking = make_booking(
                guest, self.room_type, check_in=self.today - timedelta(days=1),
                check_out=self.today + timedelta(days=1), total="0.00",
            )
        return Folio.objects.create(
            reference=reference or f"FOL-AR-{Folio.objects.count() + 1:04d}",
            guest=guest, booking=booking, currency=currency, **extra,
        )

    def _post(self, *, kind, folio, amount, transaction_type, settles="",
              business_date=None, currency="NGN", counter=None, source=None):
        """Post one balanced guest-ledger movement and mirror it on the folio."""
        amount = Decimal(amount)
        debit = kind == "DEBIT"
        counter = counter or (
            accounting.OTHER_OPERATING_REVENUE if debit else accounting.GUEST_DEPOSITS)
        ar_line = {
            "account_code": accounting.ACCOUNTS_RECEIVABLE,
            "direction": "DEBIT" if debit else "CREDIT",
            "amount": amount, "folio": folio,
        }
        other_line = {
            "account_code": counter,
            "direction": "CREDIT" if debit else "DEBIT",
            "amount": amount,
        }
        source = source or f"ar-batch01:{FinancialTransaction.objects.count() + 1}"
        return create_posted_transaction(
            transaction_type=transaction_type, actor=self.actor, source_key=source,
            business_date=business_date or self.today, currency=currency,
            lines=[ar_line, other_line],
            postings=[{
                "line_index": 0, "folio": folio,
                "kind": (FolioPosting.Kind.CHARGE if debit else FolioPosting.Kind.PAYMENT),
                "effect": FolioPosting.Effect.DEBIT if debit else FolioPosting.Effect.CREDIT,
                "amount": amount, "settles": settles,
            }],
        )[0]

    def _assert_reconciles(self, report):
        proof = report["reconciliation"]
        self.assertTrue(proof["reconciles"], proof)
        self.assertEqual(proof["difference"], "0.00")
        self.assertEqual(
            Decimal(proof["control_account_balance"]),
            Decimal(report["gross_receivables"])
            - Decimal(report["guest_credit_balances"])
            + Decimal(report["unassigned_receivables"]),
        )

    # -- scenarios -------------------------------------------------------
    def test_accommodation_food_and_other_charges_less_payments(self):
        """Charges by source, payments applied, one reconciled balance."""
        guest = make_guest("ar.mixed@example.com")
        booking = make_booking(guest, self.room_type, check_in=self.today - timedelta(days=2),
                               check_out=self.today + timedelta(days=1), total="60000.00")
        folio = self._folio(guest, booking)
        self._post(kind="DEBIT", folio=folio, amount="40000.00",
                   transaction_type=FinancialTransaction.Type.ACCOMMODATION_CHARGE,
                   counter=accounting.ACCOMMODATION_REVENUE)
        self._post(kind="DEBIT", folio=folio, amount="12000.00",
                   transaction_type=FinancialTransaction.Type.POS_CHARGE,
                   counter=accounting.FOOD_BEVERAGE_REVENUE)
        self._post(kind="DEBIT", folio=folio, amount="3000.00",
                   transaction_type=FinancialTransaction.Type.OTHER_CHARGE)
        self._post(kind="CREDIT", folio=folio, amount="25000.00",
                   transaction_type=FinancialTransaction.Type.PAYMENT_ALLOCATION,
                   settles=FolioPosting.Settles.ACCOMMODATION)

        report = guest_receivables(as_of=self.today)
        row = next(item for item in report["items"] if item["folio_reference"] == folio.reference)
        self.assertEqual(row["accommodation_charges"], "40000.00")
        self.assertEqual(row["food_beverage_charges"], "12000.00")
        self.assertEqual(row["other_charges"], "3000.00")
        self.assertEqual(row["total_charges"], "55000.00")
        self.assertEqual(row["payments_allocated"], "25000.00")
        self.assertEqual(row["outstanding"], "30000.00")
        self.assertEqual(row["credit_balance"], "0.00")
        self.assertEqual(row["guest_name"], "Ada Obi")
        self.assertEqual(row["booking_reference"], booking.booking_reference)
        self.assertEqual(report["gross_receivables"], "30000.00")
        self._assert_reconciles(report)

    def test_prepaid_stay_is_a_credit_balance_not_a_negative_receivable(self):
        """A deposit applied before the nights are charged must stay visible.

        The old report clamped this at zero, which deleted the hotel's
        obligation to the guest from the only screen that showed it.
        """
        guest = make_guest("ar.prepaid@example.com")
        booking = make_booking(guest, self.room_type, check_in=self.today,
                               check_out=self.today + timedelta(days=3), total="60000.00")
        folio = self._folio(guest, booking)
        self._post(kind="CREDIT", folio=folio, amount="60000.00",
                   transaction_type=FinancialTransaction.Type.PAYMENT_ALLOCATION,
                   settles=FolioPosting.Settles.ACCOMMODATION)
        self._post(kind="DEBIT", folio=folio, amount="20000.00",
                   transaction_type=FinancialTransaction.Type.ACCOMMODATION_CHARGE,
                   counter=accounting.ACCOMMODATION_REVENUE)

        report = guest_receivables(as_of=self.today)
        row = next(item for item in report["items"] if item["folio_reference"] == folio.reference)
        self.assertEqual(row["outstanding"], "0.00")
        self.assertEqual(row["credit_balance"], "40000.00")
        self.assertEqual(row["balance"], "-40000.00")
        self.assertEqual(report["gross_receivables"], "0.00")
        self.assertEqual(report["guest_credit_balances"], "40000.00")
        self.assertEqual(report["net_receivable"], "-40000.00")
        self._assert_reconciles(report)

    def test_one_guest_in_credit_never_cancels_another_guest_s_debt(self):
        """Debit and credit balances are reported separately, never netted."""
        owing = make_guest("ar.owing@example.com")
        holding = make_guest("ar.holding@example.com")
        owing_folio = self._folio(owing)
        credit_folio = self._folio(holding)
        self._post(kind="DEBIT", folio=owing_folio, amount="50000.00",
                   transaction_type=FinancialTransaction.Type.OTHER_CHARGE)
        self._post(kind="CREDIT", folio=credit_folio, amount="50000.00",
                   transaction_type=FinancialTransaction.Type.PAYMENT_ALLOCATION)

        report = guest_receivables(as_of=self.today)
        self.assertEqual(report["gross_receivables"], "50000.00")
        self.assertEqual(report["guest_credit_balances"], "50000.00")
        self.assertEqual(report["net_receivable"], "0.00")
        self.assertEqual(report["folios_owing"], 1)
        self.assertEqual(report["folios_in_credit"], 1)
        self._assert_reconciles(report)

    def test_unallocated_payment_is_a_deposit_and_touches_no_guest_balance(self):
        """Money with no folio must not reduce anybody's receivable."""
        guest = make_guest("ar.unallocated@example.com")
        folio = self._folio(guest)
        self._post(kind="DEBIT", folio=folio, amount="15000.00",
                   transaction_type=FinancialTransaction.Type.OTHER_CHARGE)
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.PAYMENT_COLLECTION,
            actor=self.actor, source_key="ar-batch01:unallocated", business_date=self.today,
            lines=[
                {"account_code": accounting.CASH_ON_HAND, "direction": "DEBIT", "amount": Decimal("9000.00")},
                {"account_code": accounting.GUEST_DEPOSITS, "direction": "CREDIT", "amount": Decimal("9000.00")},
            ],
        )

        report = guest_receivables(as_of=self.today)
        self.assertEqual(report["gross_receivables"], "15000.00")
        self.assertEqual(report["unapplied_guest_deposits"], "9000.00")
        self.assertEqual(unapplied_guest_deposits(as_of=self.today, currency="NGN"),
                         Decimal("9000.00"))
        self._assert_reconciles(report)

    def test_discount_and_refund_move_the_balance_in_the_right_direction(self):
        guest = make_guest("ar.discount@example.com")
        folio = self._folio(guest)
        self._post(kind="DEBIT", folio=folio, amount="30000.00",
                   transaction_type=FinancialTransaction.Type.OTHER_CHARGE)
        self._post(kind="CREDIT", folio=folio, amount="5000.00",
                   transaction_type=FinancialTransaction.Type.DISCOUNT,
                   counter=accounting.DISCOUNTS_ALLOWED)
        self._post(kind="CREDIT", folio=folio, amount="20000.00",
                   transaction_type=FinancialTransaction.Type.PAYMENT_ALLOCATION)
        self._post(kind="DEBIT", folio=folio, amount="4000.00",
                   transaction_type=FinancialTransaction.Type.REFUND,
                   counter=accounting.CASH_ON_HAND)

        detail = folio_receivable_detail(folio=folio, as_of=self.today)
        self.assertEqual(detail["total_charges"], "30000.00")
        self.assertEqual(detail["discounts"], "5000.00")
        self.assertEqual(detail["payments_allocated"], "20000.00")
        self.assertEqual(detail["refunds"], "4000.00")
        self.assertEqual(detail["outstanding"], "9000.00")
        self._assert_reconciles(guest_receivables(as_of=self.today))

    def test_cancelled_booking_keeps_the_value_still_sitting_on_its_folio(self):
        """Cancelling a reservation does not erase money already charged."""
        guest = make_guest("ar.cancelled@example.com")
        booking = make_booking(guest, self.room_type, check_in=self.today - timedelta(days=5),
                               check_out=self.today - timedelta(days=3), total="40000.00",
                               status=Booking.Status.CANCELLED)
        folio = self._folio(guest, booking)
        self._post(kind="DEBIT", folio=folio, amount="7500.00",
                   transaction_type=FinancialTransaction.Type.POS_CHARGE,
                   counter=accounting.FOOD_BEVERAGE_REVENUE)

        report = guest_receivables(as_of=self.today)
        row = next(item for item in report["items"] if item["folio_reference"] == folio.reference)
        self.assertEqual(row["outstanding"], "7500.00")
        self.assertEqual(row["booking_status"], Booking.Status.CANCELLED)
        # The cancelled contract itself contributes nothing to the exposure.
        self.assertEqual(report["stay_value_not_yet_charged"], "0.00")
        self._assert_reconciles(report)

    def test_multi_folio_booking_is_counted_once_per_folio_and_once_per_booking(self):
        guest = make_guest("ar.multifolio@example.com")
        booking = make_booking(guest, self.room_type, check_in=self.today - timedelta(days=1),
                               check_out=self.today + timedelta(days=1), total="50000.00",
                               amount_paid="50000.00")
        main = self._folio(guest, booking, kind=Folio.Kind.MAIN)
        incidental = self._folio(guest, booking, kind=Folio.Kind.INCIDENTAL)
        self._post(kind="DEBIT", folio=main, amount="6000.00",
                   transaction_type=FinancialTransaction.Type.POS_CHARGE,
                   counter=accounting.FOOD_BEVERAGE_REVENUE)
        self._post(kind="DEBIT", folio=incidental, amount="2500.00",
                   transaction_type=FinancialTransaction.Type.OTHER_CHARGE)

        report = guest_receivables(as_of=self.today)
        folio_rows = [item for item in report["items"]
                      if item["booking_reference"] == booking.booking_reference]
        self.assertEqual(len(folio_rows), 2)
        roll_up = next(item for item in report["by_booking"]
                       if item["booking_reference"] == booking.booking_reference)
        self.assertEqual(roll_up["folios"], 2)
        self.assertEqual(roll_up["outstanding"], "8500.00")
        self.assertEqual(report["gross_receivables"], "8500.00")
        self._assert_reconciles(report)

    def test_fully_paid_booking_with_a_new_charge_still_shows_the_new_charge(self):
        guest = make_guest("ar.paidthencharged@example.com")
        booking = make_booking(guest, self.room_type, check_in=self.today - timedelta(days=1),
                               check_out=self.today + timedelta(days=1), total="42000.00",
                               amount_paid="42000.00")
        folio = self._folio(guest, booking)
        self._post(kind="DEBIT", folio=folio, amount="42000.00",
                   transaction_type=FinancialTransaction.Type.ACCOMMODATION_CHARGE,
                   counter=accounting.ACCOMMODATION_REVENUE)
        self._post(kind="CREDIT", folio=folio, amount="42000.00",
                   transaction_type=FinancialTransaction.Type.PAYMENT_ALLOCATION,
                   settles=FolioPosting.Settles.ACCOMMODATION)
        self._post(kind="DEBIT", folio=folio, amount="3200.00",
                   transaction_type=FinancialTransaction.Type.OTHER_CHARGE)

        report = guest_receivables(as_of=self.today)
        row = next(item for item in report["items"] if item["folio_reference"] == folio.reference)
        self.assertEqual(row["outstanding"], "3200.00")
        self.assertEqual(report["gross_receivables"], "3200.00")
        self._assert_reconciles(report)

    def test_draft_and_voided_transactions_are_not_receivables(self):
        guest = make_guest("ar.draft@example.com")
        folio = self._folio(guest)
        posted = self._post(kind="DEBIT", folio=folio, amount="10000.00",
                            transaction_type=FinancialTransaction.Type.OTHER_CHARGE)
        FinancialTransaction.objects.create(
            type=FinancialTransaction.Type.OTHER_CHARGE, status=FinancialTransaction.Status.DRAFT,
            source_key="ar-batch01:draft", business_date=self.today, currency="NGN",
        )
        draft = FinancialTransaction.objects.get(source_key="ar-batch01:draft")
        FinancialLine.objects.create(
            transaction=draft, account_code=accounting.ACCOUNTS_RECEIVABLE,
            direction=FinancialLine.Direction.DEBIT, amount=Decimal("99000.00"), folio=folio,
        )

        report = guest_receivables(as_of=self.today)
        self.assertEqual(report["gross_receivables"], "10000.00")
        self.assertEqual(control_account_balance(as_of=self.today, currency="NGN"),
                         Decimal("10000.00"))
        self.assertEqual(posted.status, FinancialTransaction.Status.POSTED)
        self._assert_reconciles(report)

    def test_a_foreign_currency_balance_is_never_published_as_naira(self):
        guest = make_guest("ar.usd@example.com")
        naira_folio = self._folio(guest, currency="NGN")
        dollar_folio = self._folio(guest, currency="USD")
        self._post(kind="DEBIT", folio=naira_folio, amount="18000.00",
                   transaction_type=FinancialTransaction.Type.OTHER_CHARGE)
        self._post(kind="DEBIT", folio=dollar_folio, amount="500.00",
                   transaction_type=FinancialTransaction.Type.OTHER_CHARGE, currency="USD")

        report = guest_receivables(as_of=self.today, currency="NGN")
        self.assertEqual(report["gross_receivables"], "18000.00")
        self.assertEqual(report["other_currency_balances"],
                         [{"currency": "USD", "balance": "500.00", "lines": 1}])
        self._assert_reconciles(report)

        usd = guest_receivables(as_of=self.today, currency="USD")
        self.assertEqual(usd["gross_receivables"], "500.00")
        self._assert_reconciles(usd)

    def test_as_of_excludes_later_postings(self):
        guest = make_guest("ar.asof@example.com")
        folio = self._folio(guest)
        self._post(kind="DEBIT", folio=folio, amount="11000.00",
                   transaction_type=FinancialTransaction.Type.OTHER_CHARGE,
                   business_date=self.today - timedelta(days=3))
        self._post(kind="DEBIT", folio=folio, amount="4000.00",
                   transaction_type=FinancialTransaction.Type.OTHER_CHARGE,
                   business_date=self.today)

        earlier = guest_receivables(as_of=self.today - timedelta(days=1))
        self.assertEqual(earlier["gross_receivables"], "11000.00")
        self._assert_reconciles(earlier)
        self.assertEqual(guest_receivables(as_of=self.today)["gross_receivables"], "15000.00")

    def test_receivable_with_no_folio_is_reported_not_dropped(self):
        """A walk-in POS charge posted without a folio still belongs to the GL."""
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.POS_CHARGE, actor=self.actor,
            source_key="ar-batch01:nofolio", business_date=self.today,
            lines=[
                {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": "DEBIT",
                 "amount": Decimal("2200.00")},
                {"account_code": accounting.FOOD_BEVERAGE_REVENUE, "direction": "CREDIT",
                 "amount": Decimal("2200.00")},
            ],
        )
        report = guest_receivables(as_of=self.today)
        self.assertEqual(report["gross_receivables"], "0.00")
        self.assertEqual(report["unassigned_receivables"], "2200.00")
        self.assertEqual(report["net_receivable"], "2200.00")
        self._assert_reconciles(report)

    def test_aging_buckets_place_each_folio_once(self):
        guest = make_guest("ar.aging@example.com")
        recent = make_booking(guest, self.room_type, check_in=self.today - timedelta(days=10),
                              check_out=self.today - timedelta(days=8), total="30000.00")
        ancient = make_booking(guest, self.room_type, check_in=self.today - timedelta(days=200),
                               check_out=self.today - timedelta(days=197), total="30000.00")
        self._post(kind="DEBIT", folio=self._folio(guest, recent), amount="5000.00",
                   transaction_type=FinancialTransaction.Type.OTHER_CHARGE)
        self._post(kind="DEBIT", folio=self._folio(guest, ancient), amount="7000.00",
                   transaction_type=FinancialTransaction.Type.OTHER_CHARGE)

        report = guest_receivables(as_of=self.today)
        self.assertEqual(report["buckets"]["days_1_30"], "5000.00")
        self.assertEqual(report["buckets"]["days_over_90"], "7000.00")
        self.assertEqual(sum(report["bucket_counts"].values()), 2)
        self.assertEqual(report["gross_receivables"], "12000.00")
        self._assert_reconciles(report)


class GuestReceivableEndpointTests(BaseAPITestCase):
    """The aging endpoint keeps its contract and gains the reconciliation."""

    def setUp(self):
        super().setUp()
        self.today = hotel_today()
        self.manager = make_staff("ar.manager@staff.dev", role=User.Role.MANAGER)

    def test_endpoint_returns_reconciled_payload(self):
        self.auth(self.manager)
        response = self.client.get("/api/admin/finance/reports/ar-aging/")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        for key in (
            "as_of", "currency", "total_outstanding", "buckets", "items",
            "items_truncated", "definitions", "reconciliation", "net_receivable",
            "guest_credit_balances", "stay_value_not_yet_charged",
        ):
            self.assertIn(key, data)
        self.assertTrue(data["reconciliation"]["reconciles"])
        self.assertEqual(data["currency"], "NGN")

    def test_endpoint_rejects_a_malformed_currency(self):
        self.auth(self.manager)
        response = self.client.get("/api/admin/finance/reports/ar-aging/", {"currency": "naira"})
        self.assertEqual(response.status_code, 400, response.content)


class FolioBalanceCorrectnessTests(GuestReceivableScenarioTests):
    """Batch 01 task 7 — three ways the folio balance used to be wrong.

    These reuse the scenario helpers above: the same posting machinery, the
    same reconciliation proof.
    """

    def test_a_draft_charge_is_not_a_debt_yet(self):
        """An unapproved DRAFT transaction used to move the guest's balance."""
        from apps.finance.services.folio_service import folio_balance_queryset

        guest = make_guest("ar.draft@example.com")
        folio = self._folio(guest)
        self._post(kind="DEBIT", folio=folio, amount="9000.00",
                   transaction_type=FinancialTransaction.Type.OTHER_CHARGE)
        draft = self._post(kind="DEBIT", folio=folio, amount="5000.00",
                           transaction_type=FinancialTransaction.Type.OTHER_CHARGE)
        FinancialTransaction.objects.filter(pk=draft.pk).update(
            status=FinancialTransaction.Status.DRAFT
        )

        annotated = folio_balance_queryset().get(pk=folio.pk)
        self.assertEqual(annotated.balance, Decimal("9000.00"))
        # Both halves are reported, so a reader can see the charge exists
        # without it being counted as owed.
        self.assertEqual(annotated.charges_total, Decimal("9000.00"))
        self.assertEqual(annotated.credits_total, Decimal("0.00"))

    def test_a_voided_charge_is_not_counted_twice(self):
        """A VOIDED transaction is reversed by a balancing entry.

        Summing it as well as its reversal moved the balance in the wrong
        direction by the full amount.
        """
        from apps.finance.services.folio_service import folio_balance_queryset

        guest = make_guest("ar.voided@example.com")
        folio = self._folio(guest)
        self._post(kind="DEBIT", folio=folio, amount="7500.00",
                   transaction_type=FinancialTransaction.Type.OTHER_CHARGE)
        voided = self._post(kind="DEBIT", folio=folio, amount="2500.00",
                            transaction_type=FinancialTransaction.Type.OTHER_CHARGE)
        FinancialTransaction.objects.filter(pk=voided.pk).update(
            status=FinancialTransaction.Status.VOIDED
        )

        self.assertEqual(
            folio_balance_queryset().get(pk=folio.pk).balance, Decimal("7500.00")
        )

    def test_charges_and_credits_are_reported_separately_from_the_net(self):
        """A net zero can hide a large charge against a large credit."""
        from apps.finance.services.folio_service import folio_balance_queryset

        guest = make_guest("ar.netzero@example.com")
        folio = self._folio(guest)
        self._post(kind="DEBIT", folio=folio, amount="30000.00",
                   transaction_type=FinancialTransaction.Type.OTHER_CHARGE)
        self._post(kind="CREDIT", folio=folio, amount="30000.00",
                   transaction_type=FinancialTransaction.Type.PAYMENT_ALLOCATION)

        annotated = folio_balance_queryset().get(pk=folio.pk)
        self.assertEqual(annotated.balance, Decimal("0.00"))
        self.assertEqual(annotated.charges_total, Decimal("30000.00"))
        self.assertEqual(annotated.credits_total, Decimal("30000.00"))

    def test_a_voided_folio_is_not_a_debt_on_the_booking(self):
        """booking_folio_charge_map summed postings on cancelled folios."""
        from apps.finance.services.folio_service import booking_folio_charge_balance

        guest = make_guest("ar.voidfolio@example.com")
        booking = make_booking(guest, self.room_type, check_in=self.today - timedelta(days=1),
                               check_out=self.today + timedelta(days=1), total="20000.00")
        live = self._folio(guest, booking)
        dead = self._folio(guest, booking, reference="FOL-AR-VOID")
        self._post(kind="DEBIT", folio=live, amount="4000.00",
                   transaction_type=FinancialTransaction.Type.POS_CHARGE,
                   counter=accounting.FOOD_BEVERAGE_REVENUE)
        self._post(kind="DEBIT", folio=dead, amount="11000.00",
                   transaction_type=FinancialTransaction.Type.POS_CHARGE,
                   counter=accounting.FOOD_BEVERAGE_REVENUE)
        Folio.objects.filter(pk=dead.pk).update(status=Folio.Status.VOIDED)

        self.assertEqual(
            booking_folio_charge_balance(booking=booking), Decimal("4000.00")
        )

    def test_a_foreign_currency_charge_is_not_added_to_a_naira_booking(self):
        """Two currencies used to be summed as though interchangeable."""
        from apps.finance.services.folio_service import booking_folio_charge_balance

        guest = make_guest("ar.fx@example.com")
        booking = make_booking(guest, self.room_type, check_in=self.today - timedelta(days=1),
                               check_out=self.today + timedelta(days=1), total="20000.00")
        self.assertEqual(booking.currency, "NGN")
        naira = self._folio(guest, booking)
        dollars = self._folio(guest, booking, reference="FOL-AR-USD", currency="USD")
        self._post(kind="DEBIT", folio=naira, amount="6000.00",
                   transaction_type=FinancialTransaction.Type.OTHER_CHARGE)
        self._post(kind="DEBIT", folio=dollars, amount="150.00",
                   transaction_type=FinancialTransaction.Type.OTHER_CHARGE,
                   currency="USD")

        # 6000 + 150 was previously reported as 6150 "NGN".
        self.assertEqual(
            booking_folio_charge_balance(booking=booking), Decimal("6000.00")
        )
        # The dollars are not lost: the receivables report keeps them apart.
        report = guest_receivables(as_of=self.today, currency="NGN")
        self._assert_reconciles(report)
        other = {row["currency"]: row["balance"] for row in report["other_currency_balances"]}
        self.assertEqual(Decimal(other["USD"]), Decimal("150.00"))


class BookingBalanceDetailTests(GuestReceivableScenarioTests):
    """Batch 01 task 7 — the clamped figure must not be the only one."""

    def test_an_overpaid_booking_reports_the_credit_it_owes_back(self):
        from apps.finance.services.guest_receivables_service import booking_balance_detail

        guest = make_guest("ar.overpaid@example.com")
        booking = make_booking(guest, self.room_type, check_in=self.today - timedelta(days=1),
                               check_out=self.today + timedelta(days=1),
                               total="20000.00", amount_paid="20000.00")
        folio = self._folio(guest, booking)
        self._post(kind="CREDIT", folio=folio, amount="5000.00",
                   transaction_type=FinancialTransaction.Type.PAYMENT_ALLOCATION)

        detail = booking_balance_detail(booking=booking)
        # The desk still sees nothing to collect...
        self.assertEqual(detail["outstanding"], "0.00")
        # ...but the money the hotel holds is no longer invisible.
        self.assertEqual(detail["credit_balance"], "5000.00")
        self.assertEqual(detail["signed_balance"], "-5000.00")

    def test_the_clamped_desk_figure_is_unchanged(self):
        from apps.finance.services.folio_service import booking_outstanding_balance
        from apps.finance.services.guest_receivables_service import booking_balance_detail

        guest = make_guest("ar.owing@example.com")
        booking = make_booking(guest, self.room_type, check_in=self.today - timedelta(days=1),
                               check_out=self.today + timedelta(days=1),
                               total="20000.00", amount_paid="5000.00")
        folio = self._folio(guest, booking)
        self._post(kind="DEBIT", folio=folio, amount="2000.00",
                   transaction_type=FinancialTransaction.Type.POS_CHARGE,
                   counter=accounting.FOOD_BEVERAGE_REVENUE)

        detail = booking_balance_detail(booking=booking)
        self.assertEqual(
            Decimal(detail["outstanding"]), booking_outstanding_balance(booking=booking)
        )
        self.assertEqual(detail["credit_balance"], "0.00")
        self.assertEqual(detail["signed_balance"], "17000.00")
