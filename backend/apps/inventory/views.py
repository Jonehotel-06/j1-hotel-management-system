"""Bounded capability-protected inventory and procurement operations API."""
import secrets
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError
from django.db.models import Count, F, Q
from django.utils.dateparse import parse_date

from apps.core.utils import hotel_date_bounds
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.views import APIView

from apps.audit.services import log_action
from apps.core.pagination import StandardPagination
from apps.core.permissions import HasCapability
from apps.core.responses import success_response

from .models import GoodsReceipt, PurchaseOrder, StockBalance, StockConsumptionRequest, StockCount, StockItem, StockLocation, StockMovement, StockRecipe, Supplier
from .serializers import (
    GoodsReceiptCreateSerializer, GoodsReceiptSerializer, IssueStockSerializer, OpeningStockSerializer,
    PurchaseOrderCreateSerializer, PurchaseOrderDetailSerializer, PurchaseOrderListSerializer,
    PurchaseOrderPaymentInputSerializer, PurchaseOrderUpdateSerializer, ReturnStockSerializer,
    StockBalanceSerializer, StockConsumptionRequestSerializer,
    StockCountCreateSerializer, StockCountSerializer, StockCountSubmitSerializer, StockItemSerializer,
    InventoryMenuItemDirectorySerializer,
    ProductProfitabilityRowSerializer, ProductProfitabilitySummarySerializer,
    StockLocationSerializer, StockMovementSerializer, StockRecipeCreateSerializer, StockRecipeSerializer,
    SupplierSerializer, TransferStockSerializer,
)
from .services.valuation_service import (
    SORT_KEYS, product_profitability_projection, projection_filter_choices, projection_summary, sort_projection,
)
from .services.inventory_service import (
    approve_purchase_order, approve_stock_count, attach_purchase_order_receipt, create_purchase_order,
    create_stock_recipe, issue_stock, mark_purchase_order_ordered,
    process_pending_pos_consumption, process_pos_stock_consumption, receive_goods, record_opening_stock,
    record_purchase_order_payment, record_stock_return, retire_stock_recipe,
    reverse_pos_stock_consumption, scoped_key, start_stock_count,
    submit_purchase_order, submit_stock_count, transfer_stock, update_purchase_order,
)


def _page(view, request, queryset, serializer):
    paginator = StandardPagination(); page = paginator.paginate_queryset(queryset, request, view=view)
    return paginator.get_paginated_response(serializer(page, many=True).data)


def _supplier_reference():
    return f"SUP-{secrets.token_hex(6).upper()}"


def _po_or_404(reference, *, detail=False):
    queryset = PurchaseOrder.objects.select_related(
        "supplier", "delivery_location", "requested_by", "approved_by", "paid_by",
    ).annotate(line_count=Count("lines"))
    if detail: queryset = queryset.prefetch_related("lines", "payments")
    po = queryset.filter(reference=reference).first()
    if po is None: raise NotFound("Purchase order not found.")
    return po


@extend_schema(tags=["Admin · Inventory"], summary="List or create stock locations")
class StockLocationListCreateView(APIView):
    permission_classes = [HasCapability]; required_capability = "inventory.manage"
    def get(self, request):
        queryset=StockLocation.objects.all().order_by("name")
        if request.query_params.get("active") == "true": queryset=queryset.filter(is_active=True)
        if search:=request.query_params.get("search"):
            queryset=queryset.filter(Q(code__icontains=search)|Q(name__icontains=search))
        return _page(self, request, queryset, StockLocationSerializer)
    def post(self, request):
        serializer=StockLocationSerializer(data=request.data); serializer.is_valid(raise_exception=True); location=serializer.save()
        log_action(actor=request.user, action="STOCK_LOCATION_CREATED", instance=location, request=request)
        return success_response(StockLocationSerializer(location).data, message="Stock location created.", status=status.HTTP_201_CREATED)


class StockLocationDetailView(APIView):
    permission_classes = [HasCapability]; required_capability = "inventory.manage"
    def patch(self, request, pk):
        location=StockLocation.objects.filter(pk=pk).first()
        if location is None: raise NotFound("Stock location not found.")
        serializer=StockLocationSerializer(location, data=request.data, partial=True); serializer.is_valid(raise_exception=True); location=serializer.save()
        return success_response(StockLocationSerializer(location).data, message="Stock location updated.")


