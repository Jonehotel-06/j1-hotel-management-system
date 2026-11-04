"""Executed isolated regression tests for the November repair; no external money calls."""
import importlib
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.core.exceptions import RoomUnavailableError
from apps.bookings.services.booking_service import create_booking
from apps.core.utils import hotel_today
from apps.enquiries.models import Enquiry
from apps.enquiries.serializers import EnquiryAdminSerializer
from apps.enquiries.services import approve_cancellation, reject_cancellation
from apps.finance.models import ApprovalRequest, CashSession, FinancialControlPolicy, FinancialTransaction, FolioPosting
from apps.finance.services.approval_service import active_control_policy, requires_manager_approval
from apps.finance.services.cash_session_service import close_cash_session, open_cash_session
from apps.guest_services.models import ServiceRequest, ServiceRequestEvent
from apps.guest_services.services.delivery_service import confirm_guest_receipt
from apps.guest_services.services.request_service import claim_service_request, transition_service_request, assign_service_request
from apps.portal.models import PortalSession
from apps.portal.services import hash_secret
from apps.pos.models import PosOrder, MenuCategory, MenuItem
from apps.staff_operations.models import AttendanceRecord, ShiftAssignment
from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type, make_staff
from .portal_stay_helpers import make_in_house_stay


class PolicyRepairTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.admin=make_staff('controls-admin@example.test',role=User.Role.ADMIN)
        self.migration=importlib.import_module('apps.finance.migrations.0022_initialize_missing_control_policy')

    def repair(self):
        with connection.schema_editor() as editor:
            self.migration.initialize_missing_policy(apps,editor)

    def test_forward_repair_restores_empty_table_without_weakening_thresholds(self):
        FinancialControlPolicy.objects.all().delete()
        # no schema mutation is required by this data migration
        self.migration.initialize_missing_policy(apps, SimpleNamespace(connection=connection))
        p=active_control_policy()
        self.assertTrue(p.require_separate_approver)
        self.assertEqual(p.refund_manager_approval_threshold,0)
        self.assertEqual(p.expense_manager_approval_threshold,0)
        self.migration.initialize_missing_policy(apps, SimpleNamespace(connection=connection))
        self.assertEqual(FinancialControlPolicy.objects.count(),1)

    def test_repair_never_reactivates_or_overwrites_configuration(self):
        FinancialControlPolicy.objects.update(is_active=False,refund_manager_approval_threshold=Decimal('27'))
        self.migration.initialize_missing_policy(apps, SimpleNamespace(connection=connection))
        self.assertFalse(FinancialControlPolicy.objects.filter(is_active=True).exists())
        self.assertEqual(FinancialControlPolicy.objects.first().refund_manager_approval_threshold,27)
        with self.assertRaisesMessage(ValidationError,'activation and effective dates'):
            active_control_policy()

    def test_missing_and_future_policies_fail_closed(self):
        FinancialControlPolicy.objects.all().delete()
        with self.assertRaises(ValidationError):active_control_policy()
        FinancialControlPolicy.objects.create(name='future',effective_from=hotel_today()+timedelta(days=1))
        with self.assertRaises(ValidationError):active_control_policy()

    def test_thresholds_below_equal_above_and_invalid_type(self):
        p=active_control_policy();p.refund_manager_approval_threshold=Decimal('100');p.save()
        for amount,expected in [('99.99',False),('100.00',True),('100.01',True)]:
            with self.subTest(amount=amount):self.assertEqual(requires_manager_approval(request_type='REFUND',amount=amount),expected)
        with self.assertRaises(ValidationError):requires_manager_approval(request_type='invented',amount=1)
        for amount in ['0','-1']:
            with self.subTest(amount=amount),self.assertRaises(ValidationError):requires_manager_approval(request_type='REFUND',amount=amount)

    def test_zero_variance_observation_does_not_depend_on_money_release_policy(self):
        session=open_cash_session(cashier=self.admin,actor=self.admin,opening_float=100)
        FinancialControlPolicy.objects.all().delete()
        result=close_cash_session(cash_session=session,cashier=self.admin,counted_cash=100)
        self.assertEqual(result.status,CashSession.Status.CLOSED)
        self.assertEqual(result.variance,0)
        count=result.movements.count()
        close_cash_session(cash_session=result,cashier=self.admin,counted_cash=100)
        self.assertEqual(result.movements.count(),count)

    def test_nonzero_variance_without_policy_rolls_back(self):
        session=open_cash_session(cashier=self.admin,actor=self.admin,opening_float=100)
        FinancialControlPolicy.objects.all().delete()
        with self.assertRaises(ValidationError):close_cash_session(cash_session=session,cashier=self.admin,counted_cash=90)
        session.refresh_from_db();self.assertEqual(session.status,'OPEN');self.assertIsNone(session.counted_cash)

    def test_policy_config_api_denies_receptionist(self):
        self.auth(make_staff('controls-desk@example.test'))
        response=self.client.get('/api/admin/finance/control-policies/')
        self.assertEqual(response.status_code,403)


class GuestReceiptRepairTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.guest=make_guest('receipt-owner@example.test');self.kind=make_room_type();self.room=make_room(self.kind,'6')
        booking=make_booking(self.guest,self.kind,[self.room],check_in=hotel_today(),check_out=hotel_today()+timedelta(days=2),status=Booking.Status.CHECKED_IN)
        self.stay=make_in_house_stay(booking)
        self.waiter=make_staff('receipt-worker@example.test',role=User.Role.WAITER)
        self.desk=make_staff('receipt-desk@example.test')
        self.request=ServiceRequest.objects.create(reference='SRQ-RECEIPT',guest=self.guest,stay=self.stay,room=self.room,portal_email=self.guest.email,category='ROOM_SERVICE',channel='PORTAL',summary='Drinks',status='ACKNOWLEDGED')
        from apps.finance.services.folio_service import get_or_create_main_folio_for_stay
        folio,_=get_or_create_main_folio_for_stay(stay=self.stay,actor=self.waiter)
        self.order=PosOrder.objects.create(folio=folio,reference='POS-RECEIPT',service_request=self.request,stay=self.stay,mode='ROOM_SERVICE',status='DELIVERED',total_amount=Decimal('1000'),metadata={'portal_stock_rules':{}})
        self.token='receipt-test-only-opaque-session-token-1234567890'
        self.session=PortalSession.objects.create(email=self.guest.email,session_hash=hash_secret(self.token),expires_at=timezone.now()+timedelta(hours=1))
        self.url=f'/api/portal/requests/{self.request.reference}/confirm-delivery/'

    def test_correct_guest_confirms_once_without_financial_settlement(self):
        before=(FinancialTransaction.objects.count(),FolioPosting.objects.count())
        response=self.client.post(self.url,{},format='json',HTTP_X_PORTAL_SESSION=self.token)
        self.assertEqual(response.status_code,200,response.content)
        self.assertIsNotNone(response.json()['data']['delivery_confirmed_at'])
        self.order.refresh_from_db();self.request.refresh_from_db()
        self.assertEqual(self.request.status,'RESOLVED')
        confirmation=self.request.metadata['guest_delivery_confirmation']
        self.assertEqual(confirmation['guest_email'],self.guest.email)
        self.assertEqual(confirmation['portal_session_id'],self.session.pk)
        self.assertEqual(before,(FinancialTransaction.objects.count(),FolioPosting.objects.count()))
        self.assertEqual(self.order.tenders.count(),0)
        repeat=self.client.post(self.url,{},format='json',HTTP_X_PORTAL_SESSION=self.token)
        self.assertEqual(repeat.status_code,400,repeat.content)
        self.assertEqual(ServiceRequestEvent.objects.filter(request=self.request).count(),1)

    def test_other_guest_is_denied_even_with_order_reference(self):
        other_guest=make_guest('other@example.test');other_room=make_room(self.kind,'20')
        other_booking=make_booking(other_guest,self.kind,[other_room],check_in=hotel_today(),check_out=hotel_today()+timedelta(days=2),status=Booking.Status.CHECKED_IN)
        make_in_house_stay(other_booking)
        other=PortalSession.objects.create(email='other@example.test',session_hash=hash_secret('another-opaque-session-token-12345678901234'),expires_at=timezone.now()+timedelta(hours=1))
        with self.assertRaises(PermissionDenied):confirm_guest_receipt(service_request=self.request,portal_session=other)
        response=self.client.post(self.url,{},format='json',HTTP_X_PORTAL_SESSION='another-opaque-session-token-12345678901234')
        self.assertIn(response.status_code,[403,404])
        self.request.refresh_from_db();self.assertNotIn('guest_delivery_confirmation',self.request.metadata)

    def test_staff_jwt_cannot_use_guest_confirmation(self):
        self.auth(self.desk)
        response=self.client.post(self.url,{},format='json')
        self.assertIn(response.status_code,[401,403])

    def test_confirmation_before_physical_delivery_is_denied(self):
        self.order.status='READY';self.order.save()
        with self.assertRaises(ValidationError):confirm_guest_receipt(service_request=self.request,portal_session=self.session)

    def test_retired_transitions_and_claim_are_rejected(self):
        for target in ['IN_PROGRESS','ESCALATED','CANCELLED']:
            with self.subTest(target=target),self.assertRaises(ValidationError):transition_service_request(service_request=self.request,target_status=target,actor=self.waiter)
        with self.assertRaises(ValidationError):claim_service_request(service_request=self.request,actor=self.desk)

    def test_receptionist_cannot_assign_or_confirm_through_staff_status(self):
        for target in ['RESOLVED','CLOSED']:
            with self.subTest(target=target),self.assertRaises(PermissionDenied):transition_service_request(service_request=self.request,target_status=target,actor=self.desk)
        with self.assertRaises(PermissionDenied):assign_service_request(service_request=self.request,actor=self.desk,assigned_to=self.desk)

    def test_confirmation_failure_rolls_back_request_status_and_metadata(self):
        with patch('apps.guest_services.services.delivery_service.ServiceRequestEvent.objects.create',side_effect=RuntimeError('simulated audit storage failure')):
            with self.assertRaises(RuntimeError):confirm_guest_receipt(service_request=self.request,portal_session=self.session)
        self.request.refresh_from_db();self.assertEqual(self.request.status,'ACKNOWLEDGED');self.assertEqual(self.request.metadata,{})


