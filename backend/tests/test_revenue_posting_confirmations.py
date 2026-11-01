"""A5–A8 — ledger confirmations for the money-in paths.

The audit found every one of these flows already implemented but
unconfirmed against the ledger contract the task names. These tests pin the
contract, not the code:

* **A5 booking refunds** — only a provider-confirmed processed refund posts;
  the posting is a distinct immutable REFUND transaction with per-portion
  ``RefundApplication`` evidence, it posts exactly once, and two refunds can
  never consume the same money twice.
* **A6 restaurant/bar revenue** — a counter sale posts Dr receivable /
  Cr F&B revenue with no invented tax line, a tender collects against the
  receivable exactly once even on retry, and an unopened return debits the
  outlet's revenue.
* **A7 laundry revenue** — room-charge laundry bills one folio charge
  (Dr receivable / Cr other operating revenue) exactly once even when billed
  again, and walk-in laundry has no folio to bill.
* **A8 payroll** — approval posts the accrual (Dr payroll expense /
  Cr payroll payable), settlement posts the payment against the payable, and
  the statutory tax/pension INPUTS removed earlier are not reintroduced.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import (
    FinancialLine,
    FinancialTransaction,
    FolioPosting,
    RefundApplication,
)
from apps.finance.services import accounting
from apps.finance.services.ledger_service import LedgerIntegrityError
from apps.finance.services.payment_collection_service import (
    ensure_payment_collection,
    ensure_processed_refund,
)
from apps.guest_services.services.laundry_service import bill_laundry_order, create_laundry_order
from apps.payments.models import Payment, Refund
from apps.pos.models import MenuCategory, MenuItem, PosOrder
from apps.pos.services import order_service
from apps.pos.services.order_service import capture_direct_tender, create_order
from apps.pos.services.return_service import record_unopened_return
from apps.staff_operations.models import StaffProfile
from apps.staff_operations.payroll_models import PayrollLine, PayrollPeriod, StaffCompensation
from apps.staff_operations.services.payroll_service import (
    create_payroll_period,
    pay_payroll_period,
    review_payroll_period,
    submit_payroll_period,
)

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type, make_staff


class RefundLedgerConfirmationTests(BaseAPITestCase):
    """A5 — the refund ledger contract."""

    def setUp(self):
        super().setUp()
        self.manager = make_staff("a5-manager@staff.dev", role=User.Role.MANAGER)
        room_type = make_room_type("Refund suite", price="100000.00")
        self.booking = make_booking(
            make_guest("a5-guest@example.com"), room_type,
            rooms=[make_room(room_type, "A5-1")],
            check_in=hotel_today(), check_out=hotel_today() + timedelta(days=2),
        )
        self.payment = Payment.objects.create(
            booking=self.booking, user=self.manager, reference="J1P-A5-PAY",
            provider=Payment.Provider.BANK_TRANSFER, amount=Decimal("300000.00"),
            currency="NGN", status=Payment.Status.SUCCESS, paid_at=timezone.now(),
        )
        ensure_payment_collection(payment=self.payment, actor=self.manager)

    def _refund(self, amount, *, status=Refund.Status.PROCESSED, key="A5-1"):
        return Refund.objects.create(
            booking=self.booking, payment=self.payment, amount=Decimal(amount),
            currency="NGN", paystack_transaction_reference=f"PI-{key}",
            status=status,
            processed_at=timezone.now() if status == Refund.Status.PROCESSED else None,
        )

    def test_only_a_provider_confirmed_refund_posts(self):
        # One refund row per payment (the model's uniqueness), so each status
        # gets its own payment to refuse.
        for status in (Refund.Status.PENDING, Refund.Status.PROCESSING, Refund.Status.FAILED):
            payment = Payment.objects.create(
                booking=self.booking, user=self.manager, reference=f"J1P-A5-{status}",
                provider=Payment.Provider.BANK_TRANSFER, amount=Decimal("50000.00"),
                currency="NGN", status=Payment.Status.SUCCESS, paid_at=timezone.now(),
            )
            ensure_payment_collection(payment=payment, actor=self.manager)
            refund = Refund.objects.create(
                booking=self.booking, payment=payment, amount=Decimal("50000.00"),
                currency="NGN", paystack_transaction_reference=f"PI-A5-{status}",
                status=status, processed_at=None,
            )
            with self.assertRaises(ValidationError):
                ensure_processed_refund(refund=refund, actor=self.manager)
        self.assertFalse(
            FinancialTransaction.objects.filter(type=FinancialTransaction.Type.REFUND).exists(),
            "nothing may post before the provider confirms",
        )

    def test_a_processed_refund_posts_once_with_per_portion_evidence(self):
        refund = self._refund("200000.00", key="A5-POST")
        first, first_created = ensure_processed_refund(refund=refund, actor=self.manager)
        self.assertTrue(first_created)
        self.assertEqual(first.type, FinancialTransaction.Type.REFUND)
        again, again_created = ensure_processed_refund(refund=refund, actor=self.manager)
        self.assertFalse(again_created)
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(
            FinancialTransaction.objects.filter(type=FinancialTransaction.Type.REFUND).count(), 1,
            "a refund posts exactly once",
        )
        # The unapplied portion returns guest-deposit money; the credit is the
        # collection asset the money went back out of.
        debits = {line.account_code: line for line in first.lines.all() if line.direction == FinancialLine.Direction.DEBIT}
        credits = {line.account_code: line for line in first.lines.all() if line.direction == FinancialLine.Direction.CREDIT}
        self.assertIn(accounting.GUEST_DEPOSITS, debits)
        self.assertEqual(credits[accounting.BANK_CLEARING].amount, Decimal("200000.00"))
        # Per-portion evidence: every application names its refund and amount.
        applications = RefundApplication.objects.filter(refund=refund, transaction=first)
        self.assertEqual(applications.count(), 1)
        self.assertEqual(sum((a.amount for a in applications), Decimal("0")), Decimal("200000.00"))

    def test_a_refund_cannot_consume_more_than_the_payment_carried(self):
        # One refund row per payment; its amount can never exceed what the
        # payment actually collected (deposits + folio applications), so the
        # same money can never be returned twice.
        too_much = self._refund("450000.00", key="A5-OVER")
        with self.assertRaises(LedgerIntegrityError):
            ensure_processed_refund(refund=too_much, actor=self.manager)
        self.assertFalse(
            FinancialTransaction.objects.filter(source_key=f"refund:{too_much.pk}").exists(),
            "the refused refund posts nothing",
        )
        # The full payment is refundable — exactly once, with evidence that
        # accounts for every naira collected.
        full = self._refund("300000.00", key="A5-FULL")
        ensure_processed_refund(refund=full, actor=self.manager)
        self.assertEqual(
            sum((a.amount for a in RefundApplication.objects.filter(payment=self.payment)), Decimal("0")),
            Decimal("300000.00"),
            "applications can never exceed the money actually collected",
        )


class PosRevenueLedgerConfirmationTests(BaseAPITestCase):
    """A6 — restaurant/bar revenue and tenders."""

    def setUp(self):
        super().setUp()
        self.actor = make_staff("a6-cashier@staff.dev", role=User.Role.CASHIER)
        category = MenuCategory.objects.create(name="A6 Bar", slug="a6-bar", service_area=MenuCategory.ServiceArea.BAR)
        self.item = MenuItem.objects.create(
            category=category, name="Chapman", sku="A6-CHAPMAN", base_price=Decimal("1500.00"),
        )
        self.order, _ = create_order(
            mode=PosOrder.Mode.BAR, actor=self.actor,
            lines=[{"menu_item_id": self.item.pk, "quantity": 2}],
        )
        order_service.submit_order(order=self.order, actor=self.actor)
        for status in (PosOrder.Status.PREPARING, PosOrder.Status.READY, PosOrder.Status.DELIVERED):
            order_service.transition_order(order=self.order, target_status=status, actor=self.actor)
        self.order.refresh_from_db()

    def test_a_counter_sale_posts_receivable_and_fb_revenue_with_no_tax_line(self):
        charge = self.order.charge_transaction
        self.assertIsNotNone(charge, "a delivered order is charged")
        self.assertEqual(charge.type, FinancialTransaction.Type.POS_CHARGE)
        lines = list(charge.lines.all())
        self.assertEqual(len(lines), 2, "new orders carry exactly a debit and a credit — no tax line")
        debit = next(line for line in lines if line.direction == FinancialLine.Direction.DEBIT)
        credit = next(line for line in lines if line.direction == FinancialLine.Direction.CREDIT)
        self.assertEqual(debit.account_code, accounting.ACCOUNTS_RECEIVABLE)
        self.assertEqual(debit.amount, Decimal("3000.00"))
        self.assertEqual(credit.account_code, accounting.FOOD_BEVERAGE_REVENUE)
        self.assertEqual(credit.amount, Decimal("3000.00"))
        self.assertFalse(
            charge.lines.filter(account_code=accounting.TAX_PAYABLE).exists(),
            "no tax is invented on a new sale",
        )

    def test_the_charge_posts_exactly_once_even_when_re_run(self):
        before = FinancialTransaction.objects.filter(type=FinancialTransaction.Type.POS_CHARGE).count()
        order_service._post_order_charge(order=self.order, actor=self.actor)
        self.assertEqual(
            FinancialTransaction.objects.filter(type=FinancialTransaction.Type.POS_CHARGE).count(),
            before,
            "the charge is keyed to the order and cannot double-post",
        )

    def test_a_tender_collects_against_the_receivable_exactly_once(self):
        tender = capture_direct_tender(
            order=self.order, method="CARD", amount=Decimal("3000.00"),
            actor=self.actor, idempotency_key="a6-tender-1",
        )
        collection = FinancialTransaction.objects.get(source_key=f"pos-collection:{tender.reference}")
        self.assertEqual(collection.type, FinancialTransaction.Type.PAYMENT_COLLECTION)
        debit = collection.lines.get(direction=FinancialLine.Direction.DEBIT)
        credit = collection.lines.get(direction=FinancialLine.Direction.CREDIT)
        self.assertEqual(debit.amount, Decimal("3000.00"))
        self.assertTrue(debit.account_code.startswith("ASSET."), "the tender lands in its clearing asset")
        self.assertEqual(credit.account_code, accounting.ACCOUNTS_RECEIVABLE)
        # A retried capture cannot collect twice: the settled order refuses it
        # outright, and exactly one collection exists.
        with self.assertRaises(ValidationError):
            capture_direct_tender(
                order=self.order, method="CARD", amount=Decimal("3000.00"),
                actor=self.actor, idempotency_key="a6-tender-1",
            )
        self.assertEqual(
            FinancialTransaction.objects.filter(type=FinancialTransaction.Type.PAYMENT_COLLECTION).count(), 1,
        )

    def test_an_unopened_return_debits_the_outlets_revenue(self):
        order_line = self.order.lines.get()
        record, created = record_unopened_return(
            order=self.order, actor=self.actor,
            lines=[{"order_line_id": order_line.pk, "quantity_returned": 1}],
            reason="Guest returned one sealed bottle",
        )
        self.assertTrue(created)
        credit = record.credit_transaction
        self.assertIsNotNone(credit)
        debit = credit.lines.get(direction=FinancialLine.Direction.DEBIT)
        self.assertEqual(debit.account_code, accounting.FOOD_BEVERAGE_REVENUE)
        self.assertEqual(debit.amount, Decimal("1500.00"))
        reversal = credit.lines.get(direction=FinancialLine.Direction.CREDIT)
        self.assertEqual(reversal.account_code, accounting.ACCOUNTS_RECEIVABLE)


class LaundryRevenueLedgerConfirmationTests(BaseAPITestCase):
    """A7 — laundry billing."""

    def setUp(self):
        super().setUp()
        self.manager = make_staff("a7-manager@staff.dev", role=User.Role.MANAGER)
        room_type = make_room_type("Laundry suite", price="50000.00")
        self.booking = make_booking(
            make_guest("a7-guest@example.com"), room_type,
            rooms=[make_room(room_type, "A7-1")],
            check_in=hotel_today(), check_out=hotel_today() + timedelta(days=2),
        )
        from apps.bookings.services.booking_service import check_in_booking

        check_in_booking(booking=self.booking, staff_user=self.manager)
        self.stay = self.booking.stay
        self.items = [
            {"description": "Shirt", "quantity": 3, "service_type": "WASH_IRON", "unit_price": "500.00"},
            {"description": "Suit", "quantity": 1, "service_type": "DRY_CLEAN", "unit_price": "2500.00"},
        ]

    def test_room_charge_laundry_bills_one_folio_charge_exactly_once(self):
        order, _ = create_laundry_order(
            actor=self.manager, items=self.items, stay=self.stay,
            idempotency_key="a7-laundry-1",
        )
        billed, created = bill_laundry_order(order=order, actor=self.manager)
        self.assertTrue(created)
        again, created_again = bill_laundry_order(order=billed, actor=self.manager)
        self.assertFalse(created_again, "billing twice charges once")
        self.assertEqual(
            FinancialTransaction.objects.filter(source_key=f"laundry:{order.reference}").count(), 1,
        )
        charge = again.financial_transaction
        self.assertEqual(charge.type, FinancialTransaction.Type.OTHER_CHARGE)
        debit = charge.lines.get(direction=FinancialLine.Direction.DEBIT)
        credit = charge.lines.get(direction=FinancialLine.Direction.CREDIT)
        self.assertEqual(debit.account_code, accounting.ACCOUNTS_RECEIVABLE)
        self.assertEqual(credit.account_code, accounting.OTHER_OPERATING_REVENUE)
        self.assertEqual(credit.amount, Decimal("4000.00"))
        self.assertEqual(
            FolioPosting.objects.filter(transaction=charge, kind=FolioPosting.Kind.CHARGE).count(), 1,
            "one folio charge — not one per billing attempt",
        )

    def test_walk_in_laundry_has_no_folio_to_bill(self):
        order, _ = create_laundry_order(
            actor=self.manager, items=self.items,
            customer_name="Walk-in customer", idempotency_key="a7-walkin-1",
        )
        with self.assertRaises(ValidationError):
            bill_laundry_order(order=order, actor=self.manager)


class PayrollLedgerConfirmationTests(BaseAPITestCase):
    """A8 — payroll accrual and settlement postings."""

    def setUp(self):
        super().setUp()
        self.admin = make_staff("a8-admin@staff.dev", role=User.Role.ADMIN)
        self.employee = make_staff("a8-clerk@staff.dev", role=User.Role.RECEPTIONIST)
        StaffProfile.objects.get_or_create(
            user=self.employee,
            defaults={
                "employee_code": "STF-A8-1",
                "department": "Front Desk",
                "job_title": "Clerk",
                "employment_status": StaffProfile.EmploymentStatus.ACTIVE,
            },
        )
        StaffCompensation.objects.create(
            staff=self.employee, effective_from=date(2026, 10, 1), currency="NGN",
            basic_salary=Decimal("120000.00"), housing_allowance=Decimal("20000.00"),
            transport_allowance=Decimal("10000.00"), overtime_rate=Decimal("0.00"),
            created_by=self.admin,
        )

    def _approved_period(self, key="a8-period-1"):
        period, _ = create_payroll_period(
            actor=self.admin, starts_on=date(2026, 10, 1), ends_on=date(2026, 10, 31),
            department="Front Desk", idempotency_key=key,
        )
        period = submit_payroll_period(period=period, actor=self.admin)
        return review_payroll_period(
            period=period, reviewer=self.admin, approved=True, review_note="Approved on the ledger contract.",
        )

    def test_approval_posts_the_payroll_accrual_exactly_as_the_task_describes(self):
        period = self._approved_period()
        accrual = period.accrual_transaction
        self.assertIsNotNone(accrual, "approval posts the accrual")
        self.assertEqual(accrual.type, FinancialTransaction.Type.PAYROLL_ACCRUAL)
        debit = accrual.lines.get(direction=FinancialLine.Direction.DEBIT)
        credit = accrual.lines.get(direction=FinancialLine.Direction.CREDIT)
        self.assertEqual(debit.account_code, accounting.PAYROLL_EXPENSE)
        self.assertEqual(debit.amount, Decimal("150000.00"))  # gross
        self.assertEqual(credit.account_code, accounting.PAYROLL_PAYABLE)
        self.assertEqual(credit.amount, Decimal("150000.00"))  # net (no deductions here)
        self.assertEqual(
            FinancialTransaction.objects.filter(type=FinancialTransaction.Type.PAYROLL_ACCRUAL).count(), 1,
            "one approval, one accrual",
        )

    def test_settlement_posts_the_payroll_payment_against_the_payable(self):
        period = self._approved_period()
        settled = pay_payroll_period(
            period=period, actor=self.admin, method="BANK_TRANSFER", external_reference="TRF-A8-1",
        )
        payment = settled.payment_transaction
        self.assertEqual(payment.type, FinancialTransaction.Type.PAYROLL_PAYMENT)
        debit = payment.lines.get(direction=FinancialLine.Direction.DEBIT)
        credit = payment.lines.get(direction=FinancialLine.Direction.CREDIT)
        self.assertEqual(debit.account_code, accounting.PAYROLL_PAYABLE)
        self.assertEqual(credit.account_code, accounting.BANK_CLEARING)
        # Settling again is a no-op — the payable is discharged once.
        pay_payroll_period(period=settled, actor=self.admin, method="BANK_TRANSFER", external_reference="TRF-A8-2")
        self.assertEqual(
            FinancialTransaction.objects.filter(type=FinancialTransaction.Type.PAYROLL_PAYMENT).count(), 1,
        )

    def test_statutory_tax_and_pension_inputs_are_gone_for_good(self):
        """The statutory PAYE/pension surface was deliberately removed
        (accounts migrations 0017/0022); nothing may bring it back."""
        period = self._approved_period()
        # The engine zeroes the legacy statutory columns and keeps them zero.
        self.assertEqual(Decimal(period.total_paye), Decimal("0.00"))
        self.assertEqual(Decimal(period.total_employer_pension), Decimal("0.00"))
        # A manual deduction may not smuggle one back in under a label.
        with self.assertRaises(ValidationError):
            create_payroll_period(
                actor=self.admin, starts_on=date(2026, 10, 1), ends_on=date(2026, 10, 31),
                department="Front Desk", idempotency_key="a8-stat-label",
                adjustments=[{
                    "staff_id": self.employee.pk,
                    "deductions": [{"label": "PAYE", "amount": "5000.00"}],
                }],
            )