@extend_schema(tags=["Admin · Inventory"], summary="List or create stock catalog items")
class StockItemListCreateView(APIView):
    permission_classes = [HasCapability]; required_capability = "inventory.manage"
    def get(self, request):
        queryset=StockItem.objects.select_related("preferred_supplier").order_by("category", "name", "pk")
        if request.query_params.get("active") == "true": queryset=queryset.filter(is_active=True)
        if category:=request.query_params.get("category"): queryset=queryset.filter(category=category)
        if search:=request.query_params.get("search"): queryset=queryset.filter(Q(sku__icontains=search)|Q(name__icontains=search))
        return _page(self, request, queryset, StockItemSerializer)
    def post(self, request):
        serializer=StockItemSerializer(data=request.data); serializer.is_valid(raise_exception=True)
        try:
            item=serializer.save()
        except IntegrityError as exc:
            # The serializer pre-checks the direct-sale configuration; this is
            # the database's unique constraint winning a race, and it must come
            # back as a readable 400 rather than a 500.
            raise ValidationError({"detail": "Another record was saved with the same code or the same direct-sale POS menu item. Reload and try again."}) from exc
        opening_qty = request.data.get("opening_quantity")
        opening_loc_id = request.data.get("opening_location_id")
        if opening_qty not in (None, "", "0", 0, "0.0000") and opening_loc_id:
            loc = StockLocation.objects.filter(pk=opening_loc_id, is_active=True).first()
            if loc is not None:
                record_opening_stock(
                    item=item,
                    location=loc,
                    quantity=opening_qty,
                    actor=request.user,
                    source_key=f"item-opening:{item.pk}:{loc.pk}",
                    unit_cost=item.standard_cost,
                    notes="Initial opening stock on item creation",
                )
        log_action(actor=request.user, action="STOCK_ITEM_CREATED", instance=item, request=request)
        return success_response(StockItemSerializer(item).data, message="Stock item created.", status=status.HTTP_201_CREATED)


@extend_schema(tags=["Admin · Inventory"], summary="Find active POS menu items for inventory recipe setup")
class InventoryMenuItemDirectoryView(APIView):
    permission_classes = [HasCapability]; required_capability = "inventory.manage"

    def get(self, request):
        from apps.pos.models import MenuItem

        queryset = MenuItem.objects.filter(is_active=True).select_related("category").order_by("category__sort_order", "category__name", "name", "pk")
        if search := request.query_params.get("search"):
            queryset = queryset.filter(Q(sku__icontains=search) | Q(name__icontains=search) | Q(category__name__icontains=search))
        return _page(self, request, queryset, InventoryMenuItemDirectorySerializer)


class StockItemDetailView(APIView):
    permission_classes = [HasCapability]; required_capability = "inventory.manage"
    def patch(self, request, pk):
        item=StockItem.objects.select_related("preferred_supplier").filter(pk=pk).first()
        if item is None: raise NotFound("Stock item not found.")
        serializer=StockItemSerializer(item, data=request.data, partial=True); serializer.is_valid(raise_exception=True)
        try:
            item=serializer.save()
        except IntegrityError as exc:
            raise ValidationError({"detail": "Another record was saved with the same code or the same direct-sale POS menu item. Reload and try again."}) from exc
        # Classification and direct-sale configuration decide HOW stock is
        # deducted, so the change is auditable like any other control change.
        log_action(
            actor=request.user, action="STOCK_ITEM_UPDATED", instance=item, request=request,
            summary=f"Stock item {item.sku} updated",
            metadata={
                "sku": item.sku, "item_type": item.item_type,
                "direct_sale_menu_item": item.direct_sale_menu_item_id,
                "direct_sale_location": item.direct_sale_location_id,
                "direct_sale_quantity": str(item.direct_sale_quantity),
                "direct_sale_configured": item.is_direct_sale_configured,
            },
        )
        return success_response(StockItemSerializer(item).data, message="Stock item updated.")


@extend_schema(tags=["Admin · Inventory"], summary="List current locked stock-balance projections")
class StockBalanceListView(APIView):
    permission_classes = [HasCapability]; required_capability = "inventory.manage"
    def get(self, request):
        queryset=StockBalance.objects.select_related("item", "location").order_by("item__sku", "location__code")
        if item_id:=request.query_params.get("item_id"): queryset=queryset.filter(item_id=item_id)
        if location_id:=request.query_params.get("location_id"): queryset=queryset.filter(location_id=location_id)
        if request.query_params.get("reorder") == "true": queryset=queryset.filter(quantity_on_hand__lte=F("item__reorder_level"))
        if item_type:=request.query_params.get("item_type"): queryset=queryset.filter(item__item_type=item_type)
        return _page(self, request, queryset, StockBalanceSerializer)


@extend_schema(tags=["Admin · Inventory"], summary="List immutable stock movement ledger")
class StockMovementListView(APIView):
    permission_classes = [HasCapability]; required_capability = "inventory.manage"
    def get(self, request):
        queryset=StockMovement.objects.select_related("item", "location", "actor").order_by("-occurred_at", "-pk")
        if item_id:=request.query_params.get("item_id"): queryset=queryset.filter(item_id=item_id)
        if location_id:=request.query_params.get("location_id"): queryset=queryset.filter(location_id=location_id)
        if movement_type:=request.query_params.get("type"): queryset=queryset.filter(type=movement_type)
        if department:=request.query_params.get("department"): queryset=queryset.filter(department=department)
        if item_type:=request.query_params.get("item_type"): queryset=queryset.filter(item__item_type=item_type)
        start = request.query_params.get("start")
        end = request.query_params.get("end")
        start_date = parse_date(start) if start else None
        end_date = parse_date(end) if end else None
        if start and start_date is None: raise ValidationError({"start": "Use ISO date format YYYY-MM-DD."})
        if end and end_date is None: raise ValidationError({"end": "Use ISO date format YYYY-MM-DD."})
        if start_date and end_date and end_date < start_date:
            raise ValidationError({"end": "End date cannot be before start date."})
        if start_date and end_date:
            lower, upper = hotel_date_bounds(start_date, end_date)
            queryset = queryset.filter(occurred_at__gte=lower, occurred_at__lt=upper)
        elif start_date:
            lower, _ = hotel_date_bounds(start_date, start_date)
            queryset = queryset.filter(occurred_at__gte=lower)
        elif end_date:
            _, upper = hotel_date_bounds(end_date, end_date)
            queryset = queryset.filter(occurred_at__lt=upper)
        return _page(self, request, queryset, StockMovementSerializer)


