# tests/test_guest_deposit_deferral.py
"""Guest money collected ahead of its room nights stays a guest deposit.

Critical repair A2 — the negative guest-receivable class (a balance sheet
showing Guest receivables at **-₦576,800**):

The check-in bridge used to apply EVERY collected payment to the folio the
moment the folio opened, crediting ``ASSET.GUEST_RECEIVABLE`` before any
accommodation charge existed. A prepaid booking therefore drove the
receivable control account credit-normal overnight, and a cancelled,
no-show or early-checkout booking left the credit standing permanently.

The corrected allocation rules (``payment_collection_service``):

* an allocation may credit the receivable only up to the posted, unsettled
  charges of the balance it settles (``allocation_capacity``);
* money ahead of its charges stays in ``LIABILITY.GUEST_DEPOSITS`` and is
  released to the receivable as each room night is recognised (the nightly
  charge hooks ``apply_unallocated_payments_for_booking``);
* an over-application is refused, not silently posted; refunds of
  unallocated money take the deposit branch (Dr GUEST_DEPOSITS / Cr cash),
  never a receivable debit.

These tests pin the corrected behaviour for prepayments, per-night release,
cancellation and refunds. The historical mechanism is reproduced and
explained in ``test_guest_receivable_check.py``.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import (
    FinancialLine,
    FinancialTransaction,
    FolioPosting,
    PaymentAllocation,
    RefundApplication,
)
from apps.finance.services import accounting
from apps.finance.services.accommodation_service import post_accommodation_charge_for_night
from apps.finance.services.payment_collection_service import (
    allocation_capacity,
    apply_payment_to_folio,
    ensure_payment_collection,
    ensure_processed_refund,
)
from apps.payments.models import Payment, Refund
from apps.stays.models import Stay
from apps.stays.services import _ensure_main_folio

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type, make_staff


def _account_balance(account_code):
    rows = FinancialLine.objects.filter(
        account_code=account_code, transaction__status=FinancialTransaction.Status.POSTED
    )
    debits = sum(
        (r.amount for r in rows.filter(direction=FinancialLine.Direction.DEBIT)), Decimal("0.00")
    )
    credits = sum(
        (r.amount for r in rows.filter(direction=FinancialLine.Direction.CREDIT)), Decimal("0.00")
    )
    if account_code.startswith(("ASSET.", "EXPENSE.", "CONTRA_REVENUE.")):
        return debits - credits
    return credits - debits


class GuestDepositDeferralTests(BaseAPITestCase):
    """A prepaid stay is a deposit liability until its nights are recognised."""

    def setUp(self):
        super().setUp()
        self.manager = make_staff("deposit-manager@staff.dev", role=User.Role.MANAGER)
        self.today = hotel_today()
        room_type = make_room_type("Deposit suite", price="100000.00")
        room = make_room(room_type, "701")
        # A three-night stay that has already consumed all of its nights:
        # every night is elapsed and recognisable through the production
        # nightly path (charges post only for elapsed dates).
        self.booking = make_booking(
            make_guest("deposit-guest@example.com"),
            room_type,
            rooms=[room],
            check_in=self.today - timedelta(days=3),
            check_out=self.today,
            total="300000.00",
            amount_paid="300000.00",
            status="CHECKED_IN",
        )
        self.stay = Stay.objects.create(
            reference=f"STY-{self.booking.booking_reference}",
            booking=self.booking,
            guest=self.booking.guest,
            status=Stay.Status.IN_HOUSE,
            expected_arrival=self.booking.check_in,
            expected_departure=self.booking.check_out,
        )
        # The prepayment, collected the way the platform collects: cash/bank
        # evidence first (Dr clearing / Cr guest deposits).
        self.payment = Payment.objects.create(
            booking=self.booking,
            user=self.manager,
            reference="J1P-DEPOSIT-PREPAY",
            provider=Payment.Provider.BANK_TRANSFER,
            amount=Decimal("300000.00"),
            currency="NGN",
            status=Payment.Status.SUCCESS,
            paid_at=timezone.now(),
        )
        ensure_payment_collection(payment=self.payment, actor=self.manager)

    # --------------------------------------------------------------- helpers
    def _receivable(self):
        return _account_balance(accounting.ACCOUNTS_RECEIVABLE)

    def _deposits(self):
        return _account_balance(accounting.GUEST_DEPOSITS)

    def _bridge(self):
        """Open the folio through the production check-in bridge."""
        folio, _ = _ensure_main_folio(stay=self.stay, actor=self.manager)
        return folio

    def _post_night(self, service_date):
        return post_accommodation_charge_for_night(
            stay=self.stay, service_date=service_date, actor=self.manager
        )

    # ------------------------------------------------------------------ tests
    def test_money_ahead_of_its_nights_stays_a_guest_deposit(self):
        folio = self._bridge()

        # Nothing is applied to the receivable: no allocation, no negative
        # receivable. The money is a deposit liability — exactly what it is.
        self.assertEqual(PaymentAllocation.objects.filter(payment=self.payment).count(), 0)
        self.assertEqual(self._receivable(), Decimal("0.00"))
        self.assertEqual(self._deposits(), Decimal("300000.00"))
        self.assertEqual(
            allocation_capacity(folio=folio, settles=FolioPosting.Settles.ACCOMMODATION),
            Decimal("0.00"),
        )
        # And the application is refused outright, not silently posted.
        with self.assertRaises(ValidationError):
            apply_payment_to_folio(
                payment=self.payment,
                folio=folio,
                actor=self.manager,
                settles=FolioPosting.Settles.ACCOMMODATION,
            )

    def test_each_recognised_night_releases_its_share_of_the_deposit(self):
        folio = self._bridge()
        nights = [
            self.today - timedelta(days=3),
            self.today - timedelta(days=2),
            self.today - timedelta(days=1),
        ]

        for index, service_date in enumerate(nights, start=1):
            self._post_night(service_date)
            allocations = PaymentAllocation.objects.filter(payment=self.payment)
            self.assertEqual(allocations.count(), index)
            self.assertEqual(
                sum((a.amount for a in allocations), Decimal("0.00")),
                Decimal("100000.00") * index,
            )
            # The receivable never goes negative, and each night nets to zero
            # as soon as its charge is recognised.
            self.assertEqual(self._receivable(), Decimal("0.00"))
            self.assertEqual(self._deposits(), Decimal("300000.00") - Decimal("100000.00") * index)

        # Every release is accommodation money: it can never be netted
        # against folio charges the guest runs up separately.
        for allocation in PaymentAllocation.objects.filter(payment=self.payment):
            posting = FolioPosting.objects.get(
                transaction=allocation.transaction, kind=FolioPosting.Kind.PAYMENT
            )
            self.assertEqual(posting.settles, FolioPosting.Settles.ACCOMMODATION)

    def test_a_cancelled_prepayment_leaves_no_guest_receivable_credit(self):
        """The ₦576,800 class: cancel before any night posts."""
        self._bridge()
        self.booking.status = "CANCELLED"
        self.booking.save(update_fields=["status"])

        # No night ever posted, so nothing can ever have been applied: the
        # guest-receivable account stays at zero and the money is still the
        # hotel's liability until the approved refund workflow returns it.
        self.assertEqual(PaymentAllocation.objects.filter(payment=self.payment).count(), 0)
        self.assertEqual(self._receivable(), Decimal("0.00"))
        self.assertEqual(self._deposits(), Decimal("300000.00"))

    def test_refund_of_unallocated_prepayment_takes_the_deposit_branch(self):
        self._bridge()
        refund = Refund.objects.create(
            booking=self.booking,
            payment=self.payment,
            amount=Decimal("300000.00"),
            currency="NGN",
            paystack_transaction_reference="PI-DEPOSIT-REFUND-1",
            status=Refund.Status.PROCESSED,
            processed_at=timezone.now(),
        )
        transaction, created = ensure_processed_refund(refund=refund, actor=self.manager)
        self.assertTrue(created)

        # The money went back out of the DEPOSIT liability: no receivable
        # debit, no folio refund posting, no invented folio charge.
        self.assertEqual(self._receivable(), Decimal("0.00"))
        self.assertEqual(self._deposits(), Decimal("0.00"))
        self.assertFalse(FolioPosting.objects.filter(transaction=transaction).exists())
        application = RefundApplication.objects.get(refund=refund)
        self.assertIsNone(application.payment_allocation_id)
        self.assertEqual(application.amount, Decimal("300000.00"))

        # Idempotent: re-running posts nothing new.
        again, repeated = ensure_processed_refund(refund=refund, actor=self.manager)
        self.assertFalse(repeated)
        self.assertEqual(again.pk, transaction.pk)

    def test_the_over_application_that_caused_the_credit_is_refused(self):
        """The exact mechanism of the reported defect, post-correction."""
        folio = self._bridge()
        self._post_night(self.today - timedelta(days=3))  # ₦100,000 recognised

        # A second payment of ₦200,000 may settle at most the ₦100,000 of
        # charges that exist; the rest must stay a deposit.
        extra = Payment.objects.create(
            booking=self.booking,
            user=self.manager,
            reference="J1P-DEPOSIT-EXTRA",
            provider=Payment.Provider.CASH,
            amount=Decimal("200000.00"),
            currency="NGN",
            status=Payment.Status.SUCCESS,
            paid_at=timezone.now(),
        )
        ensure_payment_collection(payment=extra, actor=self.manager)
        with self.assertRaises(ValidationError):
            apply_payment_to_folio(
                payment=extra, folio=folio, actor=self.manager, amount="150000.00"
            )
        self.assertEqual(self._receivable(), Decimal("0.00"))

        # The capacity is exact: ₦100,000 of the extra payment already
        # settled the night through the bridge; nothing more is recognised.
        self.assertEqual(
            allocation_capacity(folio=folio, settles=FolioPosting.Settles.ACCOMMODATION),
            Decimal("0.00"),
        )

    def test_allocation_is_not_backdated_to_the_prepayment(self):
        # The allocation happens after the charge, not when the deposit was
        # collected. Backdating it makes an earlier balance sheet credit-normal.
        prepayment = Payment.objects.create(
            booking=self.booking, user=self.manager, reference="J1P-EARLIER-DEPOSIT",
            provider=Payment.Provider.BANK_TRANSFER, amount=Decimal("100000.00"),
            currency="NGN", status=Payment.Status.SUCCESS,
            paid_at=timezone.now() - timedelta(days=5),
        )
        collection, _ = ensure_payment_collection(payment=prepayment, actor=self.manager)
        self.assertLess(collection.business_date, self.today - timedelta(days=1))
        self._bridge()
        self._post_night(self.today - timedelta(days=1))
        allocation = PaymentAllocation.objects.get(payment=prepayment)
        self.assertEqual(allocation.transaction.business_date, self.today)
        earlier = FinancialLine.objects.filter(
            account_code=accounting.ACCOUNTS_RECEIVABLE,
            transaction__status=FinancialTransaction.Status.POSTED,
            transaction__business_date__lt=self.today,
        )
        self.assertEqual(
            sum((line.amount if line.direction == FinancialLine.Direction.DEBIT else -line.amount
                 for line in earlier), Decimal("0.00")),
            Decimal("100000.00"),
        )
