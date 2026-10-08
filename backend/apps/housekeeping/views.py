from django.db.models import Prefetch, Q
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound
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
    add_housekeeping_comment, assign_housekeeping_task, claim_housekeeping_task,
    create_housekeeping_task, housekeeping_queryset_for_user, scoped_task_key, transition_housekeeping_task,
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


@extend_schema(tags=["Admin · Housekeeping"], summary="Claim an unassigned housekeeping task")
class HousekeepingTaskClaimView(APIView):
    permission_classes = [HasCapability]; required_capability = "housekeeping.task.manage"
    def post(self, request, reference):
        task = claim_housekeeping_task(task=_task_or_404(request.user, reference), actor=request.user)
        return success_response(HousekeepingTaskDetailSerializer(_task_or_404(request.user, task.reference, detail=True)).data, message="Housekeeping task claimed.")


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
