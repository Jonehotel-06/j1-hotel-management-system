"""Capability-protected bounded POS operational API."""
from django.db.models import Count, Prefetch, Q
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView
from django.utils.text import slugify

from apps.accounts.capabilities import has_capability
from apps.core.pagination import StandardPagination
from apps.core.permissions import HasCapability
from apps.core.responses import success_response
from apps.finance.models import CashSession, Folio
from apps.stays.models import Stay, StayRoom

from .models import KitchenTicket, MenuCategory, MenuItem, MenuModifier, PosOrder, PosOrderEvent, PosTender
from .serializers import (
    CashSessionCloseSerializer,
    CashSessionOpenSerializer,
    KitchenTicketSerializer,
    MenuCategoryManageSerializer,
    MenuCategoryMenuSerializer,
    MenuItemManageSerializer,
    MenuModifierManageSerializer,
    PosOrderCreateSerializer,
    PosOrderDetailSerializer,
    PosRoomServiceStaySerializer,
    PosOrderListSerializer,
    PosOrderStatusSerializer,
    PosOrderSubmitSerializer,
    PosTenderCaptureSerializer,
)
from .services import order_service


def _order_queryset():
    return (
        PosOrder.objects.select_related("stay", "folio", "created_by", "charge_transaction")
        .annotate(line_count=Count("lines"))
        .order_by("-created_at", "-pk")
    )


def _order_detail_queryset():
    return (
        PosOrder.objects.select_related("stay", "folio", "created_by", "charge_transaction")
        .prefetch_related(
            "lines",
            Prefetch("tenders", queryset=PosTender.objects.select_related("cash_session", "captured_by", "collection_transaction")),
            Prefetch("events", queryset=PosOrderEvent.objects.select_related("actor")),
            "kitchen_ticket",
        )
        .annotate(line_count=Count("lines"))
    )


def _get_order(reference, *, detail=False):
    order = (_order_detail_queryset() if detail else _order_queryset()).filter(reference=reference).first()
    if order is None:
        raise NotFound("POS order not found.")
    return order


def _room_service_stay_queryset():
    """Bounded picker projection; not a general staff stay directory."""
    active_rooms = StayRoom.objects.filter(released_at__isnull=True).select_related("room").order_by("room__room_number")
    return (
        Stay.objects.filter(status=Stay.Status.IN_HOUSE)
        .select_related("booking", "guest")
        .prefetch_related(Prefetch("stay_rooms", queryset=active_rooms, to_attr="_pos_active_rooms"))
        .order_by("expected_departure", "pk")
    )


@extend_schema(tags=["Admin · POS"], summary="Available POS menu for order entry")
class PosMenuView(APIView):
    permission_classes = [HasCapability]
    required_capability = "pos.order.manage"

    def get(self, request):
        categories = (
            MenuCategory.objects.filter(is_active=True)
            .prefetch_related(
                Prefetch(
                    "items",
                    queryset=MenuItem.objects.filter(is_active=True, is_available=True).prefetch_related("modifiers"),
                    to_attr="_available_items",
                )
            )
            .order_by("sort_order", "name")
        )
        return success_response(MenuCategoryMenuSerializer(categories, many=True).data)


@extend_schema(tags=["Admin · POS"], summary="Manage POS menu categories")
class PosMenuCategoryManagementView(APIView):
    permission_classes = [HasCapability]
    required_capability = "pos.menu.manage"

    def get(self, request):
        return success_response(MenuCategoryManageSerializer(MenuCategory.objects.all().order_by("sort_order", "name"), many=True).data)

    def post(self, request):
        data = request.data.copy()
        if not data.get("slug") and data.get("name"):
            data["slug"] = slugify(data["name"])
        serializer = MenuCategoryManageSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        return success_response(MenuCategoryManageSerializer(serializer.save()).data, message="Menu category created.", status=status.HTTP_201_CREATED)


