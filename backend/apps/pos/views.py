"""Capability-protected bounded POS operational API."""
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView
from django.utils.text import slugify

from apps.accounts.capabilities import has_capability
from apps.audit.services import log_action
from apps.core.pagination import StandardPagination
from apps.core.permissions import HasCapability
from apps.core.responses import success_response
from apps.finance.models import CashSession, Folio
from apps.guest_services.services.request_service import service_request_queryset_for_staff
from apps.stays.models import Stay, StayRoom

from .models import (
    KitchenTicket, MenuCategory, MenuItem, MenuModifier, PosOrder, PosOrderEvent, PosTender,
    RestaurantTable, RestaurantTableSession,
)
from .serializers import (
    CashSessionCloseSerializer,
    CashSessionOpenSerializer,
    KitchenTicketSerializer,
    KitchenTicketStatusSerializer,
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
    RestaurantTableSerializer,
    RestaurantTableSessionCloseSerializer,
    RestaurantTableSessionCreateSerializer,
    RestaurantTableSessionSerializer,
)
from .services import order_service
from .services import table_service


def _order_queryset():
    return (
        PosOrder.objects.select_related("stay", "folio", "created_by", "charge_transaction", "table_session__table", "service_request")
        .annotate(line_count=Count("lines"))
        .order_by("-created_at", "-pk")
    )


def _table_session_queryset():
    return (
        RestaurantTableSession.objects.select_related("table", "opened_by", "closed_by")
        .annotate(
            order_count=Count("orders", distinct=True),
            active_order_count=Count(
                "orders", filter=Q(orders__status__in=table_service.ACTIVE_ORDER_STATUSES), distinct=True
            ),
            unpaid_order_count=Count(
                "orders",
                filter=Q(
                    orders__status=PosOrder.Status.DELIVERED,
                    orders__settlement_status__in=table_service.UNSETTLED_STATUSES,
                ),
                distinct=True,
            ),
        )
        .order_by("-opened_at", "-pk")
    )


def _order_detail_queryset():
    return (
        PosOrder.objects.select_related("stay", "folio", "created_by", "charge_transaction", "table_session__table", "service_request")
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


def _allowed_menu_areas(user):
    if has_capability(user, "pos.menu.manage"):
        return None
    areas = set()
    if has_capability(user, "restaurant.menu.manage"):
        areas.add(MenuCategory.ServiceArea.RESTAURANT)
    if has_capability(user, "bar.menu.manage"):
        areas.add(MenuCategory.ServiceArea.BAR)
    return areas


def _ensure_menu_area(user, area):
    areas = _allowed_menu_areas(user)
    if areas is not None and area not in areas:
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied("You do not have permission to manage this menu service area.")


def _allowed_order_modes(user):
    if has_capability(user, "pos.order.manage"):
        return set(PosOrder.Mode.values)
    modes = set()
    if has_capability(user, "restaurant.order.manage"):
        modes.update({PosOrder.Mode.RESTAURANT, PosOrder.Mode.TAKEAWAY})
    if has_capability(user, "bar.order.manage"):
        modes.add(PosOrder.Mode.BAR)
    return modes


def _ensure_order_mode(user, mode):
    if mode not in _allowed_order_modes(user):
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied("You do not have permission to manage this POS service area.")


def _ensure_order_access(user, order):
    if order.mode in _allowed_order_modes(user):
        return
    from rest_framework.exceptions import PermissionDenied
    raise PermissionDenied("You do not have permission to access this POS order.")


def _allowed_ticket_stations(user):
    if has_capability(user, "pos.order.manage"):
        return set(KitchenTicket.Station.values)
    stations = set()
    if has_capability(user, "restaurant.order.manage"):
        stations.add(KitchenTicket.Station.KITCHEN)
    if has_capability(user, "bar.order.manage"):
        stations.add(KitchenTicket.Station.BAR)
    if not stations and has_capability(user, "kitchen.queue.view"):
        stations.add(KitchenTicket.Station.KITCHEN)
    if not stations and has_capability(user, "kitchen.ticket.manage"):
        stations.add(KitchenTicket.Station.KITCHEN)
    return stations


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
    required_capabilities = ("pos.order.manage", "restaurant.order.manage", "bar.order.manage")
    require_any_capability = True

    def get(self, request):
        area = (request.query_params.get("area") or "").strip().upper()
        if area and area not in MenuCategory.ServiceArea.values:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"area": "Area must be RESTAURANT or BAR."})
        if area == MenuCategory.ServiceArea.BAR and not (
            has_capability(request.user, "pos.order.manage") or has_capability(request.user, "bar.order.manage")
        ):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("You do not have access to the bar menu.")
        if area == MenuCategory.ServiceArea.RESTAURANT and not (
            has_capability(request.user, "pos.order.manage") or has_capability(request.user, "restaurant.order.manage")
        ):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("You do not have access to the restaurant menu.")
        categories = MenuCategory.objects.filter(is_active=True)
        if area:
            categories = categories.filter(service_area=area)
        elif not has_capability(request.user, "pos.order.manage"):
            service_areas = set()
            if has_capability(request.user, "restaurant.order.manage"):
                service_areas.add(MenuCategory.ServiceArea.RESTAURANT)
            if has_capability(request.user, "bar.order.manage"):
                service_areas.add(MenuCategory.ServiceArea.BAR)
            categories = categories.filter(service_area__in=service_areas)
        categories = categories.prefetch_related(
            Prefetch(
                "items",
                queryset=MenuItem.objects.filter(is_active=True, is_available=True).prefetch_related("modifiers"),
                to_attr="_available_items",
            )
        ).order_by("sort_order", "name")
        return success_response(MenuCategoryMenuSerializer(categories, many=True).data)


