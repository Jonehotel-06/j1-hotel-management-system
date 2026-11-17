"""Capability-gated Mini Mart catalogue management.

Products remain OneToOne extensions of the existing ``StockItem``. Reception
checkout, POS order evidence, stock movements and finance posting continue to
use their established services; this module only creates/edits the master data
that those paths already consume.
"""
from __future__ import annotations

import secrets
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Q
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.views import APIView

from apps.accounts.capabilities import has_capability
from apps.core.pagination import StandardPagination
from apps.core.permissions import HasCapability
from apps.core.responses import success_response
from apps.inventory.models import StockBalance, StockItem, StockLocation, StockUnit
from apps.inventory.services.inventory_service import (
    assert_can_record_stock,
    canonical_unit,
    record_opening_stock,
    resolve_entered_quantity,
    unit_label,
)

from .models import MiniMartCategory, MiniMartProduct


class MiniMartCategorySerializer(serializers.ModelSerializer):
    product_count = serializers.SerializerMethodField()

    class Meta:
        model = MiniMartCategory
        fields = ["id", "name", "description", "is_active", "product_count", "created_at", "updated_at"]
        read_only_fields = ["id", "product_count", "created_at", "updated_at"]

    def get_product_count(self, obj):
        # The list view annotates ``product_total`` in ONE aggregate query.
        # Falling back to ``.count()`` only runs for a single-object read
        # (create/update response), so a 200-category page is 1 query, not 201.
        annotated = getattr(obj, "product_total", None)
        if annotated is not None:
            return annotated
        return obj.minimart_products.count()

    def validate_name(self, value):
        name = str(value or "").strip()
        if not name:
            raise serializers.ValidationError("Enter a category name.")
        clash = (
            MiniMartCategory.objects.filter(name__iexact=name)
            .exclude(pk=getattr(self.instance, "pk", None))
            .first()
        )
        if clash is not None:
            raise serializers.ValidationError(
                f"{clash.name} already exists. Edit that category instead of creating a duplicate."
            )
        return name


class MiniMartProductWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255, required=False)
    sku = serializers.CharField(max_length=80, required=False)
    barcode = serializers.CharField(max_length=80, required=False, allow_blank=True, allow_null=True)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    category_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    purchase_cost = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.00"), required=False)
    selling_price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"), required=False)
    base_unit = serializers.CharField(max_length=30, required=False)
    opening_quantity = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=Decimal("0.0001"), required=False)
    opening_unit = serializers.CharField(max_length=30, required=False, allow_blank=True)
    opening_conversion_factor = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=Decimal("0.0001"), required=False, allow_null=True)
    reorder_level = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=Decimal("0.0000"), required=False)
    location_id = serializers.IntegerField(min_value=1, required=False)
    is_active = serializers.BooleanField(required=False, default=True)
    idempotency_key = serializers.CharField(max_length=128, required=False, allow_blank=True, default="")

    def validate(self, attrs):
        product = self.context.get("product")
        creating = product is None
        if creating:
            for field in ("name", "sku", "purchase_cost", "selling_price", "base_unit", "location_id"):
                if attrs.get(field) in (None, ""):
                    raise serializers.ValidationError({field: "This field is required for a new product."})
        if "opening_quantity" in attrs and not creating:
            raise serializers.ValidationError({"opening_quantity": "Use inventory replenishment to add stock to an existing product."})
        if "sku" in attrs:
            clash = StockItem.objects.filter(sku__iexact=str(attrs["sku"]).strip()).exclude(
                pk=getattr(getattr(product, "item", None), "pk", None)
            ).first()
            if clash is not None:
                raise serializers.ValidationError({"sku": f"SKU {clash.sku} already belongs to {clash.name}. Search and edit that product instead."})
        barcode = str(attrs.get("barcode") or "").strip()
        if barcode:
            clash = StockItem.objects.filter(barcode__iexact=barcode).exclude(
                pk=getattr(getattr(product, "item", None), "pk", None)
            ).first()
            if clash is not None:
                raise serializers.ValidationError({"barcode": f"Barcode {barcode} is already recorded on {clash.sku}."})
        if "base_unit" in attrs:
            token = str(attrs["base_unit"]).strip().upper()
            if not StockUnit.objects.filter(code__iexact=token, is_active=True).exists():
                raise serializers.ValidationError({"base_unit": "Configure this measurement unit in Inventory first."})
            if product and product.item.movements.exists() and canonical_unit(token) != canonical_unit(product.item.base_unit):
                raise serializers.ValidationError({"base_unit": "This product has stock history; its counted unit cannot be changed."})
            attrs["base_unit"] = token
        if "location_id" in attrs:
            if not StockLocation.objects.filter(pk=attrs["location_id"], is_active=True).exists():
                raise serializers.ValidationError({"location_id": "Choose an active configured stock location."})
            if product and attrs["location_id"] != product.location_id and product.item.movements.exists():
                raise serializers.ValidationError({
                    "location_id": "This product has stock history. Use the inventory transfer workflow to move its balance; do not silently re-point the sale to another store."
                })
        if "category_id" in attrs and attrs["category_id"]:
            category = MiniMartCategory.objects.filter(pk=attrs["category_id"], is_active=True).first()
            if category is None:
                raise serializers.ValidationError({"category_id": "Choose an active Mini Mart category."})
        opening_unit = str(attrs.get("opening_unit") or "").strip()
        base_unit = attrs.get("base_unit") or getattr(getattr(product, "item", None), "base_unit", "")
        if opening_unit and canonical_unit(opening_unit) != canonical_unit(base_unit) and not attrs.get("opening_conversion_factor"):
            raise serializers.ValidationError({
                "opening_conversion_factor": "Enter the explicit number of base units in this counting unit; conversions are never assumed."
            })
        return attrs