class PosMenuCategoryManagementDetailView(APIView):
    permission_classes = [HasCapability]
    required_capability = "pos.menu.manage"

    def patch(self, request, pk):
        category = MenuCategory.objects.filter(pk=pk).first()
        if category is None:
            raise NotFound("Menu category not found.")
        serializer = MenuCategoryManageSerializer(category, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        return success_response(MenuCategoryManageSerializer(serializer.save()).data, message="Menu category updated.")


@extend_schema(tags=["Admin · POS"], summary="Manage POS menu items")
class PosMenuItemManagementView(APIView):
    permission_classes = [HasCapability]
    required_capability = "pos.menu.manage"
    pagination_class = StandardPagination

    def get(self, request):
        items = MenuItem.objects.select_related("category").order_by("category__sort_order", "sort_order", "name")
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(items, request, view=self)
        return paginator.get_paginated_response(MenuItemManageSerializer(page, many=True).data)

    def post(self, request):
        serializer = MenuItemManageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return success_response(MenuItemManageSerializer(serializer.save()).data, message="Menu item created.", status=status.HTTP_201_CREATED)


class PosMenuItemManagementDetailView(APIView):
    permission_classes = [HasCapability]
    required_capability = "pos.menu.manage"

    def patch(self, request, pk):
        item = MenuItem.objects.filter(pk=pk).first()
        if item is None:
            raise NotFound("Menu item not found.")
        serializer = MenuItemManageSerializer(item, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        return success_response(MenuItemManageSerializer(serializer.save()).data, message="Menu item updated.")


@extend_schema(tags=["Admin · POS"], summary="Manage POS item modifiers")
class PosMenuModifierManagementView(APIView):
    permission_classes = [HasCapability]
    required_capability = "pos.menu.manage"

    def post(self, request):
        serializer = MenuModifierManageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return success_response(MenuModifierManageSerializer(serializer.save()).data, message="Menu modifier created.", status=status.HTTP_201_CREATED)


class PosMenuModifierManagementDetailView(APIView):
    permission_classes = [HasCapability]
    required_capability = "pos.menu.manage"

    def patch(self, request, pk):
        modifier = MenuModifier.objects.filter(pk=pk).first()
        if modifier is None:
            raise NotFound("Menu modifier not found.")
        serializer = MenuModifierManageSerializer(modifier, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        return success_response(MenuModifierManageSerializer(serializer.save()).data, message="Menu modifier updated.")


@extend_schema(tags=["Admin · POS"], summary="Find in-house stays for a room-service order")
class PosRoomServiceStayListView(APIView):
    """Searchable, paginated guest picker scoped to POS order capability."""

    permission_classes = [HasCapability]
    required_capability = "pos.order.manage"
    pagination_class = StandardPagination

    def get(self, request):
        queryset = _room_service_stay_queryset()
        if search := request.query_params.get("search"):
            queryset = queryset.filter(
                Q(reference__icontains=search)
                | Q(booking__booking_reference__icontains=search)
                | Q(guest__first_name__icontains=search)
                | Q(guest__last_name__icontains=search)
                | Q(guest__email__icontains=search)
                | Q(stay_rooms__room__room_number__icontains=search)
            ).distinct()
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(PosRoomServiceStaySerializer(page, many=True).data)


@extend_schema(tags=["Admin · POS"], summary="List POS orders")
class PosOrderListCreateView(APIView):
    permission_classes = [HasCapability]
    required_capability = "pos.order.manage"
    pagination_class = StandardPagination

    def get(self, request):
        queryset = _order_queryset()
        if mode := request.query_params.get("mode"):
            queryset = queryset.filter(mode=mode)
        if order_status := request.query_params.get("status"):
            queryset = queryset.filter(status=order_status)
        if stay_reference := request.query_params.get("stay"):
            queryset = queryset.filter(stay__reference=stay_reference)
        if search := request.query_params.get("search"):
            queryset = queryset.filter(Q(reference__icontains=search) | Q(guest_name__icontains=search) | Q(table_number__icontains=search))
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request, view=self)
        serializer = PosOrderListSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def post(self, request):
        serializer = PosOrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        stay = None
        folio = None
        if data.get("stay_id"):
            stay = Stay.objects.filter(pk=data["stay_id"]).first()
            if stay is None:
                raise NotFound("Stay not found.")
        if data.get("folio_id"):
            folio = Folio.objects.filter(pk=data["folio_id"]).first()
            if folio is None:
                raise NotFound("Folio not found.")
        order, created = order_service.create_order(
            mode=data["mode"], lines=data["lines"], actor=request.user, stay=stay, folio=folio,
            guest_name=data["guest_name"], table_number=data["table_number"],
            delivery_location=data["delivery_location"], notes=data["notes"],
            idempotency_key=data["idempotency_key"],
        )
        order = _get_order(order.reference, detail=True)
        return success_response(
            PosOrderDetailSerializer(order).data,
            message="POS order created." if created else "Existing POS order returned.",
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


@extend_schema(tags=["Admin · POS"], summary="Get POS order detail")
class PosOrderDetailView(APIView):
    permission_classes = [HasCapability]
    required_capability = "pos.order.manage"

    def get(self, request, reference):
        return success_response(PosOrderDetailSerializer(_get_order(reference, detail=True)).data)


@extend_schema(tags=["Admin · POS"], summary="Submit POS order to kitchen")
class PosOrderSubmitView(APIView):
    permission_classes = [HasCapability]
    required_capability = "pos.order.manage"

    def post(self, request, reference):
        serializer = PosOrderSubmitSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        order = order_service.submit_order(
            order=_get_order(reference), actor=request.user, priority=serializer.validated_data["priority"]
        )
        return success_response(PosOrderDetailSerializer(_get_order(order.reference, detail=True)).data, message="POS order submitted to kitchen.")


@extend_schema(tags=["Admin · POS"], summary="Advance POS kitchen or delivery status")
class PosOrderStatusView(APIView):
    permission_classes = [HasCapability]
    required_capability = "pos.order.manage"

    def post(self, request, reference):
        serializer = PosOrderStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = order_service.transition_order(
            order=_get_order(reference), target_status=serializer.validated_data["status"], actor=request.user
        )
        return success_response(PosOrderDetailSerializer(_get_order(order.reference, detail=True)).data, message="POS order status updated.")


@extend_schema(tags=["Admin · POS"], summary="Capture direct POS tender")
class PosTenderCaptureView(APIView):
    permission_classes = [HasCapability]
    required_capability = "payment.capture"

    def post(self, request, reference):
        serializer = PosTenderCaptureSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        cash_session = None
        if data["cash_session_reference"]:
            cash_session = CashSession.objects.filter(reference=data["cash_session_reference"]).first()
            if cash_session is None:
                raise NotFound("Cash session not found.")
        tender = order_service.capture_direct_tender(
            order=_get_order(reference), method=data["method"], amount=data["amount"], actor=request.user,
            cash_session=cash_session, external_reference=data["external_reference"], notes=data["notes"],
        )
        return success_response(
            PosOrderDetailSerializer(_get_order(tender.order.reference, detail=True)).data,
            message="POS tender captured.",
            status=status.HTTP_201_CREATED,
        )


@extend_schema(tags=["Admin · POS"], summary="List active kitchen tickets")
class KitchenTicketListView(APIView):
    permission_classes = [HasCapability]
    required_capability = "pos.order.manage"
    pagination_class = StandardPagination

    def get(self, request):
        queryset = KitchenTicket.objects.select_related("order", "assigned_to").exclude(
            status__in=[KitchenTicket.Status.COMPLETED, KitchenTicket.Status.CANCELLED]
        ).order_by("-priority", "queued_at", "pk")
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(KitchenTicketSerializer(page, many=True).data)


@extend_schema(tags=["Admin · POS"], summary="Open my cashier session")
class CashSessionOpenView(APIView):
    permission_classes = [HasCapability]
    required_capability = "cash_session.open"

    def post(self, request):
        serializer = CashSessionOpenSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        from apps.finance.services.cash_session_service import open_cash_session

        session = open_cash_session(cashier=request.user, actor=request.user, **serializer.validated_data)
        return success_response({
            "reference": session.reference, "status": session.status, "opening_float": str(session.opening_float),
            "expected_cash": str(session.expected_cash), "business_date": session.business_date.isoformat(),
        }, message="Cash session opened.", status=status.HTTP_201_CREATED)


@extend_schema(tags=["Admin · POS"], summary="Close my cashier session")
class CashSessionCloseView(APIView):
    permission_classes = [HasCapability]
    required_capability = "cash_session.close"

    def post(self, request, reference):
        serializer = CashSessionCloseSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        session = CashSession.objects.filter(reference=reference).first()
        if session is None:
            raise NotFound("Cash session not found.")
        from apps.finance.services.cash_session_service import close_cash_session

        session = close_cash_session(cash_session=session, cashier=request.user, **serializer.validated_data)
        return success_response({
            "reference": session.reference, "status": session.status, "expected_cash": str(session.expected_cash),
            "counted_cash": str(session.counted_cash) if session.counted_cash is not None else None,
            "variance": str(session.variance) if session.variance is not None else None,
        }, message="Cash session closing count recorded.")
