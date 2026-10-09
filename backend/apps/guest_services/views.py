"""Capability-scoped staff and verified-email portal service-request APIs."""
from django.db import transaction
from django.db.models import Prefetch, Q
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts.capabilities import has_capability
from apps.accounts.models import User
from apps.audit.services import log_action
from apps.bookings.models import Guest
from apps.core.pagination import StandardPagination
from apps.core.permissions import HasCapability
from apps.core.responses import success_response
from apps.portal.authentication import PortalSessionAuthentication
from apps.portal.permissions import HasPortalSession
from apps.rooms.models import Room
from apps.stays.models import Stay, StayRoom

from .models import ServiceQRLink, ServiceRequest, ServiceRequestEvent
from .qr_serializers import ServiceQRLinkCreateSerializer, ServiceQRLinkSerializer, ServiceQRRequestCreateSerializer
from .serializers import (
    PortalServiceRequestCancelSerializer,
    PortalServiceRequestCommentSerializer,
    PortalServiceRequestCreateSerializer,
    PortalServiceRequestDetailSerializer,
    PortalServiceRequestListSerializer,
    ServiceRequestAssignSerializer,
    ServiceRequestCommentSerializer,
    ServiceRequestDetailSerializer,
    ServiceRequestListSerializer,
    ServiceRequestStatusSerializer,
    StaffServiceRequestCreateSerializer,
)
from .services.request_service import (
    add_service_request_comment,
    assign_service_request,
    cancel_service_request_by_guest,
    claim_service_request,
    create_service_request,
    scoped_idempotency_key,
    service_request_queryset_for_staff,
    transition_service_request,
)
from .services.qr_service import (
    create_service_qr_link,
    issued_qr_payload,
    mark_service_qr_used,
    revoke_service_qr_link,
    rotate_service_qr_link,
    service_qr_link_for_token,
)


def _page(view, request, queryset, serializer):
    paginator = StandardPagination()
    page = paginator.paginate_queryset(queryset, request, view=view)
    return paginator.get_paginated_response(serializer(page, many=True).data)


def _staff_base_queryset(actor, *, detail=False):
    queryset = service_request_queryset_for_staff(actor).select_related(
        "guest", "stay", "room", "qr_link", "assigned_to", "created_by", "pos_order"
    )
    if detail:
        queryset = queryset.prefetch_related(
            Prefetch("events", queryset=ServiceRequestEvent.objects.select_related("actor").order_by("created_at", "pk"))
        )
    return queryset


def _staff_request_or_404(*, actor, reference, detail=False):
    service_request = _staff_base_queryset(actor, detail=detail).filter(reference=reference).first()
    if service_request is None:
        raise NotFound("Service request not found.")
    return service_request


def _portal_base_queryset(email, *, detail=False):
    queryset = ServiceRequest.objects.filter(guest__email__iexact=email).select_related("stay", "room")
    if detail:
        queryset = queryset.prefetch_related(
            Prefetch(
                "events",
                queryset=ServiceRequestEvent.objects.filter(guest_visible=True).order_by("created_at", "pk"),
                to_attr="guest_visible_events",
            )
        )
    return queryset


def _portal_request_or_404(*, email, reference, detail=False):
    service_request = _portal_base_queryset(email, detail=detail).filter(reference=reference).first()
    if service_request is None:
        raise NotFound("Service request not found.")
    return service_request


