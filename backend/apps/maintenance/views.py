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

from .models import MaintenanceEvent, MaintenanceWorkOrder, RoomOutage
from .serializers import (
    ClearOutageSerializer, MaintenanceAssignSerializer, MaintenanceCommentSerializer, MaintenanceCreateSerializer,
    MaintenanceStatusSerializer, MaintenanceWorkOrderDetailSerializer, MaintenanceWorkOrderListSerializer,
    StartOutageSerializer,
)
from .services.work_order_service import (
    add_maintenance_comment, assign_work_order, claim_work_order, clear_room_outage, create_work_order,
    maintenance_queryset_for_user, scoped_work_order_key, start_room_outage, transition_work_order,
)


def _page(view, request, queryset, serializer):
    paginator = StandardPagination(); page = paginator.paginate_queryset(queryset, request, view=view)
    return paginator.get_paginated_response(serializer(page, many=True).data)


def _queryset(user, *, detail=False):
    queryset = maintenance_queryset_for_user(user).select_related("room", "service_request", "assigned_to", "room_outage", "room_outage__cleared_by")
    if detail:
        queryset = queryset.prefetch_related(Prefetch("events", queryset=MaintenanceEvent.objects.select_related("actor").order_by("created_at", "pk")))
    return queryset


def _work_order_or_404(user, reference, *, detail=False):
    work_order = _queryset(user, detail=detail).filter(reference=reference).first()
    if work_order is None: raise NotFound("Maintenance work order not found.")
    return work_order


