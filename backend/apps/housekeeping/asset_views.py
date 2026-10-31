"""Reusable-room-asset workflow, intentionally independent of financial systems."""
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import ValidationError, PermissionDenied
from rest_framework.permissions import BasePermission
from rest_framework.views import APIView

from apps.bookings.models import Booking, BookingRoom
from apps.core.pagination import StandardPagination
from apps.core.permissions import HasCapability
from apps.core.responses import success_response
from apps.rooms.models import Room, RoomType
from apps.stays.models import Stay, StayRoom
from .models import ReusableItem, TypeAsset, RoomAsset, AssetInspection, AssetInspectionLine, AssetReviewEvent

MANAGERS = {'ADMIN', 'MANAGER', 'GENERAL_MANAGER', 'HOUSEKEEPING_MANAGER'}
WORKERS = MANAGERS | {'HOUSEKEEPING', 'HOUSEKEEPER'}


class AssetRole(BasePermission):
    def has_permission(self, request, view):
        roles = MANAGERS if getattr(view, 'manager_only', False) else WORKERS
        return request.user.role in roles and HasCapability().has_permission(request, view) if request.user.is_authenticated else False


class Base(APIView):
    permission_classes = [AssetRole]
    required_capability = 'housekeeping.task.manage'


class Manager(Base):
    manager_only = True
    required_capability = 'housekeeping.task.assign'


class CatalogInput(serializers.Serializer):
    name = serializers.CharField(max_length=120, trim_whitespace=True)
    category = serializers.ChoiceField(choices=['REUSABLE', 'CONSUMABLE'])


class ConfigurationInput(serializers.Serializer):
    scope = serializers.ChoiceField(choices=['room', 'type'])
    scope_id = serializers.IntegerField(min_value=1)
    item_id = serializers.IntegerField(min_value=1)
    quantity = serializers.IntegerField(min_value=0, max_value=10000)


class CatalogView(Base):
    def get(self, request):
        return success_response(list(ReusableItem.objects.order_by('name').values('id', 'name', 'category')))

    def post(self, request):
        if request.user.role not in MANAGERS or not HasCapability().has_permission(request, type('V', (), {'required_capability': 'housekeeping.task.assign'})()):
            raise PermissionDenied()
        s = CatalogInput(data=request.data); s.is_valid(raise_exception=True)
        name = ' '.join(s.validated_data['name'].split())
        item, created = ReusableItem.objects.get_or_create(normalized_name=name.casefold(), defaults={'name': name, 'category': s.validated_data['category']})
        if item.category != s.validated_data['category']:
            raise ValidationError('This item name already exists in a different category; ask an administrator to review it.')
        return success_response({'id': item.id, 'name': item.name, 'category': item.category, 'created': created}, status=201 if created else 200)


class ConfigurationView(Manager):
    def get(self, request):
        types = list(TypeAsset.objects.select_related('item', 'room_type').values('id', 'room_type_id', 'room_type__name', 'item_id', 'item__name', 'quantity'))
        rooms = list(RoomAsset.objects.select_related('item', 'room').values('id', 'room_id', 'room__room_number', 'item_id', 'item__name', 'quantity'))
        return success_response({'types': types, 'rooms': rooms, 'room_options': list(Room.objects.order_by('room_number').values('id', 'room_number')), 'type_options': list(RoomType.objects.order_by('name').values('id', 'name'))})

    def post(self, request):
        s = ConfigurationInput(data=request.data); s.is_valid(raise_exception=True); d = s.validated_data
        item = get_object_or_404(ReusableItem, pk=d['item_id'])
        if item.category != 'REUSABLE': raise ValidationError('Consumables cannot be configured as reusable room assets.')
        if d['scope'] == 'room':
            obj = get_object_or_404(Room, pk=d['scope_id']); model = RoomAsset; key = {'room': obj, 'item': item}
        else:
            obj = get_object_or_404(RoomType, pk=d['scope_id']); model = TypeAsset; key = {'room_type': obj, 'item': item}
        config, _ = model.objects.update_or_create(**key, defaults={'quantity': d['quantity']})
        return success_response({'id': config.id, 'quantity': config.quantity})