#: Sentinel distinguishing "the caller resolved this product's balance and it
#: does not exist" from "the caller did not resolve it, look it up".
_BALANCE_UNRESOLVED = object()


def product_payload(product, balance=_BALANCE_UNRESOLVED):
    """Serialise one Mini Mart product.

    ``balance`` may be supplied by a caller that already fetched every balance
    for the page. A caller that knows a balance row does not exist passes
    ``None``; ``_BALANCE_UNRESOLVED`` means "query it". This keeps the product
    list at a fixed number of queries instead of one lookup per row — the N+1
    that made the catalogue slow on a real-size product list.
    """
    item = product.item
    location = product.location
    if balance is _BALANCE_UNRESOLVED:
        balance = StockBalance.objects.filter(item=item, location=location).first()
    quantity = balance.quantity_on_hand if balance else Decimal("0.0000")
    category_name = product.category.name if product.category_id else item.category
    return {
        "id": product.pk,
        "stock_item_id": item.pk,
        "name": item.name,
        "sku": item.sku,
        "barcode": item.barcode or "",
        "description": product.description,
        "category_id": product.category_id,
        "category": category_name,
        "category_name": category_name,
        "purchase_cost": str(item.standard_cost),
        "selling_price": str(product.selling_price),
        "base_unit": item.base_unit,
        "unit": item.base_unit,
        "base_unit_label": unit_label(item.base_unit),
        "opening_stock": str(quantity),
        "stock_quantity": str(quantity),
        "reorder_level": str(item.reorder_level),
        "location_id": location.pk,
        "location_name": location.name,
        "location": location.name,
        "is_active": product.is_active,
        "item_active": item.is_active,
        "status": "ACTIVE" if product.is_active and item.is_active else "INACTIVE",
        "low_stock": quantity <= item.reorder_level,
        "out_of_stock": quantity < 1,
        "created_at": product.created_at,
        "updated_at": product.updated_at,
    }


def _positive_int_param(request, name):
    raw = str(request.query_params.get(name) or "").strip()
    if not raw:
        return None
    if not raw.isdigit() or int(raw) < 1:
        raise ValidationError({name: "Use a positive numeric id."})
    return int(raw)


def _page(view, request, queryset, rows):
    paginator = StandardPagination()
    page = paginator.paginate_queryset(queryset, request, view=view)
    return paginator.get_paginated_response([row for row in rows(page)])


def _require_stock_recording(request):
    if not has_capability(request.user, "inventory.record"):
        assert_can_record_stock(request.user, action="manage the Mini Mart catalogue")


class _CatalogueView(APIView):
    permission_classes = [HasCapability]
    required_capability = "inventory.manage"


class MiniMartCategoryListCreateView(_CatalogueView):
    def get(self, request):
        queryset = (
            MiniMartCategory.objects.annotate(product_total=Count("minimart_products"))
            .order_by("name")
        )
        if request.query_params.get("active") == "true":
            queryset = queryset.filter(is_active=True)
        if search := (request.query_params.get("search") or "").strip():
            queryset = queryset.filter(Q(name__icontains=search) | Q(description__icontains=search))
        return _page(self, request, queryset, lambda rows: MiniMartCategorySerializer(rows, many=True).data)

    def post(self, request):
        _require_stock_recording(request)
        serializer = MiniMartCategorySerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        category = serializer.save()
        return success_response(
            MiniMartCategorySerializer(category).data,
            message="Mini Mart category created.",
            status=status.HTTP_201_CREATED,
        )


class MiniMartCategoryDetailView(_CatalogueView):
    def patch(self, request, pk):
        _require_stock_recording(request)
        category = MiniMartCategory.objects.filter(pk=pk).first()
        if category is None:
            raise NotFound("Mini Mart category not found.")
        serializer = MiniMartCategorySerializer(category, data=request.data or {}, partial=True)
        serializer.is_valid(raise_exception=True)
        category = serializer.save()
        return success_response(MiniMartCategorySerializer(category).data, message="Mini Mart category updated.")