class CancellationPermissionRepairTests(BaseAPITestCase):
    def setUp(self):
        super().setUp();self.desk=make_staff('cancel-desk@example.test');self.manager=make_staff('cancel-manager@example.test',role=User.Role.GENERAL_MANAGER)
        guest=make_guest();kind=make_room_type();room=make_room(kind,'20');self.booking=make_booking(guest,kind,[room])
        self.enquiry=Enquiry.objects.create(name='Guest',email=guest.email,subject='Cancellation',message='Changed travel',enquiry_type='CANCELLATION',cancellation_reference='CAN-REPAIR',related_booking=self.booking,cancellation_reason='Changed travel')

    def test_receptionist_cannot_approve_or_reject_api_or_service(self):
        self.auth(self.desk)
        for action in ['approve-cancellation','reject-cancellation']:
            response=self.client.post(f'/api/admin/enquiries/{self.enquiry.pk}/{action}/',{},format='json')
            self.assertEqual(response.status_code,403,response.content)
        for service in [approve_cancellation,reject_cancellation]:
            with self.assertRaises(DRFPermissionDenied):service(self.enquiry.pk,staff_user=self.desk)
        self.booking.refresh_from_db();self.assertEqual(self.booking.status,'CONFIRMED')

    def test_direct_booking_cancellation_is_also_management_only(self):
        from apps.bookings.services.booking_service import cancel_booking
        self.auth(self.desk)
        response=self.client.post(f'/api/admin/bookings/{self.booking.booking_reference}/cancel/',{'reason':'Attempted bypass'},format='json')
        self.assertEqual(response.status_code,403,response.content)
        with self.assertRaises(PermissionDenied):cancel_booking(self.booking,staff=True,by_user=self.desk)
        self.booking.refresh_from_db();self.assertEqual(self.booking.status,'CONFIRMED')

    def test_serializer_does_not_advertise_management_actions_to_desk(self):
        data=EnquiryAdminSerializer(self.enquiry,context={'request':SimpleNamespace(user=self.desk)}).data
        self.assertNotIn('approve_cancellation',data['actions']);self.assertNotIn('reject_cancellation',data['actions'])
        manager=EnquiryAdminSerializer(self.enquiry,context={'request':SimpleNamespace(user=self.manager)}).data
        self.assertIn('approve_cancellation',manager['actions'])

    def test_management_rejection_preserves_reason_and_identity(self):
        from rest_framework.exceptions import ValidationError as APIValidationError
        with self.assertRaises(APIValidationError):reject_cancellation(self.enquiry.pk,staff_user=self.manager)
        result=reject_cancellation(self.enquiry.pk,staff_user=self.manager,resolution='Dates remain valid')
        self.assertEqual(result.processed_by_id,self.manager.pk);self.assertEqual(result.resolution,'Dates remain valid')
        with self.assertRaises(Exception):approve_cancellation(self.enquiry.pk,staff_user=self.manager)

    def test_management_approval_cannot_be_replayed_or_reversed_by_rejection(self):
        result=approve_cancellation(self.enquiry.pk,staff_user=self.manager,notes='Authorized decision')
        self.assertEqual(result.processed_by_id,self.manager.pk)
        self.booking.refresh_from_db();self.assertEqual(self.booking.status,'CANCELLED')
        for service in [approve_cancellation,reject_cancellation]:
            with self.assertRaises(Exception):service(self.enquiry.pk,staff_user=self.manager)