def effective_checklist(room):
    items = {a.item_id: {'item_id': a.item_id, 'name': a.item.name, 'expected': a.quantity}
             for a in TypeAsset.objects.filter(room_type=room.room_type, item__category='REUSABLE').select_related('item')}
    for a in RoomAsset.objects.filter(room=room, item__category='REUSABLE').select_related('item'):
        items[a.item_id] = {'item_id': a.item_id, 'name': a.item.name, 'expected': a.quantity}
    return sorted((x for x in items.values() if x['expected'] > 0), key=lambda x: x['name'].casefold())


class QueueView(Base):
    def get(self, request):
        pre = AssetInspection.objects.filter(assignment_id=OuterRef('pk'), phase='PRE', completed_at__isnull=False)
        post = AssetInspection.objects.filter(assignment_id=OuterRef('pk'), phase='POST', completed_at__isnull=False)
        checkout = StayRoom.objects.filter(booking_room_id=OuterRef('pk'), room_id=OuterRef('room_id'), stay__booking_id=OuterRef('booking_id'), actual_check_out_at__isnull=False)
        qs = BookingRoom.objects.select_related('booking', 'room', 'room__room_type').annotate(pre_done=Exists(pre), post_done=Exists(post), checked_out=Exists(checkout)).filter(
            Q(checked_in_at__isnull=True, not_arrived_at__isnull=True, booking__status__in=[Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN], pre_done=False) |
            Q(post_done=False, checked_out=True)).order_by('-check_in', '-pk')
        paginator = StandardPagination(); page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response([{'assignment_id': a.id, 'room': a.room.room_number, 'room_type': a.room.room_type.name,
            'booking': a.booking.booking_reference, 'phase': 'POST' if a.checked_out else 'PRE', 'pre_completed': a.pre_done, 'check_in': a.check_in, 'check_out': a.check_out} for a in page])


class StartInput(serializers.Serializer):
    assignment_id = serializers.IntegerField(min_value=1)
    phase = serializers.ChoiceField(choices=['PRE', 'POST'])


def valid_phase(assignment, phase):
    if phase == 'PRE':
        if StayRoom.objects.filter(booking_room=assignment, actual_check_in_at__isnull=False).exists() or assignment.checked_in_at or assignment.not_arrived_at or assignment.booking.status not in (Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN):
            raise ValidationError('Pre-check-in verification requires an assigned room before its check-in.')
    else:
        occupancies = list(StayRoom.objects.filter(booking_room=assignment, room=assignment.room, stay__booking=assignment.booking, actual_check_out_at__isnull=False)[:2])
        if len(occupancies) != 1:
            raise ValidationError('Exactly one matching checked-out stay-room occupancy is required; refer room moves to management.')
        occupancy = occupancies[0]
        if StayRoom.objects.filter(room=assignment.room, actual_check_in_at__gt=occupancy.actual_check_out_at).exclude(pk=occupancy.pk).exists():
            raise ValidationError('A later guest has occupied this room; a reliable post-checkout comparison is no longer possible.')
        return occupancy


def inspection_data(i):
    stay_ref = i.stay_room.stay.reference if i.stay_room_id else Stay.objects.filter(booking_id=i.booking_id).values_list('reference', flat=True).first()
    lines = list(i.lines.order_by('id').values('id', 'item_id_snapshot', 'item_name', 'expected', 'before', 'actual', 'condition', 'replaced', 'remarks', 'evidence', 'missing', 'discrepancy'))
    for line in lines:
        line['verification_status'] = 'MISSING' if line['actual'] == 0 else ('PARTIAL' if line['actual'] < line['expected'] else 'PRESENT')
    return {'id': i.id, 'assignment_id': i.assignment_id, 'room': i.room_number_snapshot, 'room_type': i.room_type_snapshot,
            'booking': i.booking_reference_snapshot, 'stay': stay_ref, 'phase': i.phase, 'checklist': i.checklist, 'draft': i.draft,
            'revision': i.revision, 'completed_at': i.completed_at, 'completed_by': i.completed_by.email if i.completed_by else None, 'lines': lines}