def _recipe_or_404(reference):
    recipe = StockRecipe.objects.select_related("menu_item", "location", "created_by", "retired_by").prefetch_related("lines__item").filter(reference=reference).first()
    if recipe is None: raise NotFound("Stock recipe not found.")
    return recipe


@extend_schema(tags=["Admin · Inventory"], summary="List or create versioned POS menu stock recipes")
class StockRecipeListCreateView(APIView):
    permission_classes = [HasCapability]; required_capability = "inventory.manage"
    def get(self, request):
        queryset = StockRecipe.objects.select_related("menu_item", "location", "created_by", "retired_by").prefetch_related("lines__item").order_by("menu_item__sku", "-version")
        if request.query_params.get("active") == "true": queryset = queryset.filter(is_active=True)
        if menu_item_id := request.query_params.get("menu_item_id"): queryset = queryset.filter(menu_item_id=menu_item_id)
        return _page(self, request, queryset, StockRecipeSerializer)
    def post(self, request):
        serializer = StockRecipeCreateSerializer(data=request.data); serializer.is_valid(raise_exception=True); data = serializer.validated_data
        from apps.pos.models import MenuItem
        menu_item = MenuItem.objects.filter(pk=data["menu_item_id"], is_active=True).first()
        if menu_item is None: raise NotFound("Active POS menu item not found.")
        location = StockLocation.objects.filter(pk=data["location_id"]).first()
        if location is None: raise NotFound("Stock location not found.")
        key = scoped_key(prefix="stock-recipe", scope=f"menu:{menu_item.pk}", raw_key=data["idempotency_key"])
        recipe, created = create_stock_recipe(menu_item=menu_item, location=location, tracking_mode=data["tracking_mode"],
                                               components=data.get("components", []), actor=request.user, idempotency_key=key,
                                               notes=data.get("notes", ""))
        recipe = _recipe_or_404(recipe.reference)
        if created: log_action(actor=request.user, action="STOCK_RECIPE_VERSION_CREATED", instance=recipe, request=request, metadata={"reference": recipe.reference, "version": recipe.version})
        return success_response(StockRecipeSerializer(recipe).data, message="Stock recipe version created." if created else "Existing stock recipe returned.", status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class StockRecipeRetireView(APIView):
    permission_classes = [HasCapability]; required_capability = "inventory.manage"
    def post(self, request, reference):
        recipe = retire_stock_recipe(recipe=_recipe_or_404(reference), actor=request.user)
        recipe = _recipe_or_404(recipe.reference)
        log_action(actor=request.user, action="STOCK_RECIPE_RETIRED", instance=recipe, request=request, metadata={"reference": recipe.reference})
        return success_response(StockRecipeSerializer(recipe).data, message="Stock recipe retired.")


@extend_schema(tags=["Admin · Inventory"], summary="List durable POS-to-inventory recipe-consumption hand-offs")
class StockConsumptionRequestListView(APIView):
    permission_classes = [HasCapability]; required_capability = "inventory.manage"
    def get(self, request):
        queryset = StockConsumptionRequest.objects.select_related("pos_order", "pos_order__created_by", "processed_by").order_by("-requested_at", "-pk")
        if status_value := request.query_params.get("status"):
            queryset = queryset.filter(status=status_value)
        return _page(self, request, queryset, StockConsumptionRequestSerializer)


class StockConsumptionRequestProcessView(APIView):
    permission_classes = [HasCapability]; required_capability = "inventory.manage"
    def post(self, request, reference):
        consumption = StockConsumptionRequest.objects.filter(reference=reference).first()
        if consumption is None: raise NotFound("Stock-consumption request not found.")
        consumption, created = process_pos_stock_consumption(request=consumption, actor=request.user)
        consumption = StockConsumptionRequest.objects.select_related("pos_order", "pos_order__created_by", "processed_by").get(pk=consumption.pk)
        if consumption.status in {StockConsumptionRequest.Status.PROCESSED, StockConsumptionRequest.Status.SKIPPED}:
            message = "POS stock consumption processed." if created else "POS stock consumption was already processed."
        else:
            message = "POS stock consumption needs recipe configuration before it can post."
        log_action(actor=request.user, action="POS_STOCK_CONSUMPTION_HANDLED", instance=consumption, request=request,
                   metadata={"reference": consumption.reference, "status": consumption.status})
        return success_response(StockConsumptionRequestSerializer(consumption).data, message=message)


class StockConsumptionCatchUpView(APIView):
    permission_classes = [HasCapability]; required_capability = "inventory.manage"
    def post(self, request):
        limit = request.data.get("limit", 200) if isinstance(request.data, dict) else 200
        summary = process_pending_pos_consumption(limit=limit, actor=request.user)
        return success_response(summary, message="Pending POS consumption hand-offs processed.")


class StockConsumptionReverseView(APIView):
    permission_classes = [HasCapability]; required_capability = "inventory.manage"
    def post(self, request, reference):
        consumption = StockConsumptionRequest.objects.filter(reference=reference).first()
        if consumption is None: raise NotFound("Stock-consumption request not found.")
        reason = (request.data.get("reason") or "") if isinstance(request.data, dict) else ""
        consumption, reversed_now = reverse_pos_stock_consumption(
            request_or_order=consumption, actor=request.user, reason=reason,
        )
        consumption = StockConsumptionRequest.objects.select_related("pos_order", "pos_order__created_by", "processed_by").get(pk=consumption.pk)
        log_action(actor=request.user, action="POS_STOCK_CONSUMPTION_REVERSED", instance=consumption, request=request,
                   metadata={"reference": consumption.reference, "reversed": reversed_now})
        return success_response(
            StockConsumptionRequestSerializer(consumption).data,
            message="POS stock consumption reversed." if reversed_now else "No active consumption to reverse.",
        )


@extend_schema(tags=["Admin · Inventory"], summary="List stock counts or begin a frozen stock-count snapshot")
class StockCountListCreateView(APIView):
    permission_classes = [HasCapability]; required_capability = "inventory.manage"
    def get(self, request):
        queryset = StockCount.objects.select_related("location", "initiated_by", "submitted_by", "approved_by").prefetch_related("lines__item", "lines__adjustment_movement").order_by("-created_at", "-pk")
        if status_value := request.query_params.get("status"):
            queryset = queryset.filter(status=status_value)
        if location_id := request.query_params.get("location_id"):
            queryset = queryset.filter(location_id=location_id)
        return _page(self, request, queryset, StockCountSerializer)
    def post(self, request):
        serializer = StockCountCreateSerializer(data=request.data); serializer.is_valid(raise_exception=True); data = serializer.validated_data
        location = StockLocation.objects.filter(pk=data["location_id"]).first()
        if location is None: raise NotFound("Stock location not found.")
        stock_count = start_stock_count(location=location, item_ids=data["item_ids"], actor=request.user, notes=data.get("notes", ""))
        stock_count = StockCount.objects.select_related("location", "initiated_by", "submitted_by", "approved_by").prefetch_related("lines__item", "lines__adjustment_movement").get(pk=stock_count.pk)
        log_action(actor=request.user, action="STOCK_COUNT_STARTED", instance=stock_count, request=request, metadata={"reference": stock_count.reference})
        return success_response(StockCountSerializer(stock_count).data, message="Stock-count snapshot started.", status=status.HTTP_201_CREATED)


class StockCountDetailView(APIView):
    permission_classes = [HasCapability]; required_capability = "inventory.manage"
    def get(self, request, reference):
        stock_count = StockCount.objects.select_related("location", "initiated_by", "submitted_by", "approved_by").prefetch_related("lines__item", "lines__adjustment_movement").filter(reference=reference).first()
        if stock_count is None: raise NotFound("Stock count not found.")
        return success_response(StockCountSerializer(stock_count).data)


class StockCountSubmitView(APIView):
    permission_classes = [HasCapability]; required_capability = "inventory.manage"
    def post(self, request, reference):
        serializer = StockCountSubmitSerializer(data=request.data); serializer.is_valid(raise_exception=True)
        stock_count = StockCount.objects.filter(reference=reference).first()
        if stock_count is None: raise NotFound("Stock count not found.")
        stock_count = submit_stock_count(stock_count=stock_count, counts=serializer.validated_data["lines"], actor=request.user)
        stock_count = StockCount.objects.select_related("location", "initiated_by", "submitted_by", "approved_by").prefetch_related("lines__item", "lines__adjustment_movement").get(pk=stock_count.pk)
        log_action(actor=request.user, action="STOCK_COUNT_SUBMITTED", instance=stock_count, request=request, metadata={"reference": stock_count.reference})
        message = "Stock count posted without adjustment." if stock_count.status == StockCount.Status.POSTED else "Stock count submitted for variance approval."
        return success_response(StockCountSerializer(stock_count).data, message=message)


class StockCountApproveView(APIView):
    permission_classes = [HasCapability]; required_capability = "inventory.adjust.approve"
    def post(self, request, reference):
        stock_count = StockCount.objects.filter(reference=reference).first()
        if stock_count is None: raise NotFound("Stock count not found.")
        stock_count = approve_stock_count(stock_count=stock_count, actor=request.user)
        stock_count = StockCount.objects.select_related("location", "initiated_by", "submitted_by", "approved_by").prefetch_related("lines__item", "lines__adjustment_movement").get(pk=stock_count.pk)
        log_action(actor=request.user, action="STOCK_COUNT_APPROVED_AND_POSTED", instance=stock_count, request=request, metadata={"reference": stock_count.reference})
        return success_response(StockCountSerializer(stock_count).data, message="Approved stock-count adjustments posted.")


@extend_schema(tags=["Admin · Inventory"], summary="Record a source-keyed controlled outgoing stock issue")
class StockIssueView(APIView):
    permission_classes = [HasCapability]; required_capability = "inventory.manage"
    def post(self, request):
        serializer=IssueStockSerializer(data=request.data); serializer.is_valid(raise_exception=True); data=serializer.validated_data
        item=StockItem.objects.filter(pk=data["item_id"]).first()
        if item is None: raise NotFound("Stock item not found.")
        location=StockLocation.objects.filter(pk=data["location_id"]).first()
        if location is None: raise NotFound("Stock location not found.")
        key=scoped_key(prefix="stock-issue", scope=f"staff:{request.user.pk}", raw_key=data.get("idempotency_key"))
        if not key: raise ValidationError({"idempotency_key": "An idempotency key is required for stock issue."})
        movement, created=issue_stock(item=item, location=location, quantity=data["quantity"], actor=request.user, source_key=key,
                                      movement_type=data["type"], source_reference=data.get("source_reference", ""), notes=data.get("notes", ""),
                                      department=data.get("department", ""), recipient=data.get("recipient", ""))
        if created:
            # The immutable movement already carries item, quantity, store,
            # department, recipient, issuing user and reason; the audit log adds
            # the request-level trail (who pressed the button, from where).
            log_action(
                actor=request.user, action="STOCK_ISSUED", instance=movement, request=request,
                summary=f"Issued {movement.quantity_delta} {item.base_unit} of {item.sku} to {movement.department or 'no department named'}",
                metadata={
                    "reference": movement.reference, "item": item.sku, "location": location.code,
                    "department": movement.department, "recipient": movement.recipient,
                    "quantity": str(data["quantity"]), "type": movement.type,
                },
            )
        return success_response(StockMovementSerializer(StockMovement.objects.select_related("item", "location", "actor").get(pk=movement.pk)).data,
                                message="Stock issue recorded." if created else "Existing stock issue returned.",
                                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


@extend_schema(tags=["Admin · Inventory"], summary="Record a source-keyed opening stock balance")
class StockOpeningView(APIView):
    permission_classes = [HasCapability]; required_capability = "inventory.manage"
    def post(self, request):
        serializer = OpeningStockSerializer(data=request.data); serializer.is_valid(raise_exception=True); data = serializer.validated_data
        item = StockItem.objects.filter(pk=data["item_id"]).first()
        if item is None: raise NotFound("Stock item not found.")
        location = StockLocation.objects.filter(pk=data["location_id"]).first()
        if location is None: raise NotFound("Stock location not found.")
        raw_key = data.get("idempotency_key") or f"opening:{item.pk}:{location.pk}:{data['quantity']}"
        key = scoped_key(prefix="stock-opening", scope=f"staff:{request.user.pk}", raw_key=raw_key)
        movement, created = record_opening_stock(
            item=item, location=location, quantity=data["quantity"], actor=request.user,
            source_key=key, unit_cost=data.get("unit_cost"), notes=data.get("notes", ""),
        )
        return success_response(
            StockMovementSerializer(StockMovement.objects.select_related("item", "location", "actor").get(pk=movement.pk)).data,
            message="Opening stock recorded." if created else "Existing opening stock returned.",
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


@extend_schema(tags=["Admin · Inventory"], summary="Record a source-keyed stock return into store")
class StockReturnView(APIView):
    permission_classes = [HasCapability]; required_capability = "inventory.manage"
    def post(self, request):
        serializer = ReturnStockSerializer(data=request.data); serializer.is_valid(raise_exception=True); data = serializer.validated_data
        item = StockItem.objects.filter(pk=data["item_id"]).first()
        if item is None: raise NotFound("Stock item not found.")
        location = StockLocation.objects.filter(pk=data["location_id"]).first()
        if location is None: raise NotFound("Stock location not found.")
        raw_key = data.get("idempotency_key") or f"return:{item.pk}:{location.pk}:{data['quantity']}"
        key = scoped_key(prefix="stock-return", scope=f"staff:{request.user.pk}", raw_key=raw_key)
        movement, created = record_stock_return(
            item=item, location=location, quantity=data["quantity"], actor=request.user,
            source_key=key, unit_cost=data.get("unit_cost"),
            source_reference=data.get("source_reference", ""), notes=data.get("notes", ""),
            direction=data.get("direction", "IN"),
            department=data.get("department", ""), recipient=data.get("recipient", ""),
        )
        return success_response(
            StockMovementSerializer(StockMovement.objects.select_related("item", "location", "actor").get(pk=movement.pk)).data,
            message="Stock return recorded." if created else "Existing stock return returned.",
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


@extend_schema(tags=["Admin · Inventory"], summary="Transfer stock between two active locations")
class StockTransferView(APIView):
    permission_classes = [HasCapability]; required_capability = "inventory.manage"
    def post(self, request):
        serializer = TransferStockSerializer(data=request.data); serializer.is_valid(raise_exception=True); data = serializer.validated_data
        item = StockItem.objects.filter(pk=data["item_id"]).first()
        if item is None: raise NotFound("Stock item not found.")
        from_loc = StockLocation.objects.filter(pk=data["from_location_id"]).first()
        to_loc = StockLocation.objects.filter(pk=data["to_location_id"]).first()
        if from_loc is None or to_loc is None: raise NotFound("Stock location not found.")
        raw_key = data.get("idempotency_key") or f"transfer:{item.pk}:{from_loc.pk}:{to_loc.pk}:{data['quantity']}"
        key = scoped_key(prefix="stock-transfer", scope=f"staff:{request.user.pk}", raw_key=raw_key)
        (outbound, inbound), created = transfer_stock(
            item=item, from_location=from_loc, to_location=to_loc, quantity=data["quantity"],
            actor=request.user, transfer_key=key, notes=data.get("notes", ""),
        )
        return success_response(
            {
                "outbound": StockMovementSerializer(StockMovement.objects.select_related("item", "location", "actor").get(pk=outbound.pk)).data,
                "inbound": StockMovementSerializer(StockMovement.objects.select_related("item", "location", "actor").get(pk=inbound.pk)).data,
            },
            message="Stock transfer recorded." if created else "Existing stock transfer returned.",
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


@extend_schema(tags=["Admin · Procurement"], summary="List or create suppliers")
class SupplierListCreateView(APIView):
    permission_classes = [HasCapability]; required_capability = "procurement.manage"
    def get(self, request):
        queryset=Supplier.objects.all().order_by("name")
        if status_value:=request.query_params.get("status"): queryset=queryset.filter(status=status_value)
        if search:=request.query_params.get("search"): queryset=queryset.filter(Q(name__icontains=search)|Q(email__icontains=search)|Q(phone__icontains=search))
        return _page(self, request, queryset, SupplierSerializer)
    def post(self, request):
        serializer=SupplierSerializer(data=request.data); serializer.is_valid(raise_exception=True)
        supplier=serializer.save(reference=_supplier_reference())
        log_action(actor=request.user, action="SUPPLIER_CREATED", instance=supplier, request=request)
        return success_response(SupplierSerializer(supplier).data, message="Supplier created.", status=status.HTTP_201_CREATED)


class SupplierDetailView(APIView):
    permission_classes = [HasCapability]; required_capability = "procurement.manage"

    def get(self, request, pk):
        supplier = Supplier.objects.filter(pk=pk).first()
        if supplier is None: raise NotFound("Supplier not found.")
        return success_response(SupplierSerializer(supplier).data)

    def patch(self, request, pk):
        supplier=Supplier.objects.filter(pk=pk).first()
        if supplier is None: raise NotFound("Supplier not found.")
        serializer=SupplierSerializer(supplier, data=request.data, partial=True); serializer.is_valid(raise_exception=True); supplier=serializer.save()
        return success_response(SupplierSerializer(supplier).data, message="Supplier updated.")


@extend_schema(tags=["Admin · Procurement"], summary="List or create purchase orders")
class PurchaseOrderListCreateView(APIView):
    permission_classes = [HasCapability]; required_capability = "procurement.manage"
    def get(self, request):
        queryset=PurchaseOrder.objects.select_related("supplier", "delivery_location", "requested_by", "approved_by").annotate(line_count=Count("lines")).order_by("-created_at", "-pk")
        if status_value:=request.query_params.get("status"): queryset=queryset.filter(status=status_value)
        if supplier_id:=request.query_params.get("supplier_id"): queryset=queryset.filter(supplier_id=supplier_id)
        return _page(self, request, queryset, PurchaseOrderListSerializer)
    def post(self, request):
        serializer=PurchaseOrderCreateSerializer(data=request.data); serializer.is_valid(raise_exception=True); data=serializer.validated_data
        supplier=Supplier.objects.filter(pk=data["supplier_id"]).first()
        if supplier is None: raise NotFound("Supplier not found.")
        location=StockLocation.objects.filter(pk=data["delivery_location_id"]).first()
        if location is None: raise NotFound("Stock location not found.")
        key=scoped_key(prefix="purchase-order", scope=f"staff:{request.user.pk}", raw_key=data.get("idempotency_key"))
        po, created=create_purchase_order(supplier=supplier, delivery_location=location, lines=data["lines"], requested_by=request.user,
                                          expected_on=data.get("expected_on"), notes=data.get("notes", ""), supplier_reference=data.get("supplier_reference", ""), idempotency_key=key)
        if created: log_action(actor=request.user, action="PURCHASE_ORDER_CREATED", instance=po, request=request, metadata={"reference": po.reference})
        return success_response(PurchaseOrderDetailSerializer(_po_or_404(po.reference, detail=True)).data,
                                message="Purchase order created." if created else "Existing purchase order returned.", status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


@extend_schema(tags=["Admin · Procurement"], summary="Read or edit a purchase order and its lines")
class PurchaseOrderDetailView(APIView):
    permission_classes = [HasCapability]; required_capability = "procurement.manage"

    def get(self, request, reference):
        return success_response(PurchaseOrderDetailSerializer(_po_or_404(reference, detail=True)).data)

    def patch(self, request, reference):
        """Edit an order only while it is still DRAFT or SUBMITTED.

        The service enforces the APPROVED boundary; the dashboard removes the
        Edit action at the same point so the two can never disagree.
        """
        po = _po_or_404(reference)
        serializer = PurchaseOrderUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)

        supplier = None
        if "supplier_id" in data:
            supplier = Supplier.objects.filter(pk=data.pop("supplier_id")).first()
            if supplier is None:
                raise NotFound("Supplier not found.")
        location = None
        if "delivery_location_id" in data:
            location = StockLocation.objects.filter(pk=data.pop("delivery_location_id")).first()
            if location is None:
                raise NotFound("Stock location not found.")
        lines = data.pop("lines", None)
        changes = dict(data)
        if supplier is not None:
            changes["supplier"] = supplier
        if location is not None:
            changes["delivery_location"] = location

        po = update_purchase_order(purchase_order=po, actor=request.user, changes=changes, lines=lines)
        log_action(
            actor=request.user, action="PURCHASE_ORDER_UPDATED", instance=po, request=request,
            metadata={"reference": po.reference, "status": po.status, "lines_replaced": lines is not None},
        )
        return success_response(
            PurchaseOrderDetailSerializer(_po_or_404(po.reference, detail=True)).data,
            message="Purchase order updated.",
        )


class PurchaseOrderSubmitView(APIView):
    permission_classes = [HasCapability]; required_capability = "procurement.manage"
    def post(self, request, reference):
        po=submit_purchase_order(purchase_order=_po_or_404(reference), actor=request.user)
        return success_response(PurchaseOrderDetailSerializer(_po_or_404(po.reference, detail=True)).data, message="Purchase order submitted.")


class PurchaseOrderApproveView(APIView):
    permission_classes = [HasCapability]; required_capability = "procurement.approve"
    # Approval accepts the optional invoice/payment receipt in the same action.
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def post(self, request, reference):
        po = approve_purchase_order(
            purchase_order=_po_or_404(reference), actor=request.user,
            receipt_file=request.FILES.get("approval_receipt"),
            payment_reference=(request.data.get("payment_reference") or "").strip()[:160],
        )
        log_action(
            actor=request.user, action="PURCHASE_ORDER_APPROVED", instance=po, request=request,
            metadata={
                "reference": po.reference,
                "payment_reference": po.payment_reference,
                "has_receipt": bool(po.approval_receipt),
            },
        )
        return success_response(PurchaseOrderDetailSerializer(_po_or_404(po.reference, detail=True)).data, message="Purchase order approved.")


class PurchaseOrderReceiptUploadView(APIView):
    """Attach or replace the supplier invoice/payment receipt after approval."""

    permission_classes = [HasCapability]; required_capability = "procurement.approve"
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, reference):
        upload = request.FILES.get("approval_receipt") or request.FILES.get("receipt")
        payment_reference = request.data.get("payment_reference")
        if upload is None and payment_reference is None:
            raise ValidationError({"approval_receipt": "Attach a receipt/invoice file or enter a payment reference."})
        po = attach_purchase_order_receipt(
            purchase_order=_po_or_404(reference), actor=request.user,
            upload=upload, payment_reference=payment_reference,
        )
        log_action(
            actor=request.user, action="PURCHASE_ORDER_RECEIPT_ATTACHED", instance=po, request=request,
            metadata={"reference": po.reference, "has_receipt": bool(po.approval_receipt)},
        )
        return success_response(
            PurchaseOrderDetailSerializer(_po_or_404(po.reference, detail=True)).data,
            message="Purchase-order receipt saved.",
        )


class PurchaseOrderOrderedView(APIView):
    permission_classes = [HasCapability]; required_capability = "procurement.manage"
    def post(self, request, reference):
        po=mark_purchase_order_ordered(purchase_order=_po_or_404(reference), actor=request.user)
        return success_response(PurchaseOrderDetailSerializer(_po_or_404(po.reference, detail=True)).data, message="Purchase order marked ordered.")


@extend_schema(
    tags=["Admin · Procurement"],
    summary="Record money paid to the supplier, with receipt or invoice evidence",
)
class PurchaseOrderPayView(APIView):
    """Append a real payment to an approved order.

    The order's approval receipt only proves what was filed at
    approval; this endpoint is the money-out itself — amount, how it
    was paid, the bank/cheque reference, the date, and the supplier
    invoice or payment slip as evidence. The order moves to PAID
    once the payments cover its total.
    """

    permission_classes = [HasCapability]; required_capability = "procurement.approve"
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def post(self, request, reference):
        serializer = PurchaseOrderPaymentInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        po = _po_or_404(reference)
        payment = record_purchase_order_payment(
            purchase_order=po, actor=request.user,
            method=data["method"],
            amount=data.get("amount"),
            reference=data.get("reference", ""),
            paid_on=data.get("paid_on"),
            evidence_file=request.FILES.get("evidence_file") or request.FILES.get("receipt"),
            notes=data.get("notes", ""),
        )
        po = _po_or_404(po.reference, detail=True)
        log_action(
            actor=request.user, action="SUPPLIER_PAYMENT_RECORDED", instance=po, request=request,
            metadata={
                "reference": po.reference, "payment_id": payment.pk,
                "amount": str(payment.amount), "method": payment.method,
                "payment_reference": payment.reference,
                "has_evidence": bool(payment.evidence_file),
            },
        )
        return success_response(
            PurchaseOrderDetailSerializer(po).data,
            message="Supplier payment recorded."
            if po.status != PurchaseOrder.Status.PAID
            else "Supplier payment recorded; the purchase order is fully paid.",
        )


class GoodsReceiptCreateView(APIView):
    permission_classes = [HasCapability]; required_capability = "procurement.manage"
    def post(self, request, reference):
        serializer=GoodsReceiptCreateSerializer(data=request.data); serializer.is_valid(raise_exception=True); data=serializer.validated_data
        po=_po_or_404(reference)
        key=scoped_key(prefix="goods-receipt", scope=f"staff:{request.user.pk}:{po.reference}", raw_key=data["idempotency_key"])
        receipt, created=receive_goods(purchase_order=po, receipt_lines=data["lines"], actor=request.user, source_key=key,
                                       received_on=data.get("received_on"), supplier_delivery_reference=data.get("supplier_delivery_reference", ""), notes=data.get("notes", ""))
        receipt=GoodsReceipt.objects.select_related("location", "supplier").prefetch_related("lines__item", "lines__stock_movement").get(pk=receipt.pk)
        if created: log_action(actor=request.user, action="GOODS_RECEIPT_POSTED", instance=receipt, request=request, metadata={"reference": receipt.reference, "purchase_order": po.reference})
        return success_response(GoodsReceiptSerializer(receipt).data, message="Goods receipt posted." if created else "Existing goods receipt returned.", status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


def _bool_param(request, name):
    """Tri-state boolean query parameter: absent -> None, "true"/"false" -> bool."""
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return None
    if raw.lower() in {"true", "1", "yes"}:
        return True
    if raw.lower() in {"false", "0", "no"}:
        return False
    raise ValidationError({name: "Use true or false."})


def _decimal_param(request, name):
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, TypeError) as exc:
        raise ValidationError({name: "Use a decimal number."}) from exc


@extend_schema(
    tags=["Admin · Inventory"],
    summary="Product profitability and stock valuation projection",
    description=(
        "A read-only, clearly-labelled forward projection built from the immutable "
        "stock ledger, the goods-receipt cost history and the active recipes. It "
        "posts nothing and never alters stored amounts. Fields that cannot be "
        "derived (no cost basis, or no active recipe) are returned as null."
    ),
)
class ProductProfitabilityView(APIView):
    """Searchable, sortable, paginated product profitability/valuation projection."""

    permission_classes = [HasCapability]
    required_capabilities = ("inventory.manage", "reports.financial.view")
    require_any_capability = True

    def get(self, request):
        sort = request.query_params.get("sort") or "sku"
        if sort not in SORT_KEYS:
            raise ValidationError({"sort": f"Use one of: {', '.join(SORT_KEYS)}."})
        direction = (request.query_params.get("direction") or "asc").lower()
        if direction not in {"asc", "desc"}:
            raise ValidationError({"direction": "Use asc or desc."})

        location_id = request.query_params.get("location_id") or None
        if location_id is not None:
            try:
                location_id = int(location_id)
            except (TypeError, ValueError) as exc:
                raise ValidationError({"location_id": "Use a numeric location id."}) from exc
        supplier_id = request.query_params.get("supplier_id") or None
        if supplier_id is not None:
            try:
                supplier_id = int(supplier_id)
            except (TypeError, ValueError) as exc:
                raise ValidationError({"supplier_id": "Use a numeric supplier id."}) from exc

        rows = product_profitability_projection(
            search=(request.query_params.get("search") or "").strip(),
            location_id=location_id,
            department=(request.query_params.get("department") or "").strip(),
            supplier_id=supplier_id,
            category=(request.query_params.get("category") or "").strip(),
            low_stock_only=bool(_bool_param(request, "low_stock")),
            sellable=_bool_param(request, "sellable"),
            min_margin=_decimal_param(request, "min_margin"),
        )
        rows = sort_projection(rows, sort=sort, direction=direction)
        summary = projection_summary(rows)

        paginator = StandardPagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        response = paginator.get_paginated_response(ProductProfitabilityRowSerializer(page, many=True).data)
        # The projection is a projection: say so in the payload so a consumer can
        # never mistake the figures for posted accounting balances.
        response.data["summary"] = ProductProfitabilitySummarySerializer(summary).data
        response.data["projection"] = summary["projection"]
        return response


@extend_schema(tags=["Admin · Inventory"], summary="Filter options for the profitability projection")
class ProductProfitabilityFiltersView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("inventory.manage", "reports.financial.view")
    require_any_capability = True

    def get(self, request):
        return success_response(projection_filter_choices())
