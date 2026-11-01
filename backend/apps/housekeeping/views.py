from django.db.models import Prefetch, Q
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import APIException, NotFound
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.audit.services import log_action
from apps.core.pagination import StandardPagination
from apps.core.permissions import HasCapability
from apps.core.responses import success_response
from apps.guest_services.models import ServiceRequest
from apps.rooms.models import Room
from apps.stays.models import Stay

from .models import HousekeepingTask, HousekeepingTaskEvent
from .serializers import (
    HousekeepingAssignSerializer, HousekeepingCommentSerializer, HousekeepingStatusSerializer,
    HousekeepingTaskCreateSerializer, HousekeepingTaskDetailSerializer, HousekeepingTaskListSerializer,
)
from .services.task_service import (
    add_housekeeping_comment, assign_housekeeping_task,
    create_housekeeping_task, housekeeping_queryset_for_user, mark_room_clean, scoped_task_key,
    transition_housekeeping_task,
)


def _page(view, request, queryset, serializer):
    paginator = StandardPagination(); page = paginator.paginate_queryset(queryset, request, view=view)
    return paginator.get_paginated_response(serializer(page, many=True).data)


def _queryset(user, *, detail=False):
    queryset = housekeeping_queryset_for_user(user).select_related("room", "stay", "service_request", "assigned_to")
    if detail:
        queryset = queryset.prefetch_related(Prefetch("events", queryset=HousekeepingTaskEvent.objects.select_related("actor").order_by("created_at", "pk")))
    return queryset


def _task_or_404(user, reference, *, detail=False):
    task = _queryset(user, detail=detail).filter(reference=reference).first()
    if task is None:
        raise NotFound("Housekeeping task not found.")
    return task