@extend_schema(tags=["Admin · Guest services"], summary="List the caller's permitted service-request queue")
class ServiceRequestListCreateView(APIView):
    permission_classes = [HasCapability]
    required_capability = "guest_request.manage"

    def get(self, request):
        queryset = _staff_base_queryset(request.user)
        if status_value := request.query_params.get("status"):
            queryset = queryset.filter(status=status_value)
        if priority := request.query_params.get("priority"):
            queryset = queryset.filter(priority=priority)
        if owner_team := request.query_params.get("owner_team"):
            queryset = queryset.filter(owner_team=owner_team)
        if stay_reference := request.query_params.get("stay"):
            queryset = queryset.filter(stay__reference=stay_reference)
        if room_number := request.query_params.get("room"):
            queryset = queryset.filter(room__room_number=room_number)
        if request.query_params.get("overdue") == "true":
            from django.utils import timezone
            queryset = queryset.filter(due_at__lt=timezone.now()).exclude(
                status__in=[ServiceRequest.Status.CLOSED, ServiceRequest.Status.CANCELLED]
            )
        if search := request.query_params.get("search"):
            queryset = queryset.filter(
                Q(reference__icontains=search)
                | Q(summary__icontains=search)
                | Q(guest__first_name__icontains=search)
                | Q(guest__last_name__icontains=search)
                | Q(room__room_number__icontains=search)
                | Q(table_number__icontains=search)
            )
        return _page(self, request, queryset.order_by("due_at", "-created_at", "-pk"), ServiceRequestListSerializer)

    def post(self, request):
        serializer = StaffServiceRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        guest = Guest.objects.filter(pk=data["guest_id"]).first()
        if guest is None:
            raise NotFound("Guest not found.")
        stay = None
        if reference := data.get("stay_reference"):
            stay = Stay.objects.select_related("guest").filter(reference=reference).first()
            if stay is None:
                raise NotFound("Stay not found.")
        room = None
        if room_id := data.get("room_id"):
            room = Room.objects.filter(pk=room_id).first()
            if room is None:
                raise NotFound("Room not found.")
        assigned_to = None
        if "assigned_to_id" in data and data["assigned_to_id"] is not None:
            assigned_to = User.objects.filter(pk=data["assigned_to_id"]).first()
            if assigned_to is None:
                raise NotFound("Assignee not found.")
        key = scoped_idempotency_key(scope=f"staff:{request.user.pk}", raw_key=data.get("idempotency_key"))
        service_request, created = create_service_request(
            guest=guest,
            stay=stay,
            room=room,
            category=data["category"],
            priority=data["priority"],
            channel=data["channel"],
            owner_team=data.get("owner_team"),
            assigned_to=assigned_to,
            due_at=data.get("due_at"),
            summary=data["summary"],
            detail=data.get("detail", ""),
            actor=request.user,
            idempotency_key=key,
        )
        if created:
            log_action(
                actor=request.user,
                action="SERVICE_REQUEST_CREATED",
                instance=service_request,
                request=request,
                metadata={"reference": service_request.reference, "channel": service_request.channel},
            )
        service_request = _staff_request_or_404(actor=request.user, reference=service_request.reference, detail=True)
        return success_response(
            ServiceRequestDetailSerializer(service_request).data,
            message="Service request created." if created else "Existing service request returned.",
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


@extend_schema(tags=["Admin · Guest services"], summary="Read one permitted service request and its immutable event timeline")
class ServiceRequestDetailView(APIView):
    permission_classes = [HasCapability]
    required_capability = "guest_request.manage"

    def get(self, request, reference):
        return success_response(ServiceRequestDetailSerializer(
            _staff_request_or_404(actor=request.user, reference=reference, detail=True)
        ).data)


@extend_schema(tags=["Admin · Guest services"], summary="Dispatch or re-route a guest service request")
class ServiceRequestAssignView(APIView):
    permission_classes = [HasCapability]
    required_capability = "guest_request.assign"

    def post(self, request, reference):
        serializer = ServiceRequestAssignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        current = _staff_request_or_404(actor=request.user, reference=reference)
        if "assigned_to_id" in data:
            assignee = None
            if data["assigned_to_id"] is not None:
                assignee = User.objects.filter(pk=data["assigned_to_id"]).first()
                if assignee is None:
                    raise NotFound("Assignee not found.")
        else:
            assignee = current.assigned_to
        updated = assign_service_request(
            service_request=current,
            actor=request.user,
            assigned_to=assignee,
            owner_team=data.get("owner_team") or current.owner_team,
            due_at=data.get("due_at", current.due_at),
            note=data.get("note", ""),
        )
        log_action(actor=request.user, action="SERVICE_REQUEST_DISPATCHED", instance=updated, request=request,
                   metadata={"reference": updated.reference, "assigned_to_id": updated.assigned_to_id})
        return success_response(
            ServiceRequestDetailSerializer(_staff_request_or_404(actor=request.user, reference=updated.reference, detail=True)).data,
            message="Service request dispatch recorded.",
        )


@extend_schema(tags=["Admin · Guest services"], summary="Claim an unassigned request from the caller's own team queue")
class ServiceRequestClaimView(APIView):
    permission_classes = [HasCapability]
    required_capability = "guest_request.manage"

    def post(self, request, reference):
        current = _staff_request_or_404(actor=request.user, reference=reference)
        updated = claim_service_request(service_request=current, actor=request.user)
        log_action(actor=request.user, action="SERVICE_REQUEST_CLAIMED", instance=updated, request=request,
                   metadata={"reference": updated.reference})
        return success_response(
            ServiceRequestDetailSerializer(_staff_request_or_404(actor=request.user, reference=updated.reference, detail=True)).data,
            message="Service request claimed.",
        )


@extend_schema(tags=["Admin · Guest services"], summary="Create the linked housekeeping task for a housekeeping request")
class ServiceRequestHousekeepingTaskView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("guest_request.manage", "housekeeping.task.manage")

    def post(self, request, reference):
        service_request = _staff_request_or_404(actor=request.user, reference=reference)
        if service_request.category != ServiceRequest.Category.HOUSEKEEPING:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"reference": "Only a housekeeping service request can create a housekeeping task."})
        if service_request.room_id is None:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"reference": "A room-linked service request is required to create a housekeeping task."})
        from apps.housekeeping.services.task_service import create_housekeeping_task
        task, created = create_housekeeping_task(
            room=service_request.room,
            stay=service_request.stay,
            service_request=service_request,
            task_type="GUEST_REQUEST",
            priority=service_request.priority,
            summary=service_request.summary,
            detail=service_request.detail,
            actor=request.user,
            source_key=f"service-request-housekeeping:{service_request.reference}",
        )
        if created:
            log_action(actor=request.user, action="SERVICE_REQUEST_ROUTED_HOUSEKEEPING", instance=service_request,
                       request=request, metadata={"reference": service_request.reference, "task_reference": task.reference})
        return success_response({"task_reference": task.reference, "created": created}, message="Housekeeping task linked to service request.")