class InspectionStartView(Base):
    def post(self, request):
        s = StartInput(data=request.data); s.is_valid(raise_exception=True); d = s.validated_data
        with transaction.atomic():
            a = get_object_or_404(BookingRoom.objects.select_for_update().select_related('room__room_type', 'booking'), pk=d['assignment_id'])
            i = AssetInspection.objects.filter(assignment=a, phase=d['phase']).select_related('room__room_type', 'booking', 'completed_by', 'stay_room__stay').first()
            if i:
                return success_response(inspection_data(i), message='Existing inspection returned.')
            occupancy = valid_phase(a, d['phase'])
            if d['phase'] == 'PRE':
                checklist = effective_checklist(a.room)
            else:
                pre = AssetInspection.objects.filter(assignment=a, phase='PRE', completed_at__isnull=False, room=a.room, booking=a.booking).select_related('completed_by').first()
                if not pre: raise ValidationError('A completed pre-check-in inspection for this room and booking is required.')
                if not a.checked_in_at or pre.completed_at > a.checked_in_at:
                    raise ValidationError('The pre-check-in inspection must precede this room’s recorded check-in.')
                checklist = [{'item_id': line.item_id_snapshot, 'name': line.item_name, 'expected': line.expected, 'before': line.actual,
                              'before_condition': line.condition, 'before_remarks': line.remarks, 'before_evidence': line.evidence, 'before_inspector': pre.completed_by.email, 'before_inspected_at': pre.completed_at.isoformat()} for line in pre.lines.order_by('id')]
            if not checklist: raise ValidationError('No reusable items are configured for this room; ask a manager to configure them.')
            i = AssetInspection.objects.create(assignment=a, room=a.room, booking=a.booking, room_number_snapshot=a.room.room_number, room_type_snapshot=a.room.room_type.name, booking_reference_snapshot=a.booking.booking_reference, phase=d['phase'], stay_room=occupancy, checklist=checklist, created_by=request.user)
            return success_response(inspection_data(i), status=201)


class LineInput(serializers.Serializer):
    item_id = serializers.IntegerField(min_value=1)
    actual = serializers.IntegerField(min_value=0, max_value=10000)
    condition = serializers.ChoiceField(choices=['GOOD', 'DAMAGED'])
    replaced = serializers.BooleanField(default=False)
    remarks = serializers.CharField(max_length=5000, allow_blank=True, required=False, default='')
    evidence = serializers.CharField(max_length=1000, allow_blank=True, required=False, default='')


class SaveInput(serializers.Serializer):
    revision = serializers.IntegerField(min_value=0)
    complete = serializers.BooleanField(default=False)
    lines = LineInput(many=True, allow_empty=True)


