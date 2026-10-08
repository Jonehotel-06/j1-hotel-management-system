"""Bounded capability-protected inventory and procurement operations API."""
import secrets

from django.db.models import Count, F, Q
from django.utils.dateparse import parse_date

from apps.core.utils import hotel_date_bounds
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.views import APIView

from apps.audit.services import log_action
from apps.core.pagination import StandardPagination
from apps.core.permissions import HasCapability
from apps.core.responses import success_response

from .models import GoodsReceipt, PurchaseOrder, StockBalance, StockConsumptionRequest, StockCount, StockItem, StockLocation, StockMovement, StockRecipe, Supplier
from .serializers import (
    GoodsReceiptCreateSerializer, GoodsReceiptSerializer, IssueStockSerializer, PurchaseOrderCreateSerializer,
    PurchaseOrderDetailSerializer, PurchaseOrderListSerializer, StockBalanceSerializer, StockConsumptionRequestSerializer,
    StockCountCreateSerializer, StockCountSerializer, StockCountSubmitSerializer, StockItemSerializer,
    InventoryMenuItemDirectorySerializer,
    StockLocationSerializer, StockMovementSerializer, StockRecipeCreateSerializer, StockRecipeSerializer, SupplierSerializer,
)
from .services.inventory_service import (
    approve_purchase_order, approve_stock_count, create_purchase_order, create_stock_recipe, issue_stock, mark_purchase_order_ordered,
    process_pos_stock_consumption, receive_goods, retire_stock_recipe, scoped_key, start_stock_count, submit_purchase_order, submit_stock_count,
)


def _page(view, request, queryset, serializer):
    paginator = StandardPagination(); page = paginator.paginate_queryset(queryset, request, view=view)
    return paginator.get_paginated_response(serializer(page, many=True).data)


def _supplier_reference():
    return f"SUP-{secrets.token_hex(6).upper()}"


def _po_or_404(reference, *, detail=False):
    queryset = PurchaseOrder.objects.select_related("supplier", "delivery_location", "requested_by", "approved_by").annotate(line_count=Count("lines"))
    if detail: queryset = queryset.prefetch_related("lines")
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
        serializer=StockItemSerializer(data=request.data); serializer.is_valid(raise_exception=True); item=serializer.save()
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
        serializer=StockItemSerializer(item, data=request.data, partial=True); serializer.is_valid(raise_exception=True); item=serializer.save()
        return success_response(StockItemSerializer(item).data, message="Stock item updated.")


@extend_schema(tags=["Admin · Inventory"], summary="List current locked stock-balance projections")
class StockBalanceListView(APIView):
    permission_classes = [HasCapability]; required_capability = "inventory.manage"
    def get(self, request):
        queryset=StockBalance.objects.select_related("item", "location").order_by("item__sku", "location__code")
        if item_id:=request.query_params.get("item_id"): queryset=queryset.filter(item_id=item_id)
        if location_id:=request.query_params.get("location_id"): queryset=queryset.filter(location_id=location_id)
        if request.query_params.get("reorder") == "true": queryset=queryset.filter(quantity_on_hand__lte=F("item__reorder_level"))
        return _page(self, request, queryset, StockBalanceSerializer)


@extend_schema(tags=["Admin · Inventory"], summary="List immutable stock movement ledger")
class StockMovementListView(APIView):
    permission_classes = [HasCapability]; required_capability = "inventory.manage"
    def get(self, request):
        queryset=StockMovement.objects.select_related("item", "location", "actor").order_by("-occurred_at", "-pk")
        if item_id:=request.query_params.get("item_id"): queryset=queryset.filter(item_id=item_id)
        if location_id:=request.query_params.get("location_id"): queryset=queryset.filter(location_id=location_id)
        if movement_type:=request.query_params.get("type"): queryset=queryset.filter(type=movement_type)
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
                                      movement_type=data["type"], source_reference=data.get("source_reference", ""), notes=data.get("notes", ""))
        return success_response(StockMovementSerializer(StockMovement.objects.select_related("item", "location", "actor").get(pk=movement.pk)).data,
                                message="Stock issue recorded." if created else "Existing stock issue returned.",
                                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


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


@extend_schema(tags=["Admin · Procurement"], summary="Read a purchase order and snapshot lines")
class PurchaseOrderDetailView(APIView):
    permission_classes = [HasCapability]; required_capability = "procurement.manage"
    def get(self, request, reference): return success_response(PurchaseOrderDetailSerializer(_po_or_404(reference, detail=True)).data)


class PurchaseOrderSubmitView(APIView):
    permission_classes = [HasCapability]; required_capability = "procurement.manage"
    def post(self, request, reference):
        po=submit_purchase_order(purchase_order=_po_or_404(reference), actor=request.user)
        return success_response(PurchaseOrderDetailSerializer(_po_or_404(po.reference, detail=True)).data, message="Purchase order submitted.")


class PurchaseOrderApproveView(APIView):
    permission_classes = [HasCapability]; required_capability = "procurement.approve"
    def post(self, request, reference):
        po=approve_purchase_order(purchase_order=_po_or_404(reference), actor=request.user)
        log_action(actor=request.user, action="PURCHASE_ORDER_APPROVED", instance=po, request=request, metadata={"reference": po.reference})
        return success_response(PurchaseOrderDetailSerializer(_po_or_404(po.reference, detail=True)).data, message="Purchase order approved.")


class PurchaseOrderOrderedView(APIView):
    permission_classes = [HasCapability]; required_capability = "procurement.manage"
    def post(self, request, reference):
        po=mark_purchase_order_ordered(purchase_order=_po_or_404(reference), actor=request.user)
        return success_response(PurchaseOrderDetailSerializer(_po_or_404(po.reference, detail=True)).data, message="Purchase order marked ordered.")


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
