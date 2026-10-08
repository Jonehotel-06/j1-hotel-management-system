"""Critical finance invariants: balance, idempotency, and immutability."""
from datetime import timedelta
from unittest.mock import patch
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError

from apps.accounts.models import User
from apps.finance.models import (
    FinancialControlPolicy,
    FinancialEvent,
    FinancialLine,
    FinancialTransaction,
    FolioPosting,
    PaymentAllocation,
    RefundApplication,
)
from apps.finance.services import accounting
from apps.finance.services.folio_service import (
    folio_balance_queryset,
    get_or_create_main_folio_for_stay,
)
from apps.finance.services.ledger_service import LedgerIntegrityError, create_posted_transaction
from apps.stays.models import Stay
from apps.payments.models import Payment, Refund

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room_type, make_staff


class FinanceFoundationTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.actor = make_staff("finance-desk@staff.dev", role=User.Role.MANAGER)
        room_type = make_room_type("Finance suite", price="25000.00")
        booking = make_booking(
            make_guest("finance-guest@example.com"),
            room_type,
            check_in=self._today(),
            check_out=self._today() + timedelta(days=2),
            total="50000.00",
        )
        self.stay = Stay.objects.create(
            reference=f"STY-{booking.booking_reference}",
            booking=booking,
            guest=booking.guest,
            status=Stay.Status.IN_HOUSE,
            expected_arrival=booking.check_in,
            expected_departure=booking.check_out,
            actual_check_in_at=booking.created_at,
        )
        self.folio, self.folio_created = get_or_create_main_folio_for_stay(
            stay=self.stay, actor=self.actor
        )

    @staticmethod
    def _today():
        from apps.core.utils import hotel_today
        return hotel_today()

    def _balanced_lines(self, amount="1000.00", *, folio=None):
        return [
            {
                "account_code": accounting.ACCOUNTS_RECEIVABLE,
                "direction": FinancialLine.Direction.DEBIT,
                "amount": amount,
                "folio": folio,
                "description": "Controlled test charge",
            },
            {
                "account_code": accounting.ACCOMMODATION_REVENUE,
                "direction": FinancialLine.Direction.CREDIT,
                "amount": amount,
                "description": "Controlled test revenue",
            },
        ]

    def test_default_conservative_control_policy_is_seeded(self):
        policy = FinancialControlPolicy.objects.get(name="Default financial controls")
        self.assertTrue(policy.is_active)
        self.assertEqual(policy.refund_manager_approval_threshold, Decimal("0.00"))

    def test_main_folio_is_idempotent_per_stay(self):
        again, created = get_or_create_main_folio_for_stay(stay=self.stay, actor=self.actor)
        self.assertTrue(self.folio_created)
        self.assertFalse(created)
        self.assertEqual(again.pk, self.folio.pk)
        self.assertEqual(again.booking_id, self.stay.booking_id)
        self.assertEqual(again.guest_id, self.stay.guest_id)

    def test_unbalanced_entry_rolls_back_completely(self):
        with self.assertRaises(LedgerIntegrityError):
            create_posted_transaction(
                transaction_type=FinancialTransaction.Type.ACCOMMODATION_CHARGE,
                source_key="test:unbalanced",
                actor=self.actor,
                lines=[
                    {
                        "account_code": accounting.ACCOUNTS_RECEIVABLE,
                        "direction": FinancialLine.Direction.DEBIT,
                        "amount": "1000.00",
                    },
                    {
                        "account_code": accounting.ACCOMMODATION_REVENUE,
                        "direction": FinancialLine.Direction.CREDIT,
                        "amount": "999.00",
                    },
                ],
            )
        self.assertFalse(FinancialTransaction.objects.filter(source_key="test:unbalanced").exists())
        self.assertEqual(FinancialLine.objects.count(), 0)

    def test_folio_posting_cannot_diverge_from_its_balanced_line(self):
        with self.assertRaisesMessage(LedgerIntegrityError, "must equal its source financial line"):
            create_posted_transaction(
                transaction_type=FinancialTransaction.Type.ACCOMMODATION_CHARGE,
                source_key="test:mismatched-folio-posting",
                actor=self.actor,
                lines=self._balanced_lines("100.00", folio=self.folio),
                postings=[{
                    "line_index": 0,
                    "folio": self.folio,
                    "kind": FolioPosting.Kind.CHARGE,
                    "effect": FolioPosting.Effect.DEBIT,
                    "amount": "99.00",
                }],
            )
        self.assertFalse(FinancialTransaction.objects.filter(source_key="test:mismatched-folio-posting").exists())

    def test_folio_posting_effect_must_match_its_guest_receivable_line(self):
        with self.assertRaisesMessage(LedgerIntegrityError, "effect must match"):
            create_posted_transaction(
                transaction_type=FinancialTransaction.Type.ACCOMMODATION_CHARGE,
                source_key="test:mismatched-folio-effect",
                actor=self.actor,
                lines=self._balanced_lines("100.00", folio=self.folio),
                postings=[{
                    "line_index": 0,
                    "folio": self.folio,
                    "kind": FolioPosting.Kind.CHARGE,
                    "effect": FolioPosting.Effect.CREDIT,
                    "amount": "100.00",
                }],
            )
        self.assertFalse(FinancialTransaction.objects.filter(source_key="test:mismatched-folio-effect").exists())

    def test_posting_is_idempotent_and_makes_financial_evidence_immutable(self):
        tx, created = create_posted_transaction(
            transaction_type=FinancialTransaction.Type.ACCOMMODATION_CHARGE,
            source_key="test:immutable",
            actor=self.actor,
            lines=self._balanced_lines(),
        )
        same, repeated = create_posted_transaction(
            transaction_type=FinancialTransaction.Type.ACCOMMODATION_CHARGE,
            source_key="test:immutable",
            actor=self.actor,
            lines=self._balanced_lines(),
        )
        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(tx.pk, same.pk)
        self.assertEqual(tx.status, FinancialTransaction.Status.POSTED)
        self.assertEqual(FinancialEvent.objects.filter(transaction=tx).count(), 2)

        tx.narrative = "tampered"
        with self.assertRaisesMessage(ValidationError, "immutable"):
            tx.save()
        with self.assertRaisesMessage(ValidationError, "cannot be deleted"):
            tx.delete()
        with self.assertRaisesMessage(ValidationError, "Cannot add a line"):
            FinancialLine.objects.create(
                transaction=tx,
                account_code=accounting.ACCOUNTS_RECEIVABLE,
                direction=FinancialLine.Direction.DEBIT,
                amount="1.00",
            )

    def test_sensitive_approval_requires_a_distinct_capable_reviewer(self):
        from apps.finance.models import ApprovalRequest
        from apps.finance.services.approval_service import request_approval, review_approval

        requester = make_staff("finance-requester@staff.dev", role=User.Role.RECEPTIONIST)
        reviewer = make_staff("finance-reviewer@staff.dev", role=User.Role.MANAGER)
        approval, created = request_approval(
            request_type=ApprovalRequest.Type.REFUND,
            amount="100.00",
            requester=requester,
            request_key="test:approval:refund:1",
            source_reference=self.stay.booking.booking_reference,
            reason="Guest cancellation refund",
        )
        repeat, repeated = request_approval(
            request_type=ApprovalRequest.Type.REFUND,
            amount="100.00",
            requester=requester,
            request_key="test:approval:refund:1",
        )
        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(approval.pk, repeat.pk)
        with self.assertRaises(PermissionDenied):
            review_approval(approval=approval, reviewer=requester, approved=True)
        reviewed = review_approval(approval=approval, reviewer=reviewer, approved=True)
        self.assertEqual(reviewed.status, ApprovalRequest.Status.APPROVED)
        self.assertEqual(reviewed.reviewer_id, reviewer.pk)
        self.assertTrue(FinancialEvent.objects.filter(approval_request=reviewed).exists())

    def test_payment_collection_allocation_and_partial_refunds_are_distinct_events(self):
        from apps.finance.services.payment_collection_service import (
            apply_payment_to_folio,
            ensure_payment_collection,
            ensure_processed_refund,
        )

        payment = Payment.objects.create(
            booking=self.stay.booking,
            user=self.actor,
            reference="J1P-FINANCE-BRIDGE-1",
            provider=Payment.Provider.CASH,
            amount=Decimal("100.00"),
            currency="NGN",
            status=Payment.Status.SUCCESS,
        )
        collection, collection_created = ensure_payment_collection(payment=payment, actor=self.actor)
        repeated_collection, repeated_created = ensure_payment_collection(payment=payment, actor=self.actor)
        allocation, allocation_created = apply_payment_to_folio(
            payment=payment, folio=self.folio, actor=self.actor
        )
        self.assertTrue(collection_created)
        self.assertFalse(repeated_created)
        self.assertEqual(collection.pk, repeated_collection.pk)
        self.assertTrue(allocation_created)
        self.assertEqual(PaymentAllocation.objects.filter(payment=payment).count(), 1)
        self.assertEqual(
            folio_balance_queryset().get(pk=self.folio.pk).balance,
            Decimal("-100"),
        )

        first_refund = Refund.objects.create(
            booking=self.stay.booking,
            payment=payment,
            amount=Decimal("40.00"),
            currency="NGN",
            paystack_transaction_reference=payment.reference,
            status=Refund.Status.PROCESSED,
        )
        second_refund = Refund.objects.create(
            booking=self.stay.booking,
            payment=payment,
            amount=Decimal("30.00"),
            currency="NGN",
            paystack_transaction_reference=payment.reference,
            status=Refund.Status.PROCESSED,
        )
        first_tx, first_created = ensure_processed_refund(refund=first_refund, actor=self.actor)
        second_tx, second_created = ensure_processed_refund(refund=second_refund, actor=self.actor)
        self.assertTrue(first_created)
        self.assertTrue(second_created)
        self.assertEqual(first_tx.type, FinancialTransaction.Type.REFUND)
        self.assertEqual(second_tx.type, FinancialTransaction.Type.REFUND)
        self.assertEqual(
            sum(RefundApplication.objects.filter(payment=payment).values_list("amount", flat=True), Decimal("0.00")),
            Decimal("70.00"),
        )
        self.assertEqual(
            folio_balance_queryset().get(pk=self.folio.pk).balance,
            Decimal("-30"),
        )

    def test_folio_posting_is_immutable_and_balance_is_database_computed(self):
        tx, created = create_posted_transaction(
            transaction_type=FinancialTransaction.Type.ACCOMMODATION_CHARGE,
            source_key="test:folio-charge",
            actor=self.actor,
            source_reference=self.stay.booking.booking_reference,
            lines=self._balanced_lines("1200.00", folio=self.folio),
            postings=[
                {
                    "line_index": 0,
                    "folio": self.folio,
                    "kind": FolioPosting.Kind.CHARGE,
                    "effect": FolioPosting.Effect.DEBIT,
                    "amount": "1200.00",
                    "description": "One test night",
                }
            ],
        )
        self.assertTrue(created)
        posting = FolioPosting.objects.get(transaction=tx)
        annotated = folio_balance_queryset().get(pk=self.folio.pk)
        self.assertEqual(annotated.balance, Decimal("1200"))
        posting.description = "tampered"
        with self.assertRaisesMessage(ValidationError, "immutable"):
            posting.save()
        with self.assertRaisesMessage(ValidationError, "cannot be deleted"):
            posting.delete()


class NightlyAccommodationTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        from apps.core.utils import hotel_today

        self.actor = make_staff("night-audit@staff.dev", role=User.Role.MANAGER)
        self.today = hotel_today()
        room_type = make_room_type("Night-audit suite", price="300.00")
        booking = make_booking(
            make_guest("night-audit-guest@example.com"),
            room_type,
            check_in=self.today - timedelta(days=2),
            check_out=self.today + timedelta(days=1),
            total="900.00",
        )
        self.stay = Stay.objects.create(
            reference=f"STY-{booking.booking_reference}",
            booking=booking,
            guest=booking.guest,
            status=Stay.Status.IN_HOUSE,
            expected_arrival=booking.check_in,
            expected_departure=booking.check_out,
        )

    def test_elapsed_nights_post_once_and_reconcile_to_booking_total(self):
        from apps.finance.services.accommodation_service import post_due_accommodation_charges_for_stay

        created = post_due_accommodation_charges_for_stay(stay=self.stay, actor=self.actor)
        repeated = post_due_accommodation_charges_for_stay(stay=self.stay, actor=self.actor)
        self.assertEqual(created, 2)
        self.assertEqual(repeated, 0)
        transactions = FinancialTransaction.objects.filter(
            type=FinancialTransaction.Type.ACCOMMODATION_CHARGE,
            source_reference=self.stay.booking.booking_reference,
        )
        self.assertEqual(transactions.count(), 2)
        total = sum(
            FinancialLine.objects.filter(
                transaction__in=transactions,
                account_code=accounting.ACCOUNTS_RECEIVABLE,
                direction=FinancialLine.Direction.DEBIT,
            ).values_list("amount", flat=True),
            Decimal("0.00"),
        )
        self.assertEqual(total, Decimal("600.00"))

    def test_nightly_batch_cursor_is_deterministic_and_posts_each_due_stay_once(self):
        from apps.finance.services.accommodation_service import post_previous_night_accommodation_batch

        room_type = make_room_type("Cursor suite", price="300.00")
        extra_stays = []
        for index in range(2):
            booking = make_booking(
                make_guest(f"night-cursor-{index}@example.com"), room_type,
                check_in=self.today - timedelta(days=2), check_out=self.today + timedelta(days=1), total="900.00",
            )
            extra_stays.append(Stay.objects.create(
                reference=f"STY-{booking.booking_reference}", booking=booking, guest=booking.guest,
                status=Stay.Status.IN_HOUSE, expected_arrival=booking.check_in, expected_departure=booking.check_out,
            ))
        service_date = self.today - timedelta(days=1)
        first_count, cursor = post_previous_night_accommodation_batch(service_date=service_date, batch_size=1)
        self.assertEqual(first_count, 1)
        self.assertIsNotNone(cursor)
        second_count, cursor = post_previous_night_accommodation_batch(
            service_date=service_date, batch_size=1, after_stay_id=cursor,
        )
        self.assertEqual(second_count, 1)
        self.assertIsNotNone(cursor)
        third_count, cursor = post_previous_night_accommodation_batch(
            service_date=service_date, batch_size=1, after_stay_id=cursor,
        )
        self.assertEqual(third_count, 1)
        self.assertIsNone(cursor)
        self.assertEqual(
            FinancialTransaction.objects.filter(
                type=FinancialTransaction.Type.ACCOMMODATION_CHARGE,
                business_date=service_date,
            ).count(),
            3,
        )

    def test_nightly_task_freezes_service_date_into_cursor_continuation(self):
        from apps.finance import tasks

        frozen_service_date = self.today - timedelta(days=1)
        with patch("apps.core.utils.hotel_today", return_value=self.today), \
             patch("apps.finance.services.accommodation_service.post_previous_night_accommodation_batch", return_value=(1, 321)) as batch, \
             patch.object(tasks.post_previous_night_accommodation, "delay") as delay:
            self.assertEqual(tasks.post_previous_night_accommodation(), 1)
        batch.assert_called_once_with(service_date=frozen_service_date, after_stay_id=None)
        delay.assert_called_once_with(service_date=frozen_service_date.isoformat(), after_stay_id=321)


    def test_current_unelapsed_night_cannot_be_recognized_early(self):
        from apps.finance.services.accommodation_service import post_accommodation_charge_for_night

        with self.assertRaisesMessage(ValidationError, "after the service night has elapsed"):
            post_accommodation_charge_for_night(
                stay=self.stay, service_date=self.today, actor=self.actor
            )