@extend_schema(tags=["Admin · Guest services"], summary="Create the linked maintenance work order for a maintenance request")
class ServiceRequestMaintenanceWorkOrderView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("guest_request.manage", "maintenance.work_order.manage")

    def post(self, request, reference):
        service_request = _staff_request_or_404(actor=request.user, reference=reference)
        if service_request.category != ServiceRequest.Category.MAINTENANCE:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"reference": "Only a maintenance service request can create a maintenance work order."})
        from apps.maintenance.services.work_order_service import create_work_order
        work_order, created = create_work_order(
            room=service_request.room,
            service_request=service_request,
            category="GENERAL",
            priority=service_request.priority,
            summary=service_request.summary,
            description=service_request.detail,
            actor=request.user,
            source_key=f"service-request-maintenance:{service_request.reference}",
        )
        if created:
            log_action(actor=request.user, action="SERVICE_REQUEST_ROUTED_MAINTENANCE", instance=service_request,
                       request=request, metadata={"reference": service_request.reference, "work_order_reference": work_order.reference})
        return success_response({"work_order_reference": work_order.reference, "created": created}, message="Maintenance work order linked to service request.")


@extend_schema(tags=["Admin · Guest services"], summary="Progress a controlled guest-service lifecycle")
class ServiceRequestStatusView(APIView):
    permission_classes = [HasCapability]
    required_capability = "guest_request.manage"

    def post(self, request, reference):
        serializer = ServiceRequestStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        current = _staff_request_or_404(actor=request.user, reference=reference)
        updated = transition_service_request(
            service_request=current,
            target_status=data["status"],
            actor=request.user,
            note=data.get("note", ""),
            guest_visible=data["guest_visible"],
        )
        log_action(actor=request.user, action="SERVICE_REQUEST_STATUS_CHANGED", instance=updated, request=request,
                   metadata={"reference": updated.reference, "status": updated.status})
        return success_response(
            ServiceRequestDetailSerializer(_staff_request_or_404(actor=request.user, reference=updated.reference, detail=True)).data,
            message="Service request status updated.",
        )


