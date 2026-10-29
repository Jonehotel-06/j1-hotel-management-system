"""Capability-protected bounded POS operational API."""
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, DecimalField, OuterRef, Prefetch, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
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
    KitchenTicket, MenuCategory, MenuItem, MenuModifier, PosOrder, PosOrderEvent, PosOrderReturn,
    PosTender, RestaurantTable, RestaurantTableSession,
)
from .serializers import (
    BarcodeLookupSerializer,
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
    PosOrderReturnCreateSerializer,
    PosOrderReturnRefundConfirmSerializer,
    PosOrderReturnSerializer,
    PosOrderStatusSerializer,
    PosOrderSubmitSerializer,
    PosReturnableOrderSerializer,
    PosTenderCaptureSerializer,
    RestaurantTableSerializer,
    RestaurantTableSessionCloseSerializer,
    RestaurantTableSessionCreateSerializer,
    RestaurantTableSessionSerializer,
    RestaurantTableSessionTransferSerializer,
)
from .services import order_service
from .services import return_service
from .services import table_service


def _order_captured_amount_subquery():
    """Captured tender total for one order, as a correlated subquery.

    The order list already aggregates ``line_count``; adding the collection
    total as another aggregate would multiply rows and inflate both figures, so
    it is a single-value subquery instead. The serializer turns this into
    ``amount_paid`` / ``outstanding_balance`` — the POS never derives what a
    guest owes from the frozen order total.
    """
    return Subquery(
        PosTender.objects.filter(order=OuterRef("pk"), status=PosTender.Status.CAPTURED)
        .values("order")
        .annotate(
            captured=Coalesce(
                Sum("amount"),
                Value(Decimal("0.00")),
                output_field=DecimalField(max_digits=14, decimal_places=2),
            )
        )
        .values("captured")[:1]
    )


def _order_queryset():
    return (
        PosOrder.objects.select_related("stay", "folio", "created_by", "charge_transaction", "table_session__table", "service_request")
        .annotate(
            line_count=Count("lines"),
            captured_amount=Coalesce(
                _order_captured_amount_subquery(),
                Value(Decimal("0.00")),
                output_field=DecimalField(max_digits=14, decimal_places=2),
            ),
        )
        .order_by("-created_at", "-pk")
    )


def _table_session_captured_subquery():
    """Total captured tender amount across the session's counted orders.

    Built as a correlated subquery so the surrounding session queryset stays a
    single aggregate pass (no N+1 and no multi-aggregation row multiplication).
    """
    return Subquery(
        PosTender.objects.filter(
            order__table_session=OuterRef("pk"),
            order__status__in=table_service.COUNTED_ORDER_STATUSES,
            status=PosTender.Status.CAPTURED,
        )
        .values("order__table_session")
        .annotate(
            captured=Coalesce(
                Sum("amount"),
                Value(Decimal("0.00")),
                output_field=DecimalField(max_digits=14, decimal_places=2),
            )
        )
        .values("captured")[:1]
    )


def _table_summary_captured_subquery():
    """Total captured tender amount across the orders of one table's sessions."""
    return Subquery(
        PosTender.objects.filter(
            order__table_session__table=OuterRef("pk"),
            order__status__in=table_service.COUNTED_ORDER_STATUSES,
            status=PosTender.Status.CAPTURED,
        )
        .values("order__table_session__table")
        .annotate(
            captured=Coalesce(
                Sum("amount"),
                Value(Decimal("0.00")),
                output_field=DecimalField(max_digits=14, decimal_places=2),
            )
        )
        .values("captured")[:1]
    )


