"""Regression tests for refunded deposits and procurement instalment links."""
from datetime import timedelta
from decimal import Decimal
from django.core.exceptions import ValidationError
from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import Expense, FinancialLine, FinancialTransaction, Folio, FolioPosting, PaymentAllocation
from apps.finance.services import accounting
from apps.finance.services.guest_receivables_service import guest_receivables
from apps.finance.services.ledger_service import create_posted_transaction
from apps.finance.services.payment_collection_service import (apply_payment_to_folio, apply_unallocated_payments_for_booking, ensure_payment_collection, ensure_processed_refund)
from apps.finance.services.expense_service import post_expense, review_expense, submit_expense
from apps.finance.services.procurement_payment_posting_service import reconcile_procurement_payment_with_accounting
from apps.inventory.models import PurchaseOrderPayment, StockItem, StockLocation, Supplier
from apps.inventory.services.inventory_service import (approve_purchase_order, create_purchase_order, record_purchase_order_payment, submit_purchase_order)
from apps.payments.models import Payment, Refund
from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room_type, make_staff


class RefundedDepositRegression(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.actor = make_staff('refund-fix@staff.dev', role=User.Role.ACCOUNTANT)
        self.today = hotel_today()
        self.booking = make_booking(make_guest('refund-fix@test.dev'), make_room_type('Refund suite'), check_in=self.today, check_out=self.today + timedelta(days=1), total='70000')
        self.folio = Folio.objects.create(reference='FOL-REFUND-FIX', booking=self.booking, guest=self.booking.guest, currency='NGN')
        self.payment = Payment.objects.create(booking=self.booking, user=self.actor, reference='PAY-REFUND-FIX', provider=Payment.Provider.BANK_TRANSFER, amount=Decimal('70000'), currency='NGN', status=Payment.Status.SUCCESS)
        ensure_payment_collection(payment=self.payment, actor=self.actor)

    def _refund(self, value):
        refund = Refund.objects.create(payment=self.payment, booking=self.booking, amount=Decimal(value), currency='NGN', status=Refund.Status.PROCESSED, paystack_transaction_reference=self.payment.reference)
        ensure_processed_refund(refund=refund, actor=self.actor)

    def _charge(self, value='70000'):
        create_posted_transaction(transaction_type=FinancialTransaction.Type.ACCOMMODATION_CHARGE, source_key='refund-fix-charge', actor=self.actor,
            lines=[{'account_code': accounting.ACCOUNTS_RECEIVABLE, 'direction': 'DEBIT', 'amount': value, 'folio': self.folio}, {'account_code': accounting.ACCOMMODATION_REVENUE, 'direction': 'CREDIT', 'amount': value}],
            postings=[{'line_index': 0, 'folio': self.folio, 'kind': 'CHARGE', 'effect': 'DEBIT', 'amount': value}])

    def test_partial_unallocated_refund_cannot_be_spent_again(self):
        self._refund('20000')
        self._charge()
        with self.assertRaises(ValidationError):
            apply_payment_to_folio(payment=self.payment, folio=self.folio, actor=self.actor, amount='70000')
        applied = apply_unallocated_payments_for_booking(booking=self.booking, folio=self.folio, actor=self.actor)
        self.assertEqual(sum((x.amount for x in applied), Decimal('0')), Decimal('50000'))
        self.assertEqual(apply_unallocated_payments_for_booking(booking=self.booking, folio=self.folio, actor=self.actor), [])
        report = guest_receivables(as_of=self.today)
        self.assertEqual(report['gross_receivables'], '20000.00')
        self.assertEqual(report['unapplied_guest_deposits'], '0.00')
        self.assertTrue(report['reconciliation']['reconciles'])

    def test_full_unallocated_refund_leaves_no_allocatable_deposit(self):
        self._refund('70000')
        self._charge()
        self.assertEqual(apply_unallocated_payments_for_booking(booking=self.booking, folio=self.folio, actor=self.actor), [])
        self.assertEqual(PaymentAllocation.objects.filter(payment=self.payment).count(), 0)
        report = guest_receivables(as_of=self.today)
        self.assertEqual(report['gross_receivables'], '70000.00')
        self.assertEqual(report['unapplied_guest_deposits'], '0.00')

    def test_refund_of_allocated_money_reopens_receivable_not_deposit(self):
        self._charge('40000')
        apply_payment_to_folio(payment=self.payment, folio=self.folio, amount='40000', actor=self.actor, settles=FolioPosting.Settles.ACCOMMODATION)
        self._refund('10000')
        report = guest_receivables(as_of=self.today)
        self.assertEqual(report['gross_receivables'], '10000.00')
        self.assertEqual(report['unapplied_guest_deposits'], '30000.00')


class ProcurementLinkRegression(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.maker = make_staff('link-maker@staff.dev', role=User.Role.PROCUREMENT_OFFICER)
        self.manager = make_staff('link-manager@staff.dev', role=User.Role.MANAGER)
        self.accountant = make_staff('link-accountant@staff.dev', role=User.Role.ACCOUNTANT)
        supplier = Supplier.objects.create(reference='LINK-SUP', name='Link Supplier')
        location = StockLocation.objects.create(code='LINK', name='Link store')
        item = StockItem.objects.create(sku='LINK-ITEM', name='Link item', base_unit='piece')
        self.po, _ = create_purchase_order(supplier=supplier, delivery_location=location, requested_by=self.maker, lines=[{'item_id': item.pk, 'quantity_ordered': '2', 'unit_cost': '1000'}])
        submit_purchase_order(purchase_order=self.po, actor=self.maker)
        approve_purchase_order(purchase_order=self.po, actor=self.manager)

    def test_distinct_instalment_never_claims_first_handover_posted(self):
        first = record_purchase_order_payment(purchase_order=self.po, actor=self.manager, method='BANK_TRANSFER', amount='1000', reference='TRF-LINK-1')
        expense = Expense.objects.get(purchase_order=self.po)
        submit_expense(expense=expense, actor=self.accountant)
        review_expense(expense=expense, reviewer=self.manager, approved=True)
        post_expense(expense=expense, actor=self.accountant)
        result = reconcile_procurement_payment_with_accounting(payment=first, actor=self.accountant)
        self.assertEqual(result['status'], PurchaseOrderPayment.AccountingPostingStatus.POSTED)
        self.assertTrue(result['finance_transaction_reference'])
        second = record_purchase_order_payment(purchase_order=self.po, actor=self.manager, method='BANK_TRANSFER', amount='1000', reference='TRF-LINK-2')
        result = reconcile_procurement_payment_with_accounting(payment=second, actor=self.accountant)
        self.assertEqual(result['status'], PurchaseOrderPayment.AccountingPostingStatus.REQUIRES_REVIEW)
        self.assertEqual(result['finance_transaction_reference'], '')
        self.assertEqual(Expense.objects.filter(purchase_order=self.po).count(), 1)
        self.assertEqual(FinancialLine.objects.filter(account_code=accounting.INVENTORY_ASSET).count(), 1)


class StatementCurrencyRegression(BaseAPITestCase):
    def test_statements_never_mix_usd_into_ngn(self):
        from apps.finance.services.statements_service import balance_sheet, cash_flow
        today = hotel_today()
        for currency, value in [('NGN', '100'), ('USD', '700')]:
            create_posted_transaction(transaction_type=FinancialTransaction.Type.PAYMENT_COLLECTION,
                source_key='currency-test-' + currency, currency=currency, business_date=today,
                lines=[{'account_code': accounting.BANK_CLEARING, 'direction': 'DEBIT', 'amount': value},
                       {'account_code': accounting.GUEST_DEPOSITS, 'direction': 'CREDIT', 'amount': value}])
        sheet = balance_sheet(as_of=today)
        self.assertEqual(sheet['total_assets'], '100.00')
        self.assertEqual(sheet['total_liabilities'], '100.00')
        self.assertTrue(sheet['balanced'])
        flow = cash_flow(start=today, end=today)
        self.assertEqual(flow['closing_cash'], '100.00')
        self.assertEqual(flow['net_cash_change_classified'], '100.00')
        self.assertTrue(flow['reconciles'])