@extend_schema(tags=["Admin · Guest services"], summary="Add an immutable staff comment to a service request")
class ServiceRequestCommentView(APIView):
    permission_classes = [HasCapability]
    required_capability = "guest_request.manage"

    def post(self, request, reference):
        serializer = ServiceRequestCommentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        current = _staff_request_or_404(actor=request.user, reference=reference)
        event = add_service_request_comment(
            service_request=current,
            message=serializer.validated_data["message"],
            actor=request.user,
            guest_visible=serializer.validated_data["guest_visible"],
        )
        return success_response(ServiceRequestDetailSerializer(
            _staff_request_or_404(actor=request.user, reference=event.request.reference, detail=True)
        ).data, message="Service request comment recorded.")


class _PortalServiceRequestBase(APIView):
    authentication_classes = [PortalSessionAuthentication]
    permission_classes = [HasPortalSession]

    @property
    def portal_email(self):
        return self.request.portal_session.email


@extend_schema(tags=["Guest Portal · Requests"], summary="List service requests owned by the verified email")
class PortalServiceRequestListCreateView(_PortalServiceRequestBase):
    def get(self, request):
        queryset = _portal_base_queryset(request.portal_session.email)
        if status_value := request.query_params.get("status"):
            queryset = queryset.filter(status=status_value)
        return _page(self, request, queryset.order_by("-created_at", "-pk"), PortalServiceRequestListSerializer)

    def post(self, request):
        serializer = PortalServiceRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        email = request.portal_session.email
        stay = (
            Stay.objects.select_related("guest")
            .filter(reference=data["stay_reference"], guest__email__iexact=email, status=Stay.Status.IN_HOUSE)
            .first()
        )
        if stay is None:
            # Do not reveal whether an arbitrary reference belongs to somebody
            # else or merely is not an active in-house stay.
            raise NotFound("An active stay matching this portal session was not found.")
        room = None
        if room_id := data.get("room_id"):
            room = Room.objects.filter(pk=room_id).first()
            if room is None:
                raise NotFound("Room not found.")
            if not StayRoom.objects.filter(stay=stay, room=room, released_at__isnull=True).exists():
                from rest_framework.exceptions import ValidationError
                raise ValidationError({"room_id": "Room must be actively assigned to the selected in-house stay."})
        else:
            active_rooms = list(
                StayRoom.objects.filter(stay=stay, released_at__isnull=True).select_related("room")[:2]
            )
            if len(active_rooms) == 1:
                room = active_rooms[0].room
        key = scoped_idempotency_key(scope=f"portal:{email.strip().lower()}", raw_key=data.get("idempotency_key"))
        service_request, created = create_service_request(
            guest=stay.guest,
            stay=stay,
            room=room,
            category=data["category"],
            priority=data["priority"],
            channel=ServiceRequest.Channel.PORTAL,
            summary=data["summary"],
            detail=data.get("detail", ""),
            portal_email=email,
            idempotency_key=key,
        )
        service_request = _portal_request_or_404(email=email, reference=service_request.reference, detail=True)
        return success_response(
            PortalServiceRequestDetailSerializer(service_request).data,
            message="Service request submitted." if created else "Existing service request returned.",
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


@extend_schema(tags=["Guest Portal · Requests"], summary="Read one owned request and guest-visible updates")
class PortalServiceRequestDetailView(_PortalServiceRequestBase):
    def get(self, request, reference):
        return success_response(PortalServiceRequestDetailSerializer(
            _portal_request_or_404(email=request.portal_session.email, reference=reference, detail=True)
        ).data)


@extend_schema(tags=["Guest Portal · Requests"], summary="Add a guest-visible comment to an owned request")
class PortalServiceRequestCommentView(_PortalServiceRequestBase):
    def post(self, request, reference):
        serializer = PortalServiceRequestCommentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        current = _portal_request_or_404(email=request.portal_session.email, reference=reference)
        event = add_service_request_comment(
            service_request=current,
            message=serializer.validated_data["message"],
            actor_label="Guest",
            guest_visible=True,
        )
        return success_response(PortalServiceRequestDetailSerializer(
            _portal_request_or_404(email=request.portal_session.email, reference=event.request.reference, detail=True)
        ).data, message="Guest comment recorded.")


@extend_schema(tags=["Guest Portal · Requests"], summary="Cancel an eligible owned request")
class PortalServiceRequestCancelView(_PortalServiceRequestBase):
    def post(self, request, reference):
        serializer = PortalServiceRequestCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        current = _portal_request_or_404(email=request.portal_session.email, reference=reference)
        updated = cancel_service_request_by_guest(
            service_request=current,
            portal_email=request.portal_session.email,
            note=serializer.validated_data.get("note", ""),
        )
        return success_response(PortalServiceRequestDetailSerializer(
            _portal_request_or_404(email=request.portal_session.email, reference=updated.reference, detail=True)
        ).data, message="Service request cancelled.")


@extend_schema(tags=["Admin · Guest services"], summary="List and issue room/table service QR links")
class ServiceQRLinkListCreateView(APIView):
    permission_classes = [HasCapability]
    required_capability = "service_qr.manage"

    def get(self, request):
        queryset = ServiceQRLink.objects.select_related("room").order_by("target_type", "label", "reference")
        if active := (request.query_params.get("active") or "").strip().lower():
            if active not in {"true", "false"}:
                raise ValidationError({"active": "Use true or false."})
            queryset = queryset.filter(is_active=(active == "true"))
        if search := (request.query_params.get("search") or "").strip():
            queryset = queryset.filter(
                Q(reference__icontains=search)
                | Q(label__icontains=search)
                | Q(room__room_number__icontains=search)
                | Q(table_number__icontains=search)
            )
        return _page(self, request, queryset, ServiceQRLinkSerializer)

    def post(self, request):
        serializer = ServiceQRLinkCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        link, token, created = create_service_qr_link(actor=request.user, **serializer.validated_data)
        log_action(
            actor=request.user,
            action="SERVICE_QR_CREATED" if created else "SERVICE_QR_REACTIVATED",
            instance=link,
            request=request,
            metadata={"reference": link.reference, "target_type": link.target_type},
        )
        payload = ServiceQRLinkSerializer(link).data
        payload.update(issued_qr_payload(link=link, token=token))
        return success_response(
            payload,
            message="QR link created and ready to print." if created else "QR link reactivated with a new token.",
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


@extend_schema(tags=["Admin · Guest services"], summary="Rotate and reprint a service QR link")
class ServiceQRLinkRotateView(APIView):
    permission_classes = [HasCapability]
    required_capability = "service_qr.manage"

    def post(self, request, reference):
        link = ServiceQRLink.objects.select_related("room").filter(reference=reference).first()
        if link is None:
            raise NotFound("Service QR link not found.")
        link, token = rotate_service_qr_link(link=link, actor=request.user)
        log_action(
            actor=request.user,
            action="SERVICE_QR_ROTATED",
            instance=link,
            request=request,
            metadata={"reference": link.reference, "target_type": link.target_type},
        )
        payload = ServiceQRLinkSerializer(link).data
        payload.update(issued_qr_payload(link=link, token=token))
        return success_response(payload, message="Previous QR token revoked; print this replacement.")


@extend_schema(tags=["Admin · Guest services"], summary="Revoke a room/table service QR link")
class ServiceQRLinkRevokeView(APIView):
    permission_classes = [HasCapability]
    required_capability = "service_qr.manage"

    def post(self, request, reference):
        link = ServiceQRLink.objects.filter(reference=reference).first()
        if link is None:
            raise NotFound("Service QR link not found.")
        link = revoke_service_qr_link(link=link, actor=request.user)
        log_action(
            actor=request.user,
            action="SERVICE_QR_REVOKED",
            instance=link,
            request=request,
            metadata={"reference": link.reference, "target_type": link.target_type},
        )
        return success_response(ServiceQRLinkSerializer(link).data, message="Service QR link revoked.")


@extend_schema(tags=["Guest QR"], summary="Read the public context for a room/table QR link")
class PublicServiceQRContextView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]

    def get_throttles(self):
        self.throttle_scope = "service_qr_context"
        return super().get_throttles()

    def get(self, request):
        link = service_qr_link_for_token(request.headers.get("X-Service-QR-Token", ""))
        if link is None:
            raise NotFound("This hotel service link is not active.")
        room_target = link.target_type == ServiceQRLink.TargetType.ROOM
        location = link.room.room_number if room_target and link.room_id else link.table_number
        categories = (
            [
                {"value": ServiceRequest.Category.FOOD_BEVERAGE, "label": "Food & beverage service"},
            ]
            if not room_target
            else [
                {"value": value, "label": label}
                for value, label in ServiceRequest.Category.choices
            ]
        )
        return success_response({
            "target_type": link.target_type,
            "label": link.label,
            "location": location,
            "categories": categories,
        })


@extend_schema(tags=["Guest QR"], summary="Submit a service request from a room/table QR link")
class PublicServiceQRRequestView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]

    def get_throttles(self):
        self.throttle_scope = "service_qr_submit"
        return super().get_throttles()

    @transaction.atomic
    def post(self, request):
        serializer = ServiceQRRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        link = service_qr_link_for_token(request.headers.get("X-Service-QR-Token", ""), for_update=True)
        if link is None:
            raise NotFound("This hotel service link is not active.")

        guest = stay = room = None
        table_number = ""
        if link.target_type == ServiceQRLink.TargetType.ROOM:
            room = Room.objects.select_for_update().filter(pk=link.room_id, is_active=True).first()
            if room is None:
                raise NotFound("This room service link is not available right now.")
            active_assignments = list(
                StayRoom.objects.select_for_update()
                .select_related("stay__guest")
                .filter(room=room, released_at__isnull=True, stay__status=Stay.Status.IN_HOUSE)
                .order_by("pk")[:2]
            )
            if len(active_assignments) != 1:
                raise ValidationError("Room service requests are available only during an active in-house stay.")
            stay = active_assignments[0].stay
            guest = stay.guest
            category = data.get("category", ServiceRequest.Category.GENERAL)
        else:
            if data.get("category") not in (None, ServiceRequest.Category.FOOD_BEVERAGE):
                raise ValidationError({"category": "Table QR requests are routed to food and beverage service."})
            category = ServiceRequest.Category.FOOD_BEVERAGE
            table_number = link.table_number

        idempotency_key = scoped_idempotency_key(
            scope=f"service-qr:{link.pk}", raw_key=data["idempotency_key"]
        )
        service_request, created = create_service_request(
            guest=guest,
            stay=stay,
            room=room,
            table_number=table_number,
            qr_link=link,
            category=category,
            priority=ServiceRequest.Priority.NORMAL,
            channel=ServiceRequest.Channel.QR,
            summary=data["summary"],
            detail=data.get("detail", ""),
            actor=None,
            idempotency_key=idempotency_key,
        )
        mark_service_qr_used(link=link)
        if created:
            log_action(
                actor=None,
                action="SERVICE_QR_REQUEST_CREATED",
                instance=service_request,
                request=request,
                metadata={
                    "reference": service_request.reference,
                    "qr_link_reference": link.reference,
                    "target_type": link.target_type,
                    "category": service_request.category,
                },
            )
        return success_response({
            "reference": service_request.reference,
            "category": service_request.category,
            "status": service_request.status,
        }, message="Your request has been sent to the hotel team.", status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