@extend_schema(tags=["Admin · POS"], summary="Manage POS menu categories")
class PosMenuCategoryManagementView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("pos.menu.manage", "restaurant.menu.manage", "bar.menu.manage")
    require_any_capability = True

    def get(self, request):
        queryset = MenuCategory.objects.all()
        areas = _allowed_menu_areas(request.user)
        if areas is not None:
            queryset = queryset.filter(service_area__in=areas)
        queryset = queryset.order_by("service_area", "sort_order", "name")
        return success_response(MenuCategoryManageSerializer(queryset, many=True).data)

    def post(self, request):
        data = request.data.copy()
        if not data.get("slug") and data.get("name"):
            data["slug"] = slugify(data["name"])
        serializer = MenuCategoryManageSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        _ensure_menu_area(request.user, serializer.validated_data.get("service_area", MenuCategory.ServiceArea.RESTAURANT))
        return success_response(MenuCategoryManageSerializer(serializer.save()).data, message="Menu category created.", status=status.HTTP_201_CREATED)


class PosMenuCategoryManagementDetailView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("pos.menu.manage", "restaurant.menu.manage", "bar.menu.manage")
    require_any_capability = True

    def patch(self, request, pk):
        category = MenuCategory.objects.filter(pk=pk).first()
        if category is None:
            raise NotFound("Menu category not found.")
        _ensure_menu_area(request.user, category.service_area)
        serializer = MenuCategoryManageSerializer(category, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        _ensure_menu_area(request.user, serializer.validated_data.get("service_area", category.service_area))
        return success_response(MenuCategoryManageSerializer(serializer.save()).data, message="Menu category updated.")


@extend_schema(tags=["Admin · POS"], summary="Manage POS menu items")
class PosMenuItemManagementView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("pos.menu.manage", "restaurant.menu.manage", "bar.menu.manage")
    require_any_capability = True
    pagination_class = StandardPagination

    def get(self, request):
        items = MenuItem.objects.select_related("category")
        areas = _allowed_menu_areas(request.user)
        if areas is not None:
            items = items.filter(category__service_area__in=areas)
        items = items.order_by("category__service_area", "category__sort_order", "sort_order", "name")
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(items, request, view=self)
        return paginator.get_paginated_response(MenuItemManageSerializer(page, many=True).data)

    def post(self, request):
        serializer = MenuItemManageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        _ensure_menu_area(request.user, serializer.validated_data["category"].service_area)
        return success_response(MenuItemManageSerializer(serializer.save()).data, message="Menu item created.", status=status.HTTP_201_CREATED)


class PosMenuItemManagementDetailView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("pos.menu.manage", "restaurant.menu.manage", "bar.menu.manage")
    require_any_capability = True

    def patch(self, request, pk):
        item = MenuItem.objects.select_related("category").filter(pk=pk).first()
        if item is None:
            raise NotFound("Menu item not found.")
        _ensure_menu_area(request.user, item.category.service_area)
        serializer = MenuItemManageSerializer(item, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        _ensure_menu_area(request.user, serializer.validated_data.get("category", item.category).service_area)
        return success_response(MenuItemManageSerializer(serializer.save()).data, message="Menu item updated.")


@extend_schema(tags=["Admin · POS"], summary="Manage POS item modifiers")
class PosMenuModifierManagementView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("pos.menu.manage", "restaurant.menu.manage", "bar.menu.manage")
    require_any_capability = True

    def post(self, request):
        serializer = MenuModifierManageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        _ensure_menu_area(request.user, serializer.validated_data["menu_item"].category.service_area)
        return success_response(MenuModifierManageSerializer(serializer.save()).data, message="Menu modifier created.", status=status.HTTP_201_CREATED)


class PosMenuModifierManagementDetailView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("pos.menu.manage", "restaurant.menu.manage", "bar.menu.manage")
    require_any_capability = True

    def patch(self, request, pk):
        modifier = MenuModifier.objects.select_related("menu_item__category").filter(pk=pk).first()
        if modifier is None:
            raise NotFound("Menu modifier not found.")
        _ensure_menu_area(request.user, modifier.menu_item.category.service_area)
        serializer = MenuModifierManageSerializer(modifier, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        _ensure_menu_area(request.user, serializer.validated_data.get("menu_item", modifier.menu_item).category.service_area)
        return success_response(MenuModifierManageSerializer(serializer.save()).data, message="Menu modifier updated.")


@extend_schema(tags=["Admin · POS"], summary="List or register restaurant tables")
class PosRestaurantTableListCreateView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("restaurant.order.manage", "restaurant.table.manage")
    require_any_capability = True
    pagination_class = StandardPagination

    def get(self, request):
        queryset = RestaurantTable.objects.all()
        active = (request.query_params.get("active") or "true").strip().lower()
        if active in {"true", "1", "yes"}:
            queryset = queryset.filter(is_active=True)
        elif active in {"false", "0", "no"}:
            queryset = queryset.filter(is_active=False)
        elif active != "all":
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"active": "Use true, false, or all."})
        if search := (request.query_params.get("search") or "").strip():
            if len(search) > 100:
                from rest_framework.exceptions import ValidationError
                raise ValidationError({"search": "Search is limited to 100 characters."})
            queryset = queryset.filter(Q(code__icontains=search) | Q(name__icontains=search) | Q(section__icontains=search))
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(RestaurantTableSerializer(page, many=True).data)

    def post(self, request):
        from rest_framework.exceptions import PermissionDenied
        if not has_capability(request.user, "restaurant.table.manage"):
            raise PermissionDenied("Only an authorized table manager can register restaurant tables.")
        serializer = RestaurantTableSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        table = serializer.save(created_by=request.user, updated_by=request.user)
        log_action(
            actor=request.user,
            action="RESTAURANT_TABLE_REGISTERED",
            instance=table,
            request=request,
            metadata={"code": table.code, "section": table.section, "seats": table.seats},
        )
        return success_response(
            RestaurantTableSerializer(table).data, message="Restaurant table registered.", status=status.HTTP_201_CREATED
        )


class PosRestaurantTableDetailView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("restaurant.order.manage", "restaurant.table.manage")
    require_any_capability = True

    def patch(self, request, pk):
        from rest_framework.exceptions import PermissionDenied
        if not has_capability(request.user, "restaurant.table.manage"):
            raise PermissionDenied("Only an authorized table manager can change the table register.")
        with transaction.atomic():
            table = RestaurantTable.objects.select_for_update().filter(pk=pk).first()
            if table is None:
                raise NotFound("Restaurant table not found.")
            serializer = RestaurantTableSerializer(table, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            if not serializer.validated_data:
                from rest_framework.exceptions import ValidationError
                raise ValidationError({"detail": "At least one table field must be supplied."})
            changed = {
                key: value for key, value in serializer.validated_data.items()
                if getattr(table, key) != value
            }
            if changed and RestaurantTableSession.objects.filter(
                active_table_id=table.pk, status=RestaurantTableSession.Status.OPEN
            ).exists():
                from apps.core.exceptions import RestaurantTableSessionConflictError
                raise RestaurantTableSessionConflictError("Close the active table session before changing this table.")
            frozen_changes = {"code", "name", "section", "seats"}.intersection(changed)
            if frozen_changes and RestaurantTableSession.objects.filter(table=table).exists():
                from apps.core.exceptions import RestaurantTableSessionConflictError
                raise RestaurantTableSessionConflictError(
                    "Table identity and layout fields are frozen after the first service session; only activation can change."
                )
            updated = serializer.save(updated_by=request.user) if changed else table
            log_action(
                actor=request.user,
                action="RESTAURANT_TABLE_UPDATED",
                instance=updated,
                request=request,
                changes={key: value for key, value in serializer.validated_data.items()},
                metadata={"code": updated.code, "is_active": updated.is_active},
            )
        return success_response(RestaurantTableSerializer(updated).data, message="Restaurant table updated.")


@extend_schema(tags=["Admin · POS"], summary="List open restaurant table sessions or open one")
class PosRestaurantTableSessionListCreateView(APIView):
    permission_classes = [HasCapability]
    required_capability = "restaurant.order.manage"
    pagination_class = StandardPagination

    def get(self, request):
        queryset = _table_session_queryset()
        state = (request.query_params.get("status") or "OPEN").strip().upper()
        if state == "ALL":
            pass
        elif state in RestaurantTableSession.Status.values:
            queryset = queryset.filter(status=state)
        else:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"status": "Use OPEN, CLOSED, or ALL."})
        table_id = request.query_params.get("table_id")
        if table_id:
            try:
                queryset = queryset.filter(table_id=int(table_id))
            except (TypeError, ValueError):
                from rest_framework.exceptions import ValidationError
                raise ValidationError({"table_id": "Table id must be an integer."})
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(RestaurantTableSessionSerializer(page, many=True).data)

    def post(self, request):
        serializer = RestaurantTableSessionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        session, created = table_service.open_table_session(actor=request.user, **serializer.validated_data)
        session = _table_session_queryset().get(pk=session.pk)
        return success_response(
            RestaurantTableSessionSerializer(session).data,
            message="Restaurant table session opened." if created else "Existing table session returned.",
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class PosRestaurantTableSessionDetailView(APIView):
    permission_classes = [HasCapability]
    required_capability = "restaurant.order.manage"

    def get(self, request, reference):
        session = _table_session_queryset().filter(reference=reference).first()
        if session is None:
            raise NotFound("Restaurant table session not found.")
        return success_response(RestaurantTableSessionSerializer(session).data)


class PosRestaurantTableSessionCloseView(APIView):
    permission_classes = [HasCapability]
    required_capability = "restaurant.order.manage"

    def post(self, request, reference):
        serializer = RestaurantTableSessionCloseSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        session, closed = table_service.close_table_session(
            reference=reference, actor=request.user, **serializer.validated_data
        )
        session = _table_session_queryset().get(pk=session.pk)
        return success_response(
            RestaurantTableSessionSerializer(session).data,
            message="Restaurant table session closed." if closed else "Restaurant table session was already closed.",
        )


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
    required_capabilities = ("pos.order.manage", "restaurant.order.manage", "bar.order.manage")
    require_any_capability = True
    pagination_class = StandardPagination

    def get(self, request):
        queryset = _order_queryset().filter(mode__in=_allowed_order_modes(request.user))
        if mode := (request.query_params.get("mode") or "").strip().upper():
            if mode not in PosOrder.Mode.values:
                from rest_framework.exceptions import ValidationError
                raise ValidationError({"mode": "Invalid POS order mode."})
            _ensure_order_mode(request.user, mode)
            queryset = queryset.filter(mode=mode)
        if order_status := request.query_params.get("status"):
            queryset = queryset.filter(status=order_status)
        if stay_reference := request.query_params.get("stay"):
            queryset = queryset.filter(stay__reference=stay_reference)
        if table_session_reference := (request.query_params.get("table_session") or "").strip():
            queryset = queryset.filter(table_session__reference=table_session_reference)
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
        _ensure_order_mode(request.user, data["mode"])
        service_request = None
        if data.get("service_request_reference"):
            from rest_framework.exceptions import PermissionDenied
            if not has_capability(request.user, "guest_request.manage"):
                raise PermissionDenied("You need guest-request access to create a POS draft from a QR request.")
            service_request = (
                service_request_queryset_for_staff(request.user)
                .select_related("qr_link")
                .filter(reference=data["service_request_reference"])
                .first()
            )
            if service_request is None:
                raise NotFound("Guest service request not found.")
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
            table_session_reference=data["table_session_reference"],
            delivery_location=data["delivery_location"], notes=data["notes"],
            idempotency_key=data["idempotency_key"], service_request=service_request,
        )
        if created and service_request is not None:
            log_action(
                actor=request.user,
                action="SERVICE_REQUEST_POS_DRAFT_CREATED",
                instance=service_request,
                request=request,
                metadata={"request_reference": service_request.reference, "pos_order_reference": order.reference},
            )
        _ensure_order_access(request.user, order)
        order = _get_order(order.reference, detail=True)
        return success_response(
            PosOrderDetailSerializer(order).data,
            message="POS order created." if created else "Existing POS order returned.",
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


@extend_schema(tags=["Admin · POS"], summary="Get POS order detail")
class PosOrderDetailView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("pos.order.manage", "restaurant.order.manage", "bar.order.manage")
    require_any_capability = True

    def get(self, request, reference):
        order = _get_order(reference, detail=True)
        _ensure_order_access(request.user, order)
        return success_response(PosOrderDetailSerializer(order).data)


@extend_schema(tags=["Admin · POS"], summary="Submit POS order to its production station")
class PosOrderSubmitView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("pos.order.manage", "restaurant.order.manage", "bar.order.manage")
    require_any_capability = True

    def post(self, request, reference):
        serializer = PosOrderSubmitSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        existing = _get_order(reference)
        _ensure_order_access(request.user, existing)
        order = order_service.submit_order(
            order=existing, actor=request.user, priority=serializer.validated_data["priority"]
        )
        return success_response(PosOrderDetailSerializer(_get_order(order.reference, detail=True)).data, message="POS order submitted to its production station.")


@extend_schema(tags=["Admin · POS"], summary="Advance POS kitchen or delivery status")
class PosOrderStatusView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("pos.order.manage", "restaurant.order.manage", "bar.order.manage")
    require_any_capability = True

    def post(self, request, reference):
        serializer = PosOrderStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        existing = _get_order(reference)
        _ensure_order_access(request.user, existing)
        order = order_service.transition_order(
            order=existing, target_status=serializer.validated_data["status"], actor=request.user
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
        order = _get_order(reference)
        _ensure_order_access(request.user, order)
        tender = order_service.capture_direct_tender(
            order=order, method=data["method"], amount=data["amount"], actor=request.user,
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
    required_capabilities = (
        "pos.order.manage", "restaurant.order.manage", "bar.order.manage", "kitchen.queue.view", "kitchen.ticket.manage",
    )
    require_any_capability = True
    pagination_class = StandardPagination

    def get(self, request):
        allowed_stations = _allowed_ticket_stations(request.user)
        queryset = KitchenTicket.objects.select_related("order", "assigned_to").prefetch_related("order__lines").exclude(
            status__in=[KitchenTicket.Status.COMPLETED, KitchenTicket.Status.CANCELLED]
        ).filter(station__in=allowed_stations)
        if station := (request.query_params.get("station") or "").strip().upper():
            if station not in KitchenTicket.Station.values:
                from rest_framework.exceptions import ValidationError
                raise ValidationError({"station": "Station must be KITCHEN or BAR."})
            if station not in allowed_stations:
                from rest_framework.exceptions import PermissionDenied
                raise PermissionDenied("You do not have access to this production station.")
            queryset = queryset.filter(station=station)
        queryset = queryset.order_by("-priority", "queued_at", "pk")
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(KitchenTicketSerializer(page, many=True).data)


@extend_schema(tags=["Admin · POS"], summary="Progress a kitchen or bar production ticket")
class KitchenTicketStatusView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = (
        "pos.order.manage", "restaurant.order.manage", "bar.order.manage", "kitchen.ticket.manage",
    )
    require_any_capability = True

    def post(self, request, pk):
        ticket = KitchenTicket.objects.select_related("order").filter(pk=pk).first()
        if ticket is None:
            raise NotFound("Production ticket not found.")
        if ticket.station not in _allowed_ticket_stations(request.user):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("You do not have access to this production station.")
        serializer = KitchenTicketStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        target_status = serializer.validated_data["status"]
        target_order_status = (
            PosOrder.Status.PREPARING if target_status == KitchenTicket.Status.PREPARING else PosOrder.Status.READY
        )
        order_service.transition_order(order=ticket.order, target_status=target_order_status, actor=request.user)
        ticket = KitchenTicket.objects.select_related("order", "assigned_to").get(pk=ticket.pk)
        return success_response(KitchenTicketSerializer(ticket).data, message="Production ticket updated.")


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
