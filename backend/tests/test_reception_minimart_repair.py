"""Scoped integration/security tests for the reception repair and retail checkout."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from threading import Barrier
from django.db import close_old_connections, connection
from django.core.exceptions import ValidationError as DjangoValidationError
from django.test import TransactionTestCase, skipUnlessDBFeature
from rest_framework.exceptions import ValidationError
from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import CashMovement, FinancialTransaction
from apps.finance.services.cash_session_service import open_cash_session
from apps.guest_services.models import MiniMartProduct, MiniMartSale, MiniMartSaleLine, BreakfastIssuance
from apps.guest_services.minimart_service import complete_sale
from apps.guest_services.services_amenities import issue_guest_amenity, get_breakfast_config
from apps.inventory.models import StockItem, StockLocation, StockBalance, StockMovement
from apps.inventory.services.inventory_service import record_stock_movement
from apps.pos.models import PosOrder, PosTender
from tests.base import BaseAPITestCase
from tests.factories import make_staff, make_guest, make_room_type, make_room, make_booking

BASE = '/api/admin/service-requests/'
MART = BASE + 'reception/minimart/'


class RetailFixtures:
    def retail_setup(self):
        self.staff = make_staff('mart@staff.dev', role=User.Role.RECEPTIONIST)
        self.other = make_staff('mart-other@staff.dev', role=User.Role.RECEPTIONIST)
        self.store = StockLocation.objects.create(code='RECEPTION', name='Reception retail shelf')
        self.item = StockItem.objects.create(sku='TEST-WATER', name='Test bottled water', category='Test drinks',
                                              item_type='SELLABLE_PRODUCT', base_unit='BOTTLE', standard_cost='20', reorder_level='2')
        self.product = MiniMartProduct.objects.create(item=self.item, location=self.store, selling_price='150')
        record_stock_movement(item=self.item, location=self.store, movement_type='OPENING', quantity_delta='5', source_key='test-mart-opening')
        self.session = open_cash_session(cashier=self.staff, actor=self.staff)
        self.payload = {'expected_total':'300.00', 'idempotency_key':'test-checkout', 'cash_session_reference':self.session.reference,
                        'lines':[{'product_id':self.product.pk, 'quantity':2}]}

    def balance(self):
        return StockBalance.objects.get(item=self.item, location=self.store).quantity_on_hand


class MiniMartAPITests(RetailFixtures, BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.retail_setup()
        self.auth(self.staff)

    def checkout(self, data=None):
        return self.client.post(MART+'checkout/', data or self.payload, format='json')

    def test_catalogue_has_real_stock_and_no_confidential_fields(self):
        response=self.client.get(MART+'products/')
        self.assertEqual(response.status_code,200)
        data=response.data['data']; row=data['results'][0]
        self.assertEqual(Decimal(row['quantity']),5)
        self.assertEqual(row['name'],self.item.name)
        self.assertEqual(row['category'],self.item.category)
        self.assertEqual(row['location'],self.store.name)
        self.assertNotIn('standard_cost',str(data))
        self.assertNotIn('supplier',str(data))
        # Batch 5: the catalogue advertises every supported method.
        self.assertEqual(data['payment_methods'],['CASH','CARD','BANK_TRANSFER','PAYSTACK'])

    def test_cash_sale_updates_existing_ledgers_once(self):
        response=self.checkout()
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(self.balance(),3)
        sale=MiniMartSale.objects.get()
        self.assertEqual(sale.order.settlement_status,'PAID')
        self.assertEqual(sale.order.total_amount,300)
        self.assertEqual(PosTender.objects.get().amount,300)
        self.assertEqual(CashMovement.objects.count(),1)
        self.session.refresh_from_db();self.assertEqual(self.session.expected_cash,300)
        self.assertEqual(FinancialTransaction.objects.filter(source_reference=sale.order.reference).count(),2)
        self.assertTrue(sale.order.charge_transaction.lines.filter(account_code='REVENUE.OTHER_OPERATING').exists())
        link=MiniMartSaleLine.objects.get()
        self.assertEqual(link.movement.quantity_delta,-2)
        self.assertEqual(link.movement.location,self.store)
        self.assertEqual(link.movement.source_reference,sale.order.reference)

    def test_identical_request_replays_even_after_stock_exhausted(self):
        self.payload['lines'][0]['quantity']=5
        self.payload['expected_total']='750.00'
        self.assertEqual(self.checkout().status_code,200)
        response=self.checkout()
        self.assertEqual(response.status_code,200,response.data)
        self.assertFalse(response.data['data']['created'])
        self.assertEqual(self.balance(),0)
        self.assertEqual(MiniMartSale.objects.count(),1)
        self.assertEqual(PosTender.objects.count(),1)

    def test_stale_price_confirmation_is_rejected_without_any_posting(self):
        self.product.selling_price='200';self.product.save()
        self.assertEqual(self.checkout().status_code,400)
        self.assertEqual(self.balance(),5)
        self.assertFalse(MiniMartSale.objects.exists())

    def test_same_key_different_cart_is_rejected(self):
        self.checkout(); self.payload['lines'][0]['quantity']=1
        self.assertEqual(self.checkout().status_code,400)
        self.assertEqual(self.balance(),3)

    def test_negative_zero_fractional_and_excessive_quantities_rejected(self):
        for quantity in [-1,0,1.5,6,1000]:
            with self.subTest(quantity=quantity):
                self.payload['lines'][0]['quantity']=quantity
                self.assertEqual(self.checkout().status_code,400)
                self.assertEqual(self.balance(),5)
        self.assertFalse(MiniMartSale.objects.exists())

    def test_duplicate_lines_rejected(self):
        self.payload['lines'] *= 2
        self.assertEqual(self.checkout().status_code,400)
        self.assertEqual(self.balance(),5)

    def test_client_price_payment_status_and_stock_fields_rejected(self):
        for key,value in [('price','0.01'),('payment_status','PAID'),('payment_method','BANK_TRANSFER'),('quantity_on_hand',900)]:
            with self.subTest(field=key):
                self.assertEqual(self.checkout(dict(self.payload,**{key:value})).status_code,400)
        self.assertEqual(self.balance(),5)

    def test_closed_missing_or_other_staff_cash_session_rejected(self):
        other=open_cash_session(cashier=self.other,actor=self.other)
        for reference in ['unknown',other.reference]:
            self.assertEqual(self.checkout(dict(self.payload,cash_session_reference=reference)).status_code,400)
        self.session.status='CLOSED';self.session.save()
        self.assertEqual(self.checkout().status_code,400)
        self.assertEqual(self.balance(),5)

    def test_failed_capture_rolls_back_stock_sale_and_finance(self):
        with patch('apps.guest_services.minimart_service.capture_direct_tender',side_effect=ValidationError('Payment failed')):
            self.assertEqual(self.checkout().status_code,400)
        self.assertEqual(self.balance(),5)
        self.assertFalse(MiniMartSale.objects.exists())
        self.assertFalse(PosOrder.objects.exists())
        self.assertFalse(FinancialTransaction.objects.exists())
        self.assertFalse(StockMovement.objects.filter(type='ISSUE').exists())

    def test_cart_browsing_and_abandonment_do_not_deduct(self):
        self.client.get(MART+'products/');self.client.get(MART+'sales/')
        self.assertEqual(self.balance(),5)
        self.assertFalse(MiniMartSale.objects.exists())

    def test_inventory_changes_are_visible(self):
        record_stock_movement(item=self.item,location=self.store,movement_type='ISSUE',quantity_delta='-4',source_key='elsewhere')
        row=self.client.get(MART+'products/').data['data']['results'][0]
        self.assertEqual(Decimal(row['quantity']),1);self.assertTrue(row['low_stock'])
        self.assertEqual(self.checkout().status_code,400)

    def test_inactive_and_non_sellable_items_cannot_be_sold(self):
        for field,value in [('is_active',False),('item_type','REUSABLE_ASSET')]:
            original=getattr(self.item,field);setattr(self.item,field,value);self.item.save()
            self.assertEqual(self.checkout().status_code,400)
            self.assertEqual(self.client.get(MART+'products/').data['data']['results'],[])
            setattr(self.item,field,original);self.item.save()
        self.assertEqual(self.balance(),5)

    def test_restaurant_return_path_cannot_reverse_retail_sale(self):
        from apps.pos.services.return_service import record_unopened_return, eligible_orders
        self.checkout()
        order=MiniMartSale.objects.get().order
        admin=make_staff('return-boss@staff.dev',role=User.Role.ADMIN)
        self.assertFalse(any(o.pk == order.pk for o in eligible_orders()))
        with self.assertRaises(DjangoValidationError):
            record_unopened_return(order=order, lines=[{'order_line_id':order.lines.get().pk,'quantity_returned':1}], actor=admin,reason='test')
        self.assertEqual(self.balance(),3)

    def test_walkin_needs_no_guest_or_booking(self):
        self.assertEqual(self.checkout().status_code,200)
        sale=MiniMartSale.objects.get();self.assertIsNone(sale.guest_id)
        self.assertEqual(sale.order.guest_name,'Walk-in customer')

    def test_guest_sale_uses_real_guest_room_and_dates(self):
        guest=make_guest();rt=make_room_type();room=make_room(rt,'101')
        booking=make_booking(guest,rt,rooms=[room],check_in=hotel_today(),check_out=hotel_today()+timedelta(days=2))
        response=self.checkout(dict(self.payload,guest_id=guest.pk,booking_id=booking.pk))
        self.assertEqual(response.status_code,200,response.data)
        sale=response.data['data']['sale'];self.assertEqual(sale['guest_name'],'Ada Obi');self.assertEqual(sale['room_number'],'101')

    def test_guest_booking_mismatch_rejected(self):
        g=make_guest();other=make_guest('other@example.test');booking=make_booking(other,make_room_type())
        self.assertEqual(self.checkout(dict(self.payload,guest_id=g.pk,booking_id=booking.pk)).status_code,400)
        self.assertEqual(self.balance(),5)

    def test_sales_history_search_date_filter_and_summary(self):
        self.checkout(); today=hotel_today().isoformat()
        response=self.client.get(MART+'sales/',{'start':today,'end':today,'search':'water'})
        self.assertEqual(response.status_code,200,response.data)
        data=response.data['data'];self.assertEqual(data['count'],1)
        self.assertEqual(data['summary']['items_sold'],2)
        self.assertEqual(Decimal(data['summary']['total_sales']),300)
        self.assertEqual(data['results'][0]['payment_status'],'PAID')
        self.assertEqual(self.client.get(MART+'sales/',{'start':'invalid'}).status_code,400)
        self.assertEqual(self.client.get(MART+'sales/',{'page':'-1'}).status_code,400)
        self.assertEqual(self.client.get(MART+'sales/',{'search':'no-match'}).data['data']['count'],0)

    def test_receptionist_history_is_own_only_management_sees_all(self):
        self.checkout();self.auth(self.other)
        self.assertEqual(self.client.get(MART+'sales/').data['data']['count'],0)
        self.auth(make_staff('boss@staff.dev',role=User.Role.ADMIN))
        self.assertEqual(self.client.get(MART+'sales/').data['data']['count'],1)

    def test_unauthenticated_and_other_departments_denied(self):
        self.unauth()
        for endpoint in ['products/','sales/']:
            self.assertEqual(self.client.get(MART+endpoint).status_code,401)
        for role in ['STOREKEEPER','WAITER','ACCOUNTANT','HOUSEKEEPING']:
            self.auth(make_staff(role+'@staff.dev',role=role))
            self.assertEqual(self.client.get(MART+'products/').status_code,403)
            self.assertEqual(self.checkout().status_code,403)
        self.assertEqual(self.balance(),5)

    def test_no_stock_edit_or_history_mutation_api(self):
        self.assertEqual(self.client.post(MART+'products/',{'quantity':200},format='json').status_code,405)
        self.assertEqual(self.client.patch(MART+'sales/',{'total':0},format='json').status_code,405)


class ReceptionRepairTests(RetailFixtures,BaseAPITestCase):
    def setUp(self):
        super().setUp();self.retail_setup();self.auth(self.staff)
        self.guest=make_guest();self.rt=make_room_type();self.room=make_room(self.rt,'206')
        self.booking=make_booking(self.guest,self.rt,rooms=[self.room],check_in=hotel_today(),check_out=hotel_today()+timedelta(days=2))

    def test_guest_directory_names_rooms_and_booking_dates(self):
        response=self.client.get(BASE+'reception/guests/',{'search':'206'})
        self.assertEqual(response.status_code,200,response.data)
        guest=response.data['data']['results'][0];self.assertEqual(guest['full_name'],'Ada Obi')
        self.assertEqual(guest['bookings'][0]['rooms'],['206'])
        self.assertEqual(str(guest['bookings'][0]['check_in']),str(hotel_today()))

    def test_amenity_wrong_guest_or_room_rejected(self):
        g=make_guest('other@guest.dev')
        for guest,room in [(g,'206'),(self.guest,'999')]:
            with self.assertRaises(ValueError):
                issue_guest_amenity(guest=guest,booking=self.booking,room_number=room,item=self.item,quantity=1,actor=self.staff)
        self.assertEqual(self.balance(),5)

    def test_amenity_retry_when_shelf_empty_does_not_fail_or_deduct_twice(self):
        args=dict(guest=self.guest,booking=self.booking,room_number='206',item=self.item,quantity=5,actor=self.staff,idempotency_key='last-stock')
        issue,created=issue_guest_amenity(**args);again,created=issue_guest_amenity(**args)
        self.assertFalse(created);self.assertEqual(again.pk,issue.pk);self.assertEqual(self.balance(),0)

    def test_breakfast_uses_date_matching_booking_not_later_reservation(self):
        make_booking(self.guest,self.rt)  # future booking must not hide the present stay
        cfg=get_breakfast_config();cfg.is_enabled=True;cfg.save()
        payload={'guest_id':self.guest.pk,'service_date':str(hotel_today()),'idempotency_key':'breakfast-test'}
        response=self.client.post(BASE+'breakfast/issue/',payload,format='json')
        self.assertEqual(response.status_code,200,response.data)
        issue=response.data['data']['issuance'];self.assertEqual(issue['booking_reference'],self.booking.booking_reference)
        self.assertEqual(issue['room_number'],'206');self.assertEqual(issue['guest_name'],'Ada Obi')
        self.assertEqual(self.client.post(BASE+'breakfast/issue/',payload,format='json').status_code,200)
        self.assertEqual(BreakfastIssuance.objects.count(),1);self.assertEqual(self.balance(),5)

    def test_breakfast_invalid_date_and_wrong_guest_eligibility(self):
        self.assertEqual(self.client.get(BASE+'breakfast/eligibility/',{'guest_id':self.guest.pk,'service_date':'invalid'}).status_code,400)
        g=make_guest('bad-booking@guest.dev');cfg=get_breakfast_config();cfg.is_enabled=True;cfg.save()
        response=self.client.get(BASE+'breakfast/eligibility/',{'guest_id':g.pk,'booking_id':self.booking.pk,'service_date':str(hotel_today())})
        self.assertFalse(response.data['data']['eligible'])


class MiniMartConcurrencyTests(RetailFixtures,TransactionTestCase):
    """Runs on a real row-locking backend; never pretend SQLite is MySQL."""
    def setUp(self):
        if not connection.features.has_select_for_update:
            self.skipTest("Requires a real row-locking database; run on MySQL/MariaDB.")
        # TransactionTestCase flush removes migration-seeded grants between
        # methods. Recreate only the grants required by these test cashiers.
        from apps.accounts.models import Capability, RoleCapability
        for code in ("guest_amenity.issue", "payment.capture"):
            capability, _ = Capability.objects.get_or_create(code=code, defaults={"name": code})
            RoleCapability.objects.get_or_create(role=User.Role.RECEPTIONIST, capability=capability)
        self.retail_setup()

    @skipUnlessDBFeature('has_select_for_update')
    def test_two_cashiers_cannot_sell_same_remaining_stock(self):
        other_session=open_cash_session(cashier=self.other,actor=self.other)
        barrier=Barrier(2)
        def sell(staff_id,reference,key):
            close_old_connections()
            try:
                actor=User.objects.get(pk=staff_id)
                data={'expected_total':'750.00','idempotency_key':key,'cash_session_reference':reference,'lines':[{'product_id':self.product.pk,'quantity':5}]}
                barrier.wait(timeout=10)
                try:
                    complete_sale(actor=actor,data=data)
                    return 'sold'
                except ValidationError:
                    return 'insufficient'
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(sell,self.staff.pk,self.session.reference,'concurrent-a')
            b=pool.submit(sell,self.other.pk,other_session.reference,'concurrent-b')
            results=[a.result(),b.result()]
        self.assertCountEqual(results,['sold','insufficient'])
        self.assertEqual(self.balance(),0)
        self.assertEqual(MiniMartSale.objects.count(),1)

    @skipUnlessDBFeature('has_select_for_update')
    def test_concurrent_retries_of_same_checkout_only_post_once(self):
        barrier=Barrier(2)
        def sell():
            close_old_connections()
            try:
                actor=User.objects.get(pk=self.staff.pk)
                barrier.wait(timeout=10)
                _, created=complete_sale(actor=actor,data=self.payload)
                return created
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a,b=pool.submit(sell),pool.submit(sell)
            self.assertCountEqual([a.result(),b.result()],[True,False])
        self.assertEqual(self.balance(),3)
        self.assertEqual(MiniMartSale.objects.count(),1)
        self.assertEqual(PosTender.objects.count(),1)
