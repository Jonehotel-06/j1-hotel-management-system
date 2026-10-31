from datetime import timedelta
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from apps.accounts.models import User, Capability, UserCapabilityOverride
from apps.bookings.models import Booking, RoomOccupant
from apps.housekeeping.models import AssetInspection, AssetInspectionLine, AssetReviewEvent, ReusableItem, RoomAsset, TypeAsset
from apps.stays.models import Stay, StayRoom
from .base import BaseAPITestCase
from .factories import make_staff, make_guest, make_room, make_room_type, make_booking

BASE = '/api/admin/housekeeping/assets/'


class RoomAssetTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff('asset-manager@staff.dev', User.Role.MANAGER)
        self.worker = make_staff('asset-worker@staff.dev', User.Role.HOUSEKEEPING)
        self.guest_user = make_staff('asset-guest@staff.dev', User.Role.GUEST)
        self.rt = make_room_type('Asset suite')
        self.room = make_room(self.rt, 'A01')
        self.room2 = make_room(self.rt, 'A02')
        self.guest = make_guest('asset-guest@test.dev')
        self.booking = make_booking(self.guest, self.rt, [self.room, self.room2], number_of_rooms=2)
        self.assignment = self.booking.room_assignments.get(room=self.room)
        self.other = self.booking.room_assignments.get(room=self.room2)
        self.remote = ReusableItem.objects.create(name='TV remote')
        self.hangers = ReusableItem.objects.create(name='Hangers')
        TypeAsset.objects.create(room_type=self.rt, item=self.remote, quantity=1)
        TypeAsset.objects.create(room_type=self.rt, item=self.hangers, quantity=5)
        RoomAsset.objects.create(room=self.room2, item=self.hangers, quantity=2)
        self.auth(self.worker)

    def start(self, assignment=None, phase='PRE'):
        return self.client.post(BASE + 'inspections/', {'assignment_id': (assignment or self.assignment).pk, 'phase': phase}, format='json')

    def save(self, i, lines, complete=True, revision=0):
        return self.client.post(BASE + f'inspections/{i}/', {'revision':revision, 'complete':complete, 'lines':lines}, format='json')

    def lines(self, remote=1, hangers=5, damaged=False, replaced=False):
        return [{'item_id':self.remote.pk, 'actual':remote, 'condition':'DAMAGED' if damaged else 'GOOD', 'remarks':'test', 'replaced':replaced},
                {'item_id':self.hangers.pk, 'actual':hangers, 'condition':'GOOD'}]

    def checkout(self, assignment=None, booking=None):
        a = assignment or self.assignment; b = booking or self.booking
        a.checked_in_at = timezone.now(); a.save(update_fields=['checked_in_at'])
        stay, _ = Stay.objects.get_or_create(booking=b, defaults={'reference':'STY-'+b.booking_reference, 'guest':b.guest, 'status':'CHECKED_OUT', 'expected_arrival':b.check_in, 'expected_departure':b.check_out})
        return StayRoom.objects.create(stay=stay, room=a.room, booking_room=a, actual_check_in_at=a.checked_in_at, actual_check_out_at=timezone.now(), released_at=timezone.now())

    def test_configuration_overrides_and_catalog_unique(self):
        first = self.start(); second = self.start(self.other)
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual({x['name']:x['expected'] for x in first.data['data']['checklist']}, {'Hangers':5, 'TV remote':1})
        self.assertEqual({x['name']:x['expected'] for x in second.data['data']['checklist']}, {'Hangers':2, 'TV remote':1})
        self.auth(self.manager)
        r = self.client.post(BASE+'catalog/', {'name':'  tv  REMOTE ', 'category':'REUSABLE'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data['data']['id'], self.remote.id)
        r = self.client.post(BASE+'configuration/', {'scope':'room','scope_id':self.room.pk,'item_id':self.remote.pk,'quantity':0}, format='json')
        self.assertEqual(r.status_code, 200)
        soap = self.client.post(BASE+'catalog/', {'name':'Soap', 'category':'CONSUMABLE'}, format='json').data['data']['id']
        self.assertEqual(self.client.post(BASE+'configuration/', {'scope':'room','scope_id':self.room.pk,'item_id':soap,'quantity':2}, format='json').status_code, 400)
        # Existing drafts have a stable snapshot.
        self.assertEqual(len(self.client.get(BASE+'inspections/%s/' % first.data['data']['id']).data['data']['checklist']), 2)
        booking = make_booking(self.guest, self.rt, [self.room]); a = booking.room_assignments.first()
        self.assertEqual(len(self.start(a).data['data']['checklist']), 1)

    def test_pre_post_draft_discrepancy_and_review_history(self):
        i = self.start().data['data']['id']
        self.assertEqual(self.start().data['data']['id'], i)
        self.assertEqual(self.save(i, self.lines()[:1]).status_code, 400)
        self.assertEqual(self.save(i, self.lines()[:1], complete=False).status_code, 200)
        self.assertEqual(self.save(i, self.lines(), revision=0).status_code, 400)
        self.assertEqual(self.save(i, self.lines(), revision=1).status_code, 200)
        self.assertEqual(self.save(i, self.lines(), revision=2).status_code, 400)
        self.checkout()
        r = self.start(phase='POST'); self.assertEqual(r.status_code, 201, r.content)
        post = r.data['data']; self.assertEqual(post['stay'], 'STY-'+self.booking.booking_reference)
        self.assertEqual({x['name']:x['before'] for x in post['checklist']}, {'Hangers':5, 'TV remote':1})
        done = self.save(post['id'], self.lines(remote=0, hangers=3, damaged=True, replaced=True))
        self.assertEqual(done.status_code, 200, done.content)
        self.assertEqual({x['item_name']:x['missing'] for x in done.data['data']['lines']}, {'TV remote':1, 'Hangers':2})
        self.auth(self.manager)
        report = self.client.get(BASE+'discrepancies/'); self.assertEqual(report.status_code, 200, report.content)
        self.assertEqual(len(report.data['data']), 2)
        line = report.data['data'][0]
        self.assertEqual(line['stay'], 'STY-'+self.booking.booking_reference)
        for state in ('INVESTIGATING','RESOLVED'):
            self.assertEqual(self.client.post(BASE+'discrepancies/%d/reviews/' % line['id'], {'status':state,'explanation':'Verified and documented'}, format='json').status_code, 201)
        new = self.client.get(BASE+'discrepancies/').data['data'][0]
        self.assertEqual(new['status'], 'RESOLVED'); self.assertEqual(len(new['history']),2)
        event = AssetReviewEvent.objects.first(); event.explanation = 'tamper'
        with self.assertRaises(DjangoValidationError): event.save()
        line_obj = AssetInspectionLine.objects.get(pk=line['id']); line_obj.actual=999
        with self.assertRaises(DjangoValidationError): line_obj.save()
        record = AssetInspection.objects.get(pk=post['id']); record.checklist=[]
        with self.assertRaises(DjangoValidationError): record.save()

    def test_authorization_and_lifecycle(self):
        self.assertEqual(self.client.get(BASE+'queue/').status_code, 200)
        self.assertEqual(self.client.get(BASE+'discrepancies/').status_code, 403)
        self.assertEqual(self.client.post(BASE+'catalog/', {'name':'Soap', 'category':'CONSUMABLE'}, format='json').status_code, 403)
        self.assertEqual(self.start(phase='POST').status_code, 400)
        i = self.start().data['data']['id']
        self.assignment.checked_in_at=timezone.now(); self.assignment.save(update_fields=['checked_in_at'])
        self.assertEqual(self.save(i,self.lines()).status_code,400)
        self.auth(self.guest_user)
        for path in ['queue/','catalog/','inspections/%s/' % i,'discrepancies/']:
            self.assertEqual(self.client.get(BASE+path).status_code,403)
        self.assertEqual(self.client.post(BASE+'inspections/', {'assignment_id':self.assignment.id,'phase':'PRE'},format='json').status_code,403)

    def test_stays_are_separate_and_room_relationship_is_validated(self):
        pre = self.start().data['data']['id']; self.assertEqual(self.save(pre,self.lines()).status_code,200)
        self.checkout()
        r = self.start(phase='POST'); self.assertEqual(r.status_code,201,r.content)
        self.assertEqual(self.save(r.data['data']['id'],self.lines()).status_code,200)
        next_booking = make_booking(self.guest,self.rt,[self.room]); next_assignment = next_booking.room_assignments.first()
        p = self.start(next_assignment); self.assertEqual(p.status_code,201,p.content)
        self.assertNotEqual(p.data['data']['id'],pre)
        self.assertEqual(self.save(p.data['data']['id'],self.lines()).status_code,200)
        self.assertEqual(AssetInspection.objects.filter(room=self.room,phase='PRE').count(),2)
        # A checkout occupancy for another room cannot validate this assignment.
        other_booking = make_booking(self.guest,self.rt,[self.room2]); other_a=other_booking.room_assignments.first()
        other_a.checked_in_at=timezone.now(); other_a.save(update_fields=['checked_in_at'])
        stay = Stay.objects.create(reference='STY-INVALID', booking=other_booking, guest=self.guest, expected_arrival=other_booking.check_in, expected_departure=other_booking.check_out)
        StayRoom.objects.create(stay=stay,room=self.room,booking_room=other_a,actual_check_out_at=timezone.now())
        self.assertEqual(self.start(other_a,'POST').status_code,400)

    def test_later_occupancy_blocks_misleading_post_inspection(self):
        pre = self.start().data['data']['id']; self.assertEqual(self.save(pre,self.lines()).status_code,200)
        occ = self.checkout(); next_booking = make_booking(self.guest,self.rt,[self.room]); a = next_booking.room_assignments.first()
        stay = Stay.objects.create(reference='STY-LATER', booking=next_booking, guest=self.guest, expected_arrival=next_booking.check_in, expected_departure=next_booking.check_out)
        StayRoom.objects.create(stay=stay,room=self.room,booking_room=a,actual_check_in_at=occ.actual_check_out_at+timedelta(seconds=1))
        self.assertEqual(self.start(phase='POST').status_code,400)

    def test_duplicates_unknown_items_and_pre_discrepancy(self):
        i = self.start().data['data']['id']
        duplicate = self.lines(); duplicate[1]['item_id'] = self.remote.pk
        self.assertEqual(self.save(i, duplicate).status_code, 400)
        unknown = self.lines(); unknown[1]['item_id'] = 99999
        self.assertEqual(self.save(i, unknown).status_code, 400)
        self.assertEqual(self.save(i, self.lines(remote=0)).status_code, 200)
        self.checkout()
        post = self.start(phase='POST').data['data']['id']
        done = self.save(post, self.lines(remote=1, hangers=5, replaced=True))
        self.assertEqual(done.status_code, 200, done.content)
        remote = next(x for x in done.data['data']['lines'] if x['item_name'] == 'TV remote')
        self.assertEqual(remote['before'], 0); self.assertEqual(remote['actual'], 1); self.assertEqual(remote['missing'], 0)
        self.assertTrue(remote['replaced']); self.assertTrue(remote['discrepancy'])
        self.auth(self.manager)
        report = self.client.get(BASE+'discrepancies/').data['data']
        pre_line = next(x for x in report if x['phase'] == 'PRE')
        self.assertEqual(pre_line['stay'], 'STY-'+self.booking.booking_reference)
        self.auth(self.worker)
        self.assertEqual(self.client.post(BASE+'discrepancies/%d/reviews/' % pre_line['id'], {'status':'RESOLVED', 'explanation':'not authorized'}, format='json').status_code, 403)

    def test_booking_room_change_prevents_draft_completion(self):
        i = self.start().data['data']['id']
        self.assignment.room = self.room2; self.assignment.save(update_fields=['room'])
        self.assertEqual(self.save(i, self.lines()).status_code, 400)
        self.assertIsNone(AssetInspection.objects.get(pk=i).completed_at)

    def test_multiple_occupants_and_room_status_unchanged(self):
        for n in range(2):
            RoomOccupant.objects.create(booking_room=self.assignment, full_name='Occupant %s' % n, email='occupant%s@test.dev' % n)
        old_status = self.room.housekeeping_status
        i = self.start().data['data']['id']
        self.assertEqual(self.save(i, self.lines()).status_code, 200)
        self.room.refresh_from_db(); self.assertEqual(self.room.housekeeping_status, old_status)
        self.assertEqual(AssetInspection.objects.get(pk=i).booking_id, self.booking.id)
        self.assertEqual(self.assignment.occupants.count(), 2)

    def test_revoked_capability_and_anonymous_denied(self):
        cap = Capability.objects.get(code='housekeeping.task.manage')
        UserCapabilityOverride.objects.create(user=self.worker, capability=cap, effect='REVOKE')
        self.worker.refresh_from_db(); self.auth(self.worker)
        self.assertEqual(self.client.get(BASE+'queue/').status_code, 403)
        self.unauth()
        self.assertIn(self.client.get(BASE+'queue/').status_code, (401,403))