def _table_summary_queryset():
    """Per-registered-table order/money summary.

    Orders are counted through the table's sessions, so a transferred session and
    its orders move with it to the destination table exactly once — there is no
    double count and no reliance on free-text table labels. Cancelled and voided
    orders are excluded from the guest-spend figures.
    """
    counted = Q(sessions__orders__status__in=table_service.COUNTED_ORDER_STATUSES)
    return RestaurantTable.objects.annotate(
        guest_order_count=Count("sessions__orders", filter=counted, distinct=True),
        total_ordered_amount=Coalesce(
            Sum("sessions__orders__total_amount", filter=counted, output_field=DecimalField(max_digits=14, decimal_places=2)),
            Value(Decimal("0.00")),
            output_field=DecimalField(max_digits=14, decimal_places=2),
        ),
        returned_amount=Coalesce(
            Sum("sessions__orders__returned_amount", filter=counted, output_field=DecimalField(max_digits=14, decimal_places=2)),
            Value(Decimal("0.00")),
            output_field=DecimalField(max_digits=14, decimal_places=2),
        ),
        captured_amount=Coalesce(
            _table_summary_captured_subquery(),
            Value(Decimal("0.00")),
            output_field=DecimalField(max_digits=14, decimal_places=2),
        ),
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
            guest_order_count=Count(
                "orders", filter=Q(orders__status__in=table_service.COUNTED_ORDER_STATUSES), distinct=True
            ),
            total_ordered_amount=Coalesce(
                Sum(
                    "orders__total_amount",
                    filter=Q(orders__status__in=table_service.COUNTED_ORDER_STATUSES),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                ),
                Value(Decimal("0.00")),
                output_field=DecimalField(max_digits=14, decimal_places=2),
            ),
            returned_amount=Coalesce(
                Sum(
                    "orders__returned_amount",
                    filter=Q(orders__status__in=table_service.COUNTED_ORDER_STATUSES),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                ),
                Value(Decimal("0.00")),
                output_field=DecimalField(max_digits=14, decimal_places=2),
            ),
            captured_amount=Coalesce(
                _table_session_captured_subquery(),
                Value(Decimal("0.00")),
                output_field=DecimalField(max_digits=14, decimal_places=2),
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
        .annotate(
            line_count=Count("lines"),
            captured_amount=Coalesce(
                _order_captured_amount_subquery(),
                Value(Decimal("0.00")),
                output_field=DecimalField(max_digits=14, decimal_places=2),
            ),
        )
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
        # One bulk stock resolution for the whole menu: each item the picker
        # shows carries its fulfilment store and current availability, computed
        # from the same recipe/direct-sale mapping the delivery-time deduction
        # uses (see apps.inventory.services.inventory_service.pos_stock_summaries).
        from apps.inventory.services.inventory_service import pos_stock_summaries

        menu_item_ids = [
            item.pk for category in categories for item in category._available_items
        ]
        return success_response(
            MenuCategoryMenuSerializer(
                categories, many=True, context={"pos_stock_summaries": pos_stock_summaries(menu_item_ids)}
            ).data
        )


@extend_schema(tags=["Admin · POS"], summary="Resolve one scanned barcode to a sellable menu item")
class PosBarcodeLookupView(APIView):
    """Narrow, single-product lookup for USB HID barcode scanners.

    One indexed query per scan (barcode/SKU) instead of downloading the
    whole catalogue. The response carries the server-stored selling price;
    checkout still re-prices every line in ``order_service``.

    ``menu_item_id`` (optional) resolves a known menu item directly instead of
    parsing a barcode: the POS page uses it to re-verify an item's CURRENT
    stock before adding it to an order, instead of relying on the figures
    embedded when the menu was loaded. It accepts the same ``mode``/``area``
    permission scoping and returns the identical payload shape, so every
    existing consumer keeps working unchanged.
    """

    permission_classes = [HasCapability]
    required_capabilities = ("pos.order.manage", "restaurant.order.manage", "bar.order.manage")
    require_any_capability = True

    def get(self, request):
        from rest_framework.exceptions import ValidationError

        from .services.barcode_service import barcode_lookup_payload, resolve_menu_item

        code = request.query_params.get("barcode") or request.query_params.get("code") or ""
        mode = (request.query_params.get("mode") or "").strip().upper()
        area = (request.query_params.get("area") or "").strip().upper()
        raw_menu_item_id = (request.query_params.get("menu_item_id") or "").strip()
        if mode and mode not in PosOrder.Mode.values:
            raise ValidationError({"mode": "Enter a valid POS order mode."})
        if area and area not in MenuCategory.ServiceArea.values:
            raise ValidationError({"area": "Area must be RESTAURANT or BAR."})
        if mode:
            # Reuse the order-mode permission map: a bartender scanning in
            # BAR mode must not resolve restaurant-only products, and the
            # sale itself would be rejected by order_service anyway.
            _ensure_order_mode(request.user, mode)
            if mode == PosOrder.Mode.BAR:
                service_areas = {MenuCategory.ServiceArea.BAR}
            elif mode == PosOrder.Mode.ROOM_SERVICE:
                service_areas = {MenuCategory.ServiceArea.RESTAURANT, MenuCategory.ServiceArea.BAR}
            else:
                service_areas = {MenuCategory.ServiceArea.RESTAURANT}
        elif area:
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
            service_areas = {area}
        else:
            allowed_modes = _allowed_order_modes(request.user)
            service_areas = set()
            if {PosOrder.Mode.BAR, PosOrder.Mode.ROOM_SERVICE} & allowed_modes:
                service_areas.add(MenuCategory.ServiceArea.BAR)
            if allowed_modes - {PosOrder.Mode.BAR}:
                service_areas.add(MenuCategory.ServiceArea.RESTAURANT)
            if has_capability(request.user, "pos.order.manage"):
                service_areas = {MenuCategory.ServiceArea.RESTAURANT, MenuCategory.ServiceArea.BAR}
        if raw_menu_item_id:
            try:
                menu_item_id = int(raw_menu_item_id)
            except (TypeError, ValueError) as exc:
                raise ValidationError({"menu_item_id": "menu_item_id must be an integer."}) from exc
            payload = barcode_lookup_payload(
                resolve_menu_item(menu_item_id, service_areas=service_areas)
            )
        else:
            payload = barcode_lookup_payload(code, service_areas=service_areas)
        return success_response(BarcodeLookupSerializer(payload).data)


CATEGORY_ORDERINGS = {
    "name": ("name",),
    "-name": ("-name",),
    "slug": ("slug",),
    "-slug": ("-slug",),
    "service_area": ("service_area", "sort_order", "name"),
    "-service_area": ("-service_area", "sort_order", "name"),
    "sort_order": ("sort_order", "name"),
    "-sort_order": ("-sort_order", "name"),
    "items": ("item_count", "name"),
    "-items": ("-item_count", "name"),
    "updated": ("-updated_at",),
    "-updated": ("updated_at",),
}

ITEM_ORDERINGS = {
    "name": ("name",),
    "-name": ("-name",),
    "sku": ("sku",),
    "-sku": ("-sku",),
    "barcode": ("barcode",),
    "-barcode": ("-barcode",),
    "price": ("base_price", "name"),
    "-price": ("-base_price", "name"),
    "category": ("category__name", "name"),
    "-category": ("-category__name", "name"),
    "sort_order": ("sort_order", "name"),
    "-sort_order": ("-sort_order", "name"),
    "updated": ("-updated_at",),
    "-updated": ("updated_at",),
}


def _boolean_param(request, name):
    raw = (request.query_params.get(name) or "").strip().lower()
    if raw in {"", "all"}:
        return None
    if raw in {"1", "true", "yes"}:
        return True
    if raw in {"0", "false", "no"}:
        return False
    from rest_framework.exceptions import ValidationError

    raise ValidationError({name: "Use true, false, or all."})


@extend_schema(tags=["Admin · POS"], summary="Manage POS menu categories")
class PosMenuCategoryManagementView(APIView):
    """Menu categories with server-side search, filtering, sorting, pagination.

    The table is paginated only when the caller asks for a page (``page`` or
    ``page_size``), so the historical contract — a plain list for the order-entry
    screen and older clients — keeps working unchanged.
    """

    permission_classes = [HasCapability]
    required_capabilities = ("pos.menu.manage", "restaurant.menu.manage", "bar.menu.manage")
    require_any_capability = True
    pagination_class = StandardPagination

    def get(self, request):
        queryset = MenuCategory.objects.all()
        areas = _allowed_menu_areas(request.user)
        if areas is not None:
            queryset = queryset.filter(service_area__in=areas)
        search = (request.query_params.get("search") or "").strip()
        if search:
            if len(search) > 100:
                from rest_framework.exceptions import ValidationError

                raise ValidationError({"search": "Search is limited to 100 characters."})
            queryset = queryset.filter(
                Q(name__icontains=search) | Q(slug__icontains=search) | Q(description__icontains=search)
            )
        area_param = (request.query_params.get("service_area") or "").strip().upper()
        if area_param:
            if area_param not in MenuCategory.ServiceArea.values:
                from rest_framework.exceptions import ValidationError

                raise ValidationError({"service_area": "Area must be RESTAURANT or BAR."})
            _ensure_menu_area(request.user, area_param)
            queryset = queryset.filter(service_area=area_param)
        is_active = _boolean_param(request, "is_active")
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active)
        ordering = (request.query_params.get("ordering") or "service_area").strip()
        fields = CATEGORY_ORDERINGS.get(ordering)
        if fields is None:
            from rest_framework.exceptions import ValidationError

            raise ValidationError({"ordering": f"Use one of: {', '.join(sorted(CATEGORY_ORDERINGS))}."})
        queryset = queryset.annotate(item_count=Count("items")).order_by(*fields)
        wants_page = any(
            (request.query_params.get(key) or "").strip() for key in ("page", "page_size")
        )
        if wants_page:
            paginator = self.pagination_class()
            page = paginator.paginate_queryset(queryset, request, view=self)
            return paginator.get_paginated_response(MenuCategoryManageSerializer(page, many=True).data)
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
        items = MenuItem.objects.select_related("category").prefetch_related(
            "stock_recipes__location", "direct_sale_stock_items__direct_sale_location"
        )
        areas = _allowed_menu_areas(request.user)
        if areas is not None:
            items = items.filter(category__service_area__in=areas)
        search = (request.query_params.get("search") or "").strip()
        if search:
            if len(search) > 100:
                from rest_framework.exceptions import ValidationError

                raise ValidationError({"search": "Search is limited to 100 characters."})
            items = items.filter(
                Q(name__icontains=search)
                | Q(sku__icontains=search)
                | Q(barcode__icontains=search)
                | Q(category__name__icontains=search)
            )
        category_param = (request.query_params.get("category") or "").strip()
        if category_param:
            try:
                category_id = int(category_param)
            except (TypeError, ValueError) as exc:
                from rest_framework.exceptions import ValidationError

                raise ValidationError({"category": "Category must be an integer identifier."}) from exc
            items = items.filter(category_id=category_id)
        area_param = (request.query_params.get("service_area") or "").strip().upper()
        if area_param:
            if area_param not in MenuCategory.ServiceArea.values:
                from rest_framework.exceptions import ValidationError

                raise ValidationError({"service_area": "Area must be RESTAURANT or BAR."})
            _ensure_menu_area(request.user, area_param)
            items = items.filter(category__service_area=area_param)
        for flag, field in (("is_active", "is_active"), ("is_available", "is_available")):
            value = _boolean_param(request, flag)
            if value is not None:
                items = items.filter(**{field: value})
        ordering = (request.query_params.get("ordering") or "category").strip()
        fields = ITEM_ORDERINGS.get(ordering)
        if fields is None:
            from rest_framework.exceptions import ValidationError

            raise ValidationError({"ordering": f"Use one of: {', '.join(sorted(ITEM_ORDERINGS))}."})
        items = items.order_by(*fields)
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(items, request, view=self)
        context = {"request": request, "include_margin": bool(_boolean_param(request, "with_margin"))}
        return paginator.get_paginated_response(
            MenuItemManageSerializer(page, many=True, context=context).data
        )

    def post(self, request):
        serializer = MenuItemManageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        _ensure_menu_area(request.user, serializer.validated_data["category"].service_area)
        return success_response(
            MenuItemManageSerializer(serializer.save(), context={"request": request, "include_margin": True}).data,
            message="Menu item created.", status=status.HTTP_201_CREATED,
        )


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
        return success_response(
            MenuItemManageSerializer(serializer.save(), context={"request": request, "include_margin": True}).data,
            message="Menu item updated.",
        )


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
        queryset = _table_summary_queryset()
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


@extend_schema(tags=["Admin · POS"], summary="Transfer an open restaurant table session")
class PosRestaurantTableSessionTransferView(APIView):
    permission_classes = [HasCapability]
    required_capability = "restaurant.order.manage"

    def post(self, request, reference):
        from rest_framework.exceptions import PermissionDenied

        if not has_capability(request.user, "restaurant.order.manage"):
            raise PermissionDenied("You cannot transfer restaurant table sessions.")
        serializer = RestaurantTableSessionTransferSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        session, changed = table_service.transfer_table_session(
            reference=reference, destination_table_id=serializer.validated_data["destination_table_id"], actor=request.user
        )
        session = _table_session_queryset().get(pk=session.pk)
        return success_response(
            RestaurantTableSessionSerializer(session).data,
            message="Table session transferred." if changed else "Table session already uses this destination.",
        )


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
            currency=data["currency"], idempotency_key=data["idempotency_key"],
            expected_outstanding_balance=data.get("expected_outstanding_balance"),
        )
        # A repeated idempotency key converges on the tender already recorded, so
        # the answer is 200 (nothing new was collected) rather than 201.
        replayed = bool(getattr(tender, "idempotent_replay", False))
        return success_response(
            PosOrderDetailSerializer(_get_order(tender.order.reference, detail=True)).data,
            message=(
                "This tender was already captured; the existing record was returned."
                if replayed else "POS tender captured."
            ),
            status=status.HTTP_200_OK if replayed else status.HTTP_201_CREATED,
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


# ---------------------------------------------------------------------------
# Bar unopened-item returns
#
# The owner's workflow: a guest orders twelve bottles, leaves five unopened,
# and the hotel must charge for seven, put five bottles back, and refund the
# difference. Every amount is derived on the server from the frozen order line
# and the captured tenders; the browser only chooses quantities and a method.
# ---------------------------------------------------------------------------


def _return_scope(request):
    """Authorize the caller and confine the query to their service areas."""
    modes = _allowed_order_modes(request.user)
    allowed = set(return_service.RETURNABLE_MODES) & modes
    if not allowed:
        from rest_framework.exceptions import PermissionDenied

        raise PermissionDenied("You do not have permission to record unopened-item returns.")
    return allowed


@extend_schema(tags=["Admin · POS"], summary="Bar/restaurant orders that can take an unopened-item return")
class PosReturnableOrderListView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("pos.return.record", "pos.order.manage")
    require_any_capability = True

    def get(self, request):
        allowed = _return_scope(request)
        orders = [
            order
            for order in return_service.eligible_orders(
                search=request.query_params.get("search", ""),
                order_reference=request.query_params.get("order", ""),
                unsettled_only=(request.query_params.get("unsettled") or "").lower() in {"1", "true", "yes"},
                limit=request.query_params.get("limit", 200),
            )
            if order.mode in allowed
        ]
        return success_response(PosReturnableOrderSerializer(orders, many=True).data)


@extend_schema(tags=["Admin · POS"], summary="Per-line returnable quantities and money for one order")
class PosOrderReturnPreviewView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("pos.return.record", "pos.order.manage")
    require_any_capability = True

    def get(self, request, reference):
        order = _get_order(reference, detail=True)
        _ensure_order_access(request.user, order)
        if order.mode not in return_service.RETURNABLE_MODES:
            from rest_framework.exceptions import ValidationError

            raise ValidationError(
                "Unopened-item returns are recorded against bar and restaurant service only."
            )
        return success_response(return_service.return_preview(order))


@extend_schema(tags=["Admin · POS"], summary="Record an unopened-item return and its stock/refund effect")
class PosOrderReturnListCreateView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("pos.return.record", "pos.order.manage")
    require_any_capability = True
    pagination_class = StandardPagination

    def get(self, request):
        modes = _return_scope(request)
        queryset = return_service.return_queryset().filter(order__mode__in=modes)
        status_filter = (request.query_params.get("refund_status") or "").strip().upper()
        if status_filter:
            if status_filter not in PosOrderReturn.RefundStatus.values:
                from rest_framework.exceptions import ValidationError

                raise ValidationError({"refund_status": "Use a valid refund status."})
            queryset = queryset.filter(refund_status=status_filter)
        if search := (request.query_params.get("search") or "").strip()[:100]:
            queryset = queryset.filter(
                Q(reference__icontains=search)
                | Q(order__reference__icontains=search)
                | Q(order__guest_name__icontains=search)
            )
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(PosOrderReturnSerializer(page, many=True).data)

    def post(self, request, reference):
        order = _get_order(reference)
        _ensure_order_access(request.user, order)
        serializer = PosOrderReturnCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        cash_session = None
        session_reference = (data.get("cash_session_reference") or "").strip()
        if session_reference:
            cash_session = CashSession.objects.filter(reference=session_reference).first()
            if cash_session is None:
                from rest_framework.exceptions import ValidationError

                raise ValidationError({"cash_session_reference": "That cashier session does not exist."})
        restock_location = None
        if data.get("restock_location"):
            from apps.inventory.models import StockLocation

            restock_location = StockLocation.objects.filter(pk=data["restock_location"], is_active=True).first()
            if restock_location is None:
                from rest_framework.exceptions import ValidationError

                raise ValidationError({"restock_location": "Choose an active stock location."})
        record, created = return_service.record_unopened_return(
            order=order,
            lines=data["lines"],
            actor=request.user,
            reason=data.get("reason", ""),
            notes=data.get("notes", ""),
            idempotency_key=data.get("idempotency_key") or request.headers.get("Idempotency-Key", ""),
            restock_location=restock_location,
            received_by=data.get("received_by", ""),
            refund_method=data.get("refund_method") or PosOrderReturn.RefundMethod.NONE,
            cash_session=cash_session,
        )
        payload = PosOrderReturnSerializer(
            return_service.return_queryset().get(pk=record.pk)
        ).data
        message = (
            f"Unopened items returned on {order.reference}. Final charge {record.final_total} "
            f"{order.currency}; refund {record.refund_due} ({record.get_refund_status_display().lower()})."
            if created
            else "This return was already recorded; nothing was changed."
        )
        return success_response(
            payload, message=message, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK
        )


@extend_schema(tags=["Admin · POS"], summary="Complete or fail a return refund")
class PosOrderReturnRefundConfirmView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("pos.return.refund",)

    def post(self, request, reference):
        modes = _return_scope(request)
        record = return_service.return_queryset().filter(reference=reference).first()
        if record is None or record.order.mode not in modes:
            raise NotFound("Unopened-item return not found.")
        if record.order.mode not in _allowed_order_modes(request.user):
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied("You do not have permission to refund this order's service area.")
        serializer = PosOrderReturnRefundConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        cash_session = None
        session_reference = (data.get("cash_session_reference") or "").strip()
        if session_reference:
            cash_session = CashSession.objects.filter(reference=session_reference).first()
            if cash_session is None:
                from rest_framework.exceptions import ValidationError

                raise ValidationError({"cash_session_reference": "That cashier session does not exist."})
        record = return_service.confirm_return_refund(
            record=record,
            actor=request.user,
            refund_method=data.get("refund_method", ""),
            provider_reference=data.get("provider_reference", ""),
            succeeded=data.get("succeeded", True),
            failure_reason=data.get("failure_reason", ""),
            cash_session=cash_session,
        )
        return success_response(
            PosOrderReturnSerializer(return_service.return_queryset().get(pk=record.pk)).data,
            message=f"Refund for {record.reference} is now {record.get_refund_status_display().lower()}.",
        )