@extend_schema(tags=["Admin · Maintenance"], summary="List the caller's permitted maintenance queue")
class MaintenanceWorkOrderListCreateView(APIView):
    permission_classes = [HasCapability]; required_capability = "maintenance.work_order.manage"
    def get(self, request):
        queryset = _queryset(request.user)
        for field in ("status", "priority", "category"):
            if value := request.query_params.get(field): queryset = queryset.filter(**{field: value})
        if room := request.query_params.get("room"): queryset = queryset.filter(room__room_number=room)
        if search := request.query_params.get("search"):
            queryset = queryset.filter(Q(reference__icontains=search) | Q(summary__icontains=search) | Q(room__room_number__icontains=search))
        return _page(self, request, queryset.order_by("due_at", "-created_at", "-pk"), MaintenanceWorkOrderListSerializer)

    def post(self, request):
        serializer = MaintenanceCreateSerializer(data=request.data); serializer.is_valid(raise_exception=True); data=serializer.validated_data
        room = None
        if room_id := data.get("room_id"):
            room = Room.objects.filter(pk=room_id).first()
            if room is None: raise NotFound("Room not found.")
        service_request = None
        if ref := data.get("service_request_reference"):
            service_request = ServiceRequest.objects.filter(reference=ref).first()
            if service_request is None: raise NotFound("Service request not found.")
        assignee = None
        if data.get("assigned_to_id") is not None:
            assignee = User.objects.filter(pk=data["assigned_to_id"]).first()
            if assignee is None: raise NotFound("Assignee not found.")
        work_order, created = create_work_order(
            room=room, service_request=service_request, category=data["category"], priority=data["priority"], summary=data["summary"],
            description=data.get("description", ""), due_at=data.get("due_at"), assigned_to=assignee, actor=request.user,
            source_key=scoped_work_order_key(scope=f"staff:{request.user.pk}", raw_key=data.get("idempotency_key")),
        )
        if created: log_action(actor=request.user, action="MAINTENANCE_WORK_ORDER_CREATED", instance=work_order, request=request, metadata={"reference": work_order.reference})
        return success_response(MaintenanceWorkOrderDetailSerializer(_work_order_or_404(request.user, work_order.reference, detail=True)).data,
                                message="Maintenance work order created." if created else "Existing maintenance work order returned.",
                                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


@extend_schema(tags=["Admin · Maintenance"], summary="Read a maintenance work order and immutable timeline")
class MaintenanceWorkOrderDetailView(APIView):
    permission_classes = [HasCapability]; required_capability = "maintenance.work_order.manage"
    def get(self, request, reference): return success_response(MaintenanceWorkOrderDetailSerializer(_work_order_or_404(request.user, reference, detail=True)).data)


@extend_schema(tags=["Admin · Maintenance"], summary="Dispatch a maintenance work order")
class MaintenanceWorkOrderAssignView(APIView):
    permission_classes = [HasCapability]; required_capability = "maintenance.work_order.assign"
    def post(self, request, reference):
        serializer = MaintenanceAssignSerializer(data=request.data); serializer.is_valid(raise_exception=True); data=serializer.validated_data
        current = _work_order_or_404(request.user, reference); assignee=current.assigned_to
        if "assigned_to_id" in data:
            assignee = None if data["assigned_to_id"] is None else User.objects.filter(pk=data["assigned_to_id"]).first()
            if data["assigned_to_id"] is not None and assignee is None: raise NotFound("Assignee not found.")
        work_order = assign_work_order(work_order=current, actor=request.user, assignee=assignee, due_at=data.get("due_at"), note=data.get("note", ""))
        log_action(actor=request.user, action="MAINTENANCE_WORK_ORDER_DISPATCHED", instance=work_order, request=request, metadata={"reference": work_order.reference})
        return success_response(MaintenanceWorkOrderDetailSerializer(_work_order_or_404(request.user, work_order.reference, detail=True)).data, message="Maintenance dispatch recorded.")


@extend_schema(tags=["Admin · Maintenance"], summary="Claim an unassigned maintenance work order")
class MaintenanceWorkOrderClaimView(APIView):
    permission_classes = [HasCapability]; required_capability = "maintenance.work_order.manage"
    def post(self, request, reference):
        work_order = claim_work_order(work_order=_work_order_or_404(request.user, reference), actor=request.user)
        return success_response(MaintenanceWorkOrderDetailSerializer(_work_order_or_404(request.user, work_order.reference, detail=True)).data, message="Maintenance work order claimed.")


@extend_schema(tags=["Admin · Maintenance"], summary="Progress the controlled maintenance state machine")
class MaintenanceWorkOrderStatusView(APIView):
    permission_classes = [HasCapability]; required_capability = "maintenance.work_order.manage"
    def post(self, request, reference):
        serializer = MaintenanceStatusSerializer(data=request.data); serializer.is_valid(raise_exception=True)
        work_order = transition_work_order(work_order=_work_order_or_404(request.user, reference), target_status=serializer.validated_data["status"], actor=request.user, note=serializer.validated_data.get("note", ""))
        log_action(actor=request.user, action="MAINTENANCE_WORK_ORDER_STATUS_CHANGED", instance=work_order, request=request, metadata={"reference": work_order.reference, "status": work_order.status})
        return success_response(MaintenanceWorkOrderDetailSerializer(_work_order_or_404(request.user, work_order.reference, detail=True)).data, message="Maintenance status updated.")


@extend_schema(tags=["Admin · Maintenance"], summary="Record immutable maintenance work-order comment")
class MaintenanceWorkOrderCommentView(APIView):
    permission_classes = [HasCapability]; required_capability = "maintenance.work_order.manage"
    def post(self, request, reference):
        serializer=MaintenanceCommentSerializer(data=request.data); serializer.is_valid(raise_exception=True)
        event=add_maintenance_comment(work_order=_work_order_or_404(request.user, reference), actor=request.user, message=serializer.validated_data["message"])
        return success_response(MaintenanceWorkOrderDetailSerializer(_work_order_or_404(request.user, event.work_order.reference, detail=True)).data, message="Maintenance comment recorded.")


@extend_schema(tags=["Admin · Maintenance"], summary="Place a room under an explicit maintenance outage")
class MaintenanceOutageStartView(APIView):
    permission_classes = [HasCapability]; required_capability = "maintenance.work_order.assign"
    def post(self, request, reference):
        serializer=StartOutageSerializer(data=request.data); serializer.is_valid(raise_exception=True)
        work_order=_work_order_or_404(request.user, reference)
        outage=start_room_outage(work_order=work_order, actor=request.user, outage_status=serializer.validated_data["outage_status"], reason=serializer.validated_data.get("reason", ""))
        return success_response(MaintenanceWorkOrderDetailSerializer(_work_order_or_404(request.user, outage.work_order.reference, detail=True)).data, message="Room outage started.")


@extend_schema(tags=["Admin · Maintenance"], summary="Clear an explicit maintenance outage")
class MaintenanceOutageClearView(APIView):
    permission_classes = [HasCapability]; required_capability = "maintenance.work_order.assign"
    def post(self, request, reference):
        serializer=ClearOutageSerializer(data=request.data); serializer.is_valid(raise_exception=True)
        work_order=_work_order_or_404(request.user, reference, detail=True)
        try: outage=work_order.room_outage
        except RoomOutage.DoesNotExist: raise NotFound("Active or historical room outage not found for this work order.")
        outage=clear_room_outage(outage=outage, actor=request.user, note=serializer.validated_data.get("note", ""))
        return success_response(MaintenanceWorkOrderDetailSerializer(_work_order_or_404(request.user, outage.work_order.reference, detail=True)).data, message="Room outage cleared.")