class ExactWalkInRepairTests(BaseAPITestCase):
    def setUp(self):
        super().setUp();self.staff=make_staff('room-desk@example.test');self.kind=make_room_type();self.one=make_room(self.kind,'1');self.six=make_room(self.kind,'6');self.guest=make_guest();self.start=hotel_today()+timedelta(days=1);self.end=self.start+timedelta(days=2)

    def create(self,room):
        return create_booking(room_type_value=self.kind.slug,check_in=self.start,check_out=self.end,rooms=1,adults=1,children=0,guest_data={'first_name':'Walk','last_name':'Guest','email':'walk-exact@example.test','phone':'08031234567'},source='WALK_IN',require_payment=False,actor=self.staff,room_id=room.pk)

    def test_room_six_is_exactly_assigned_at_existing_price(self):
        booking=self.create(self.six)
        self.assertEqual(booking.room_assignments.get().room_id,self.six.pk)
        self.assertEqual(booking.price_per_night,self.kind.base_price)

    def test_reserved_exact_room_is_not_substituted_with_free_room_one(self):
        make_booking(self.guest,self.kind,[self.six],check_in=self.start,check_out=self.end)
        with self.assertRaises(Exception):self.create(self.six)
        self.assertFalse(Booking.objects.filter(guest__email='walk-exact@example.test').exists())

    def test_wrong_type_is_rejected(self):
        other=make_room(make_room_type('Other'),'20')
        with self.assertRaises(Exception):self.create(other)

    def test_available_endpoint_excludes_reserved_selection(self):
        make_booking(self.guest,self.kind,[self.six],check_in=self.start,check_out=self.end)
        response=self.client.get(f'/api/rooms/{self.kind.slug}/rooms/',{'check_in':str(self.start),'check_out':str(self.end)})
        self.assertEqual(response.status_code,200,response.content)
        rooms={row['room_number']:row['available'] for row in response.json()['data']}
        self.assertEqual(rooms,{'1':True,'6':False})