@extend_schema(tags=["Admin · Housekeeping"], summary="List the caller's permitted housekeeping task queue")
class HousekeepingTaskListCreateView(APIView):
    permission_classes = [HasCapability]
    required_capability = "housekeeping.task.manage"

    def get(self, request):
        queryset = _queryset(request.user)
        for field in ("status", "priority", "type"):
            if value := request.query_params.get(field):
                queryset = queryset.filter(**{field: value})
        if room := request.query_params.get("room"):
            queryset = queryset.filter(room__room_number=room)
        if search := request.query_params.get("search"):
            queryset = queryset.filter(Q(reference__icontains=search) | Q(summary__icontains=search) | Q(room__room_number__icontains=search))
        return _page(self, request, queryset.order_by("due_at", "-created_at", "-pk"), HousekeepingTaskListSerializer)

    def post(self, request):
        serializer = HousekeepingTaskCreateSerializer(data=request.data); serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        room = Room.objects.filter(pk=data["room_id"]).first()
        if room is None: raise NotFound("Room not found.")
        stay = None
        if ref := data.get("stay_reference"):
            stay = Stay.objects.filter(reference=ref).first()
            if stay is None: raise NotFound("Stay not found.")
        service_request = None
        if ref := data.get("service_request_reference"):
            service_request = ServiceRequest.objects.filter(reference=ref).first()
            if service_request is None: raise NotFound("Service request not found.")
        assignee = None
        if data.get("assigned_to_id") is not None:
            assignee = User.objects.filter(pk=data["assigned_to_id"]).first()
            if assignee is None: raise NotFound("Assignee not found.")
        task, created = create_housekeeping_task(
            room=room, stay=stay, service_request=service_request, task_type=data["type"], priority=data["priority"],
            summary=data["summary"], detail=data.get("detail", ""), due_at=data.get("due_at"), assigned_to=assignee,
            actor=request.user, source_key=scoped_task_key(scope=f"staff:{request.user.pk}", raw_key=data.get("idempotency_key")),
        )
        if created: log_action(actor=request.user, action="HOUSEKEEPING_TASK_CREATED", instance=task, request=request, metadata={"reference": task.reference})
        return success_response(HousekeepingTaskDetailSerializer(_task_or_404(request.user, task.reference, detail=True)).data,
                                message="Housekeeping task created." if created else "Existing housekeeping task returned.",
                                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


@extend_schema(tags=["Admin · Housekeeping"], summary="Read a housekeeping task and immutable timeline")
class HousekeepingTaskDetailView(APIView):
    permission_classes = [HasCapability]; required_capability = "housekeeping.task.manage"
    def get(self, request, reference):
        return success_response(HousekeepingTaskDetailSerializer(_task_or_404(request.user, reference, detail=True)).data)


@extend_schema(tags=["Admin · Housekeeping"], summary="Dispatch a housekeeping task")
class HousekeepingTaskAssignView(APIView):
    permission_classes = [HasCapability]; required_capability = "housekeeping.task.assign"
    def post(self, request, reference):
        serializer = HousekeepingAssignSerializer(data=request.data); serializer.is_valid(raise_exception=True); data=serializer.validated_data
        current = _task_or_404(request.user, reference)
        assignee = current.assigned_to
        if "assigned_to_id" in data:
            assignee = None if data["assigned_to_id"] is None else User.objects.filter(pk=data["assigned_to_id"]).first()
            if data["assigned_to_id"] is not None and assignee is None: raise NotFound("Assignee not found.")
        task = assign_housekeeping_task(task=current, actor=request.user, assignee=assignee, due_at=data.get("due_at"), note=data.get("note", ""))
        log_action(actor=request.user, action="HOUSEKEEPING_TASK_DISPATCHED", instance=task, request=request, metadata={"reference": task.reference})
        return success_response(HousekeepingTaskDetailSerializer(_task_or_404(request.user, task.reference, detail=True)).data, message="Housekeeping dispatch recorded.")


class HousekeepingClaimRetired(APIException):
    status_code = status.HTTP_410_GONE
    default_detail = "Self-claiming housekeeping tasks have been retired. A dispatcher assigns the task to a housekeeper."
    default_code = "HOUSEKEEPING_CLAIM_RETIRED"


@extend_schema(tags=["Admin · Housekeeping"], summary="Retired: self-claim of housekeeping tasks (use dispatch)")
class HousekeepingTaskClaimView(APIView):
    """The route is kept so old clients receive a clear 410 instead of a 404.

    The task is still looked up first, so an out-of-scope reference answers 404
    exactly as it did before, and no task state is changed.
    """
    permission_classes = [HasCapability]; required_capability = "housekeeping.task.manage"
    def post(self, request, reference):
        _task_or_404(request.user, reference)
        raise HousekeepingClaimRetired()


@extend_schema(tags=["Admin · Housekeeping"], summary="Progress the controlled housekeeping task state machine")
class HousekeepingTaskStatusView(APIView):
    permission_classes = [HasCapability]; required_capability = "housekeeping.task.manage"
    def post(self, request, reference):
        serializer = HousekeepingStatusSerializer(data=request.data); serializer.is_valid(raise_exception=True)
        task = transition_housekeeping_task(task=_task_or_404(request.user, reference), target_status=serializer.validated_data["status"], actor=request.user, note=serializer.validated_data.get("note", ""))
        log_action(actor=request.user, action="HOUSEKEEPING_TASK_STATUS_CHANGED", instance=task, request=request, metadata={"reference": task.reference, "status": task.status})
        return success_response(HousekeepingTaskDetailSerializer(_task_or_404(request.user, task.reference, detail=True)).data, message="Housekeeping task status updated.")


@extend_schema(tags=["Admin · Housekeeping"], summary="Add an immutable housekeeping task comment")
class HousekeepingTaskCommentView(APIView):
    permission_classes = [HasCapability]; required_capability = "housekeeping.task.manage"
    def post(self, request, reference):
        serializer = HousekeepingCommentSerializer(data=request.data); serializer.is_valid(raise_exception=True)
        event = add_housekeeping_comment(task=_task_or_404(request.user, reference), actor=request.user, message=serializer.validated_data["message"])
        return success_response(HousekeepingTaskDetailSerializer(_task_or_404(request.user, event.task.reference, detail=True)).data, message="Housekeeping comment recorded.")


@extend_schema(tags=["Admin · Housekeeping"], summary="List active rooms that are not ready (not clean)")
class HousekeepingRoomsNotReadyView(APIView):
    """The 'Rooms not ready' board behind the receptionist mark-clean flow.

    Critical repair D1: receptionists could not release a ready room at all —
    room edits are manager-only, so a clean room stayed 'not ready' until a
    manager logged in. This bounded projection (room, states, room type) is
    the data the housekeeping page needs; the release itself is the audited
    mark-clean action below.
    """
    permission_classes = [HasCapability]; required_capability = "housekeeping.task.assign"

    def get(self, request):
        rooms = (
            Room.objects.filter(is_active=True)
            .exclude(housekeeping_status=Room.HousekeepingStatus.CLEAN)
            .select_related("room_type")
            .order_by("room_number")
        )
        data = [
            {
                "room_number": room.room_number,
                "floor": room.floor,
                "room_type": room.room_type.name,
                "housekeeping_status": room.housekeeping_status,
                "status": room.status,
            }
            for room in rooms
        ]
        return success_response(data)


@extend_schema(tags=["Admin · Housekeeping"], summary="Mark a room clean (receptionist release)")
class RoomMarkCleanView(APIView):
    """Backend-authoritative 'mark room clean' (critical repair D1).

    Gated to ``housekeeping.task.assign`` (the dispatcher capability the
    housekeeping page already uses, held by receptionists and housekeeping
    staff). Only the clean/dirty projection moves — occupancy and maintenance
    states are preserved — repeat calls are idempotent, and every call is
    audited with who, when and the before/after state.
    """
    permission_classes = [HasCapability]; required_capability = "housekeeping.task.assign"

    def post(self, request, room_number):
        room = Room.objects.filter(room_number=room_number, is_active=True).first()
        if room is None:
            raise NotFound("Room not found.")
        note = str((request.data or {}).get("note", "") or "").strip()[:500]
        room, before, changed = mark_room_clean(room=room)
        log_action(
            actor=request.user,
            action="HOUSEKEEPING_ROOM_MARKED_CLEAN",
            instance=room,
            request=request,
            metadata={
                "room_number": room.room_number,
                "housekeeping_status_before": before,
                "housekeeping_status_after": room.housekeeping_status,
                "room_status_unchanged": room.status,
                "note": note,
            },
        )
        return success_response(
            {
                "room_number": room.room_number,
                "housekeeping_status": room.housekeeping_status,
                "status": room.status,
                "changed": changed,
            },
            message="Room marked clean." if changed else "Room was already clean.",
        )