class InspectionView(Base):
    def get(self, request, pk):
        i = get_object_or_404(AssetInspection.objects.select_related('room__room_type', 'booking', 'completed_by', 'stay_room__stay').prefetch_related('lines'), pk=pk)
        return success_response(inspection_data(i))

    def post(self, request, pk):
        s = SaveInput(data=request.data); s.is_valid(raise_exception=True); d = s.validated_data
        with transaction.atomic():
            # Always acquire assignment before inspection (same order as start).
            assignment_id = get_object_or_404(AssetInspection.objects.only('assignment_id'), pk=pk).assignment_id
            a = BookingRoom.objects.select_for_update().select_related('booking', 'room').get(pk=assignment_id)
            i = get_object_or_404(AssetInspection.objects.select_for_update().select_related('room__room_type', 'booking', 'completed_by'), pk=pk)
            if i.completed_at: raise ValidationError('This inspection is already complete and cannot be changed.')
            if i.revision != d['revision']: raise ValidationError('This draft changed elsewhere. Reload before saving.')
            if a.room_id != i.room_id or a.booking_id != i.booking_id: raise ValidationError('The booking-room assignment changed; this inspection cannot be completed.')
            occupancy = valid_phase(a, i.phase)
            if i.phase == 'POST' and (not occupancy or occupancy.pk != i.stay_room_id):
                raise ValidationError('The matching stay-room occupancy has changed.')
            submitted = d['lines']; ids = [x['item_id'] for x in submitted]; allowed = {x['item_id']: x for x in i.checklist}
            if len(ids) != len(set(ids)) or not set(ids).issubset(allowed): raise ValidationError('Duplicate or unknown checklist item.')
            if d['complete'] and set(ids) != set(allowed): raise ValidationError('Check every item before completing verification.')
            i.draft = {str(x['item_id']): x for x in submitted}; i.revision += 1
            if d['complete']:
                for x in submitted:
                    ref = allowed[x['item_id']]; before = ref.get('before'); baseline = before if before is not None else ref['expected']
                    missing = max(0, baseline - x['actual'])
                    AssetInspectionLine.objects.create(inspection=i, item_id_snapshot=x['item_id'], item_name=ref['name'], expected=ref['expected'], before=before,
                        actual=x['actual'], condition=x['condition'], replaced=x['replaced'], remarks=x['remarks'], evidence=x['evidence'], missing=missing,
                        discrepancy=bool(missing or x['actual'] != ref['expected'] or x['condition'] == 'DAMAGED' or x['replaced']))
                i.completed_at = timezone.now(); i.completed_by = request.user
            i.save()
            return success_response(inspection_data(i), message='Verification completed.' if i.completed_at else 'Progress saved.')


class ReportView(Manager):
    def get(self, request):
        qs = AssetInspectionLine.objects.filter(discrepancy=True, inspection__completed_at__isnull=False).select_related(
            'inspection__room__room_type', 'inspection__booking', 'inspection__completed_by', 'inspection__stay_room__stay').prefetch_related('reviews__actor').order_by('-inspection__completed_at', '-pk')
        paginator = StandardPagination(); page = paginator.paginate_queryset(qs, request, view=self)
        result = []
        stay_refs = dict(Stay.objects.filter(booking_id__in={line.inspection.booking_id for line in page}).values_list('booking_id', 'reference'))
        for line in page:
            i = line.inspection; events = list(line.reviews.all())
            result.append({'id': line.id, 'room': i.room_number_snapshot, 'room_type': i.room_type_snapshot, 'booking': i.booking_reference_snapshot,
                'stay': stay_refs.get(i.booking_id), 'assignment_id': i.assignment_id, 'phase': i.phase, 'item': line.item_name, 'expected': line.expected, 'before': line.before,
                'actual': line.actual, 'missing': line.missing, 'condition': line.condition, 'replaced': line.replaced, 'remarks': line.remarks,
                'evidence': line.evidence, 'housekeeper': i.completed_by.email, 'inspected_at': i.completed_at,
                'status': events[-1].status if events else 'OPEN', 'resolution': events[-1].explanation if events else '',
                'history': [{'status': e.status, 'explanation': e.explanation, 'actor': e.actor.email, 'created_at': e.created_at} for e in events]})
        return paginator.get_paginated_response(result)


class ReviewInput(serializers.Serializer):
    status = serializers.ChoiceField(choices=['INVESTIGATING', 'RESOLVED'])
    explanation = serializers.CharField(max_length=5000, trim_whitespace=True)


class ReviewView(Manager):
    def post(self, request, pk):
        s = ReviewInput(data=request.data); s.is_valid(raise_exception=True)
        with transaction.atomic():
            line = get_object_or_404(AssetInspectionLine.objects.select_for_update(), pk=pk, discrepancy=True, inspection__completed_at__isnull=False)
            event = AssetReviewEvent.objects.create(line=line, actor=request.user, **s.validated_data)
        return success_response({'id': event.id, 'status': event.status, 'created_at': event.created_at}, status=201)