class AttendanceAndMenuRepairTests(BaseAPITestCase):
    def setUp(self):
        super().setUp();self.admin=make_staff('workforce-admin@example.test',role=User.Role.ADMIN);self.worker=make_staff('worker-search@example.test',role=User.Role.WAITER);self.auth(self.admin)
        now=timezone.now();self.date=hotel_today()-timedelta(days=3)
        self.record=AttendanceRecord.objects.create(reference='ATT-SEARCH',staff=self.worker,business_date=self.date,clock_in_at=now-timedelta(days=3),last_event_at=now-timedelta(days=3))

    def test_history_search_date_range_and_pagination_return_genuine_record(self):
        url='/api/admin/staff-operations/attendance/'
        for params in [{},{'search':'worker-search'},{'date_from':str(self.date),'date_to':str(self.date),'page_size':1}]:
            response=self.client.get(url,params);self.assertEqual(response.status_code,200,response.content)
            rows=response.json()['data'];self.assertEqual([row['reference'] for row in rows],['ATT-SEARCH'])
            self.assertEqual(rows[0]['arrival_classification'],'UNSCHEDULED');self.assertIsNone(rows[0]['clock_out_at'])
        invalid=self.client.get(url,{'date_from':'invalid'});self.assertEqual(invalid.status_code,400)

    def test_search_cannot_escape_worker_own_scope(self):
        self.auth(make_staff('other-worker@example.test',role=User.Role.WAITER))
        response=self.client.get('/api/admin/staff-operations/attendance/',{'search':'worker-search'})
        self.assertEqual(response.status_code,200,response.content);self.assertEqual(response.json()['data'],[])

    def test_all_active_categories_are_returned_without_duplicates_and_modes_remain_scoped(self):
        for area in ['RESTAURANT','BAR']:
            cat=MenuCategory.objects.create(name=area,slug=area.lower(),service_area=area)
            MenuItem.objects.create(category=cat,name=area+' item',sku=area,base_price=100)
        MenuCategory.objects.create(name='Inactive',slug='inactive',is_active=False)
        response=self.client.get('/api/admin/pos/menu/')
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual({row['service_area'] for row in response.json()['data']},{'RESTAURANT','BAR'})
        self.assertEqual(len(response.json()['data']),2)
        bar=self.client.get('/api/admin/pos/menu/',{'area':'BAR'})
        self.assertEqual(len(bar.json()['data']),1);self.assertEqual(bar.json()['data'][0]['service_area'],'BAR')


from .test_pos_unopened_returns import UnopenedReturnTestBase
from apps.pos.services.return_service import record_unopened_return
from apps.pos.models import PosOrderReturn
from apps.inventory.models import StockMovement