def _product_rows(products):
    """Serialise a page of products with ONE balance query for the whole page."""
    balances = {}
    pairs = {(p.item_id, p.location_id) for p in products}
    if pairs:
        item_ids = {item_id for item_id, _ in pairs}
        location_ids = {location_id for _, location_id in pairs}
        for row in StockBalance.objects.filter(
            item_id__in=item_ids, location_id__in=location_ids
        ).only("item_id", "location_id", "quantity_on_hand"):
            balances[(row.item_id, row.location_id)] = row
    return [product_payload(p, balances.get((p.item_id, p.location_id))) for p in products]


class MiniMartProductListCreateView(_CatalogueView):
    def get(self, request):
        queryset = MiniMartProduct.objects.select_related("item", "location", "category").all().order_by("item__name", "pk")
        if request.query_params.get("active") == "true":
            queryset = queryset.filter(is_active=True, item__is_active=True, location__is_active=True)
        category_id = _positive_int_param(request, "category_id")
        if category_id:
            queryset = queryset.filter(category_id=category_id)
        if search := (request.query_params.get("search") or "").strip():
            queryset = queryset.filter(
                Q(item__name__icontains=search) | Q(item__sku__icontains=search)
                | Q(item__barcode__iexact=search) | Q(category__name__icontains=search)
                | Q(description__icontains=search)
            )
        return _page(self, request, queryset, lambda rows: _product_rows(rows))

    @transaction.atomic
    def post(self, request):
        _require_stock_recording(request)
        serializer = MiniMartProductWriteSerializer(data=request.data or {}, context={"product": None})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        category = MiniMartCategory.objects.filter(pk=data["category_id"]).first() if data.get("category_id") else None
        location = StockLocation.objects.get(pk=data["location_id"])
        item = StockItem.objects.create(
            sku=str(data["sku"]).strip(),
            name=str(data["name"]).strip(),
            category=category.name if category else "",
            barcode=str(data.get("barcode") or "").strip() or None,
            item_type=StockItem.ItemType.SELLABLE_PRODUCT,
            base_unit=data["base_unit"],
            standard_cost=data["purchase_cost"],
            reorder_level=data.get("reorder_level", Decimal("0.0000")),
            currency="NGN",
            notes=str(data.get("description") or "")[:500],
            is_active=True,
        )
        product = MiniMartProduct.objects.create(
            item=item,
            location=location,
            category=category,
            description=str(data.get("description") or ""),
            selling_price=data["selling_price"],
            is_active=data.get("is_active", True),
        )
        if data.get("opening_quantity") is not None:
            quantity, _evidence = resolve_entered_quantity(
                data["opening_quantity"],
                item=item,
                unit=data.get("opening_unit") or item.base_unit,
                conversion_factor=data.get("opening_conversion_factor"),
                allow_declared_pack_size=False,
            )
            record_opening_stock(
                item=item,
                location=location,
                quantity=quantity,
                actor=request.user,
                unit_cost=data["purchase_cost"],
                source_key=f"minimart-opening:{item.pk}:{location.pk}:{data.get('idempotency_key') or secrets.token_hex(12)}",
                notes="Mini Mart product opening stock",
            )
        return success_response(
            product_payload(product),
            message="Mini Mart product created.",
            status=status.HTTP_201_CREATED,
        )


class MiniMartProductDetailView(_CatalogueView):
    @transaction.atomic
    def patch(self, request, pk):
        _require_stock_recording(request)
        product = MiniMartProduct.objects.select_related("item", "location", "category").filter(pk=pk).first()
        if product is None:
            raise NotFound("Mini Mart product not found.")
        serializer = MiniMartProductWriteSerializer(
            product, data=request.data or {}, partial=True, context={"product": product}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        item = product.item
        if "sku" in data:
            item.sku = str(data["sku"]).strip()
        if "name" in data:
            item.name = str(data["name"]).strip()
        if "barcode" in data:
            item.barcode = str(data.get("barcode") or "").strip() or None
        if "description" in data:
            item.notes = str(data.get("description") or "")[:500]
            product.description = str(data.get("description") or "")
        if "purchase_cost" in data:
            item.standard_cost = data["purchase_cost"]
        if "reorder_level" in data:
            item.reorder_level = data["reorder_level"]
        if "base_unit" in data:
            item.base_unit = data["base_unit"]
        if "category_id" in data:
            category = MiniMartCategory.objects.filter(pk=data["category_id"]).first() if data["category_id"] else None
            product.category = category
            item.category = category.name if category else ""
        if "location_id" in data:
            product.location = StockLocation.objects.get(pk=data["location_id"])
        if "selling_price" in data:
            product.selling_price = data["selling_price"]
        if "is_active" in data:
            product.is_active = data["is_active"]
        item.save()
        product.save()
        return success_response(product_payload(product), message="Mini Mart product updated.")
