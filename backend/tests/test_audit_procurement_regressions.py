"""Synthetic regressions for supplier arithmetic and goods-receipt safety.

These do not assert that supplier payments are ledger-posted or session-backed;
those separate controls remain unresolved and must not be inferred here.
"""
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient
from apps.accounts.models import User
from apps.inventory.models import PurchaseOrder, StockBalance, StockItem, StockLocation, Supplier
from apps.inventory.services.inventory_service import (
    approve_purchase_order, attach_purchase_order_receipt, create_purchase_order, receive_goods,
    record_purchase_order_payment, submit_purchase_order,
)


class ProcurementRegressions(TestCase):
    def setUp(self):
        self.maker = User.objects.create_user(email='repro-maker@example.invalid', role=User.Role.PROCUREMENT_OFFICER)
        self.checker = User.objects.create_user(email='repro-checker@example.invalid', role=User.Role.MANAGER)
        self.supplier = Supplier.objects.create(reference='SYN-SUPPLIER', name='Synthetic Supplier')
        self.location = StockLocation.objects.create(code='SYN-STORE', name='Synthetic Store')
        self.item = StockItem.objects.create(sku='SYN-ITEM', name='Synthetic Item', base_unit='piece')
        self.po, _ = create_purchase_order(supplier=self.supplier, delivery_location=self.location,
            requested_by=self.maker, lines=[{'item_id':self.item.pk, 'quantity_ordered':'1', 'unit_cost':'1000.00'}])
        submit_purchase_order(purchase_order=self.po, actor=self.maker)
        approve_purchase_order(purchase_order=self.po, actor=self.checker)
        self.line = self.po.lines.get()

    def pay(self, amount, reference='SYN-REF-1'):
        return record_purchase_order_payment(purchase_order=self.po, actor=self.checker,
            method='BANK_TRANSFER', amount=Decimal(amount), reference=reference)

    def receive(self, quantity, key='syn-receipt-1', po=None, line=None):
        return receive_goods(purchase_order=po or self.po, actor=self.maker, source_key=key,
            receipt_lines=[{'purchase_order_line_id': (line or self.line).pk, 'quantity_received':quantity}])

    def test_half_payment_is_not_fully_paid_and_final_payment_is(self):
        self.pay('500.00')
        self.po.refresh_from_db()
        self.assertEqual(self.po.total_paid, Decimal('500.00'))
        self.assertEqual(self.po.outstanding_amount, Decimal('500.00'))
        self.assertEqual(self.po.status, PurchaseOrder.Status.APPROVED)
        self.assertIsNone(self.po.paid_at)
        self.pay('500.00', 'SYN-REF-2')
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.Status.PAID)
        self.assertEqual(self.po.outstanding_amount, Decimal('0.00'))
        self.assertIsNotNone(self.po.paid_at)

    def test_sub_cent_internal_payment_cannot_create_a_zero_value_record(self):
        with self.assertRaises(ValidationError):
            self.pay('0.001')
        self.assertEqual(self.po.payments.count(), 0)

    def test_duplicate_nonempty_reference_on_same_order_is_rejected(self):
        self.pay('100.00', 'Syn-Ref-1')
        with self.assertRaises(ValidationError):
            self.pay('100.00', 'syn-ref-1')
        self.assertEqual(self.po.payments.count(), 1)
        self.assertEqual(self.po.total_paid, Decimal('100.00'))

    def test_prepaid_partial_and_final_receipts_preserve_paid_state(self):
        self.pay('1000.00')
        first, created = self.receive('0.4')
        self.assertTrue(created)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.Status.PAID)
        self.assertEqual(self.po.outstanding_amount, Decimal('0.00'))
        second, created = self.receive('0.6', 'syn-receipt-2')
        self.assertTrue(created)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.Status.PAID)
        self.assertEqual(StockBalance.objects.get(item=self.item, location=self.location).quantity_on_hand, Decimal('1.0000'))
        self.assertNotEqual(first.pk, second.pk)

    def test_fully_paid_order_can_file_invoice_reference(self):
        self.pay('1000.00')
        updated = attach_purchase_order_receipt(purchase_order=self.po, actor=self.checker,
                                                upload=None, payment_reference='SYN-INVOICE')
        self.assertEqual(updated.status, PurchaseOrder.Status.PAID)
        self.assertEqual(updated.payment_reference, 'SYN-INVOICE')

    def test_retry_completed_receipt_returns_same_record(self):
        first, created = self.receive('1')
        self.assertTrue(created)
        second, again = self.receive('1')
        self.assertFalse(again)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(StockBalance.objects.get(item=self.item, location=self.location).quantity_on_hand, Decimal('1.0000'))

    def test_same_receipt_key_with_changed_payload_is_rejected(self):
        self.receive('0.4')
        with self.assertRaises(ValidationError):
            self.receive('0.5')
        self.assertEqual(StockBalance.objects.get(item=self.item, location=self.location).quantity_on_hand, Decimal('0.4000'))

    def test_same_receipt_key_cannot_return_another_orders_receipt(self):
        self.receive('0.4')
        other, _ = create_purchase_order(supplier=self.supplier, delivery_location=self.location,
            requested_by=self.maker, lines=[{'item_id':self.item.pk, 'quantity_ordered':'1', 'unit_cost':'1000.00'}])
        submit_purchase_order(purchase_order=other, actor=self.maker)
        approve_purchase_order(purchase_order=other, actor=self.checker)
        with self.assertRaises(ValidationError):
            self.receive('0.4', po=other, line=other.lines.get())

    def test_api_receipt_retry_returns_200_without_duplicate_stock(self):
        client=APIClient(); client.force_authenticate(self.maker)
        payload={'idempotency_key':'synthetic-api-key', 'lines':[{'purchase_order_line_id':self.line.pk, 'quantity_received':'1'}]}
        url=f'/api/admin/inventory/purchase-orders/{self.po.reference}/receipts/'
        first=client.post(url,payload,format='json'); second=client.post(url,payload,format='json')
        self.assertEqual(first.status_code,201)
        self.assertEqual(second.status_code,200)
        self.assertEqual(first.json()['data']['reference'],second.json()['data']['reference'])
        self.assertEqual(StockBalance.objects.get(item=self.item, location=self.location).quantity_on_hand, Decimal('1.0000'))