class ReturnIntegrityRepairTests(UnopenedReturnTestBase):
    def test_missing_policy_paid_return_fails_atomically_without_stock_or_money_change(self):
        order=self._deliver_bar_order(4);self._pay(order,Decimal('8000'))
        before=self.balance();transactions=FinancialTransaction.objects.count()
        FinancialControlPolicy.objects.all().delete()
        with self.assertRaises(ValidationError):record_unopened_return(order=order,lines=[{'order_line_id':order.lines.first().pk,'quantity_returned':1}],actor=self.bartender,idempotency_key='missing-policy-paid-return')
        self.assertEqual(self.balance(),before);self.assertEqual(FinancialTransaction.objects.count(),transactions);self.assertFalse(PosOrderReturn.objects.exists())

    def test_unpaid_return_does_not_require_unrelated_refund_policy(self):
        order=self._deliver_bar_order(4);FinancialControlPolicy.objects.all().delete()
        record,_=record_unopened_return(order=order,lines=[{'order_line_id':order.lines.first().pk,'quantity_returned':1}],actor=self.bartender)
        self.assertEqual(record.refund_due,0);self.assertIsNone(record.refund_transaction_id);self.assertEqual(self.balance(),Decimal('197'))

    def test_injected_credit_failure_rolls_back_stock_and_return_record(self):
        order=self._deliver_bar_order(4);before=self.balance();transactions=FinancialTransaction.objects.count()
        with patch('apps.pos.services.return_service._post_return_credit',side_effect=RuntimeError('simulated ledger storage failure')):
            with self.assertRaises(RuntimeError):record_unopened_return(order=order,lines=[{'order_line_id':order.lines.first().pk,'quantity_returned':1}],actor=self.bartender)
        self.assertEqual(self.balance(),before);self.assertFalse(PosOrderReturn.objects.exists());self.assertEqual(FinancialTransaction.objects.count(),transactions)
        self.assertFalse(StockMovement.objects.filter(type='POS_RETURN').exists())

    def test_unpaid_guest_folio_return_credits_only_guest_charge_not_cash(self):
        from apps.finance.services.folio_service import get_or_create_main_folio_for_stay
        from apps.pos.services.order_service import create_order,submit_order,transition_order
        guest=make_guest('return-room-guest@example.test');kind=make_room_type('Return Suite');room=make_room(kind,'RETURN-6')
        booking=make_booking(guest,kind,[room],check_in=hotel_today(),check_out=hotel_today()+timedelta(days=2),status=Booking.Status.CHECKED_IN)
        stay=make_in_house_stay(booking);folio,_=get_or_create_main_folio_for_stay(stay=stay,actor=self.manager)
        order,_=create_order(mode='ROOM_SERVICE',stay=stay,folio=folio,actor=self.manager,lines=[{'menu_item_id':self.beer_menu.pk,'quantity':4}],idempotency_key='room-unpaid-return')
        submit_order(order=order,actor=self.manager);order=transition_order(order=order,target_status='DELIVERED',actor=self.manager)
        from apps.finance.services.folio_service import folio_balance_queryset
        before=folio_balance_queryset().get(pk=folio.pk).balance
        record,_=record_unopened_return(order=order,lines=[{'order_line_id':order.lines.first().pk,'quantity_returned':1}],actor=self.bartender)
        folio.refresh_from_db();self.assertEqual(folio_balance_queryset().get(pk=folio.pk).balance,before-Decimal('2000'));self.assertEqual(record.refund_due,0);self.assertIsNone(record.refund_transaction_id)
        self.assertEqual(self.balance(),Decimal('197'))

    def test_return_restock_uses_original_sale_mapping_after_menu_configuration_changes(self):
        from apps.inventory.models import StockLocation,StockItem
        order=self._deliver_bar_order(4)
        new_location=StockLocation.objects.create(code='NEW-RETURN-STORE',name='Changed configuration')
        StockItem.objects.filter(pk=self.beer.pk).update(direct_sale_quantity=2,direct_sale_location=new_location)
        record,_=record_unopened_return(order=order,lines=[{'order_line_id':order.lines.first().pk,'quantity_returned':1}],actor=self.bartender)
        self.assertEqual(self.balance(),Decimal('197'))
        movement=StockMovement.objects.get(type='POS_RETURN');self.assertEqual(movement.location_id,self.location.pk);self.assertEqual(movement.quantity_delta,1)

    def test_changed_payload_cannot_replay_existing_return_key(self):
        order=self._deliver_bar_order(4);line=order.lines.first()
        record_unopened_return(order=order,lines=[{'order_line_id':line.pk,'quantity_returned':1}],actor=self.bartender,idempotency_key='same-return-key')
        with self.assertRaises(ValidationError):record_unopened_return(order=order,lines=[{'order_line_id':line.pk,'quantity_returned':2}],actor=self.bartender,idempotency_key='same-return-key')
        for changed in [{'refund_method':'CARD'},{'reason':'Changed'},{'restock_location':self.location}]:
            with self.subTest(changed=changed),self.assertRaises(ValidationError):
                record_unopened_return(order=order,lines=[{'order_line_id':line.pk,'quantity_returned':1}],actor=self.bartender,idempotency_key='same-return-key',**changed)
        self.assertEqual(self.balance(),Decimal('197'));self.assertEqual(PosOrderReturn.objects.count(),1)

    def test_refund_approval_cannot_be_bypassed_by_changing_refund_status(self):
        from apps.pos.services.return_service import confirm_return_refund
        FinancialControlPolicy.objects.update(refund_manager_approval_threshold=0)
        order=self._deliver_bar_order(4);self._pay(order,Decimal('8000'))
        record,_=record_unopened_return(order=order,lines=[{'order_line_id':order.lines.first().pk,'quantity_returned':1}],actor=self.bartender)
        # Simulate imported failure state with an undecided approval attached.
        record.refund_status='FAILED';record.save(update_fields=['refund_status','updated_at'])
        with self.assertRaises(ValidationError):confirm_return_refund(record=record,actor=self.manager,refund_method='CARD',provider_reference='LOCAL-NO-PROVIDER-CALL')
        record.refresh_from_db();self.assertIsNone(record.refund_transaction_id)
