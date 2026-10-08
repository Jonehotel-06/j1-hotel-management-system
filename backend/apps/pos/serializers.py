"""Explicit POS API representations; money is always server-derived."""
from decimal import Decimal

from rest_framework import serializers

from apps.stays.models import Stay

from .models import KitchenTicket, MenuCategory, MenuItem, MenuModifier, PosOrder, PosOrderEvent, PosOrderLine, PosTender


class MenuModifierSerializer(serializers.ModelSerializer):
    class Meta:
        model = MenuModifier
        fields = ["id", "name", "price_delta", "is_active", "sort_order"]
        read_only_fields = fields


class MenuItemSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)
    modifiers = MenuModifierSerializer(many=True, read_only=True)

    class Meta:
        model = MenuItem
        fields = [
            "id", "category", "category_name", "name", "sku", "description", "base_price", "currency",
            "tax_rate_percent", "preparation_minutes", "is_available", "is_active", "sort_order", "modifiers",
        ]
        read_only_fields = fields


class MenuCategoryMenuSerializer(serializers.ModelSerializer):
    items = serializers.SerializerMethodField()

    class Meta:
        model = MenuCategory
        fields = ["id", "name", "slug", "description", "sort_order", "items"]
        read_only_fields = fields

    def get_items(self, obj):
        # View prefetches filtered available items into this attr; the fallback
        # makes the serializer safe in admin/shell use without N+1 in hot APIs.
        items = getattr(obj, "_available_items", None)
        if items is None:
            items = obj.items.filter(is_active=True, is_available=True).prefetch_related("modifiers")
        return MenuItemSerializer(items, many=True).data


class PosRoomServiceStaySerializer(serializers.ModelSerializer):
    """Small, bounded in-house guest picker for POS room-service entry.

    The browser receives only the operational identifiers it needs to create a
    room-service order. It never guesses a database ``stay_id`` from a booking
    reference, and it does not expose a general unbounded stay directory.
    """

    booking_reference = serializers.CharField(source="booking.booking_reference", read_only=True)
    guest_name = serializers.CharField(source="guest.full_name", read_only=True)
    guest_email = serializers.CharField(source="guest.email", read_only=True)
    room_numbers = serializers.SerializerMethodField()

    class Meta:
        model = Stay
        fields = [
            "id", "reference", "booking_reference", "guest_name", "guest_email",
            "room_numbers", "expected_departure",
        ]
        read_only_fields = fields

    def get_room_numbers(self, obj):
        rooms = getattr(obj, "_pos_active_rooms", None)
        if rooms is None:
            rooms = obj.stay_rooms.filter(released_at__isnull=True).select_related("room")
        return [assignment.room.room_number for assignment in rooms]


class PosOrderLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = PosOrderLine
        fields = [
            "id", "menu_item", "item_name", "item_sku", "unit_price", "quantity", "modifier_total",
            "modifiers_snapshot", "line_total", "notes", "created_at",
        ]
        read_only_fields = fields


class PosTenderSerializer(serializers.ModelSerializer):
    captured_by_email = serializers.CharField(source="captured_by.email", read_only=True)
    cash_session_reference = serializers.CharField(source="cash_session.reference", read_only=True)
    financial_reference = serializers.CharField(source="collection_transaction.reference", read_only=True)

    class Meta:
        model = PosTender
        fields = [
            "id", "reference", "method", "status", "amount", "currency", "external_reference",
            "cash_session_reference", "captured_at", "captured_by_email", "financial_reference", "notes",
        ]
        read_only_fields = fields


class PosOrderEventSerializer(serializers.ModelSerializer):
    actor_email = serializers.CharField(source="actor.email", read_only=True)

    class Meta:
        model = PosOrderEvent
        fields = ["id", "type", "occurred_at", "actor_email", "details"]
        read_only_fields = fields


class KitchenTicketSerializer(serializers.ModelSerializer):
    assigned_to_email = serializers.CharField(source="assigned_to.email", read_only=True)
    order_reference = serializers.CharField(source="order.reference", read_only=True)
    order_guest_name = serializers.CharField(source="order.guest_name", read_only=True)
    order_mode = serializers.CharField(source="order.mode", read_only=True)
    order_delivery_location = serializers.CharField(source="order.delivery_location", read_only=True)
    order_table_number = serializers.CharField(source="order.table_number", read_only=True)

    class Meta:
        model = KitchenTicket
        fields = [
            "id", "status", "priority", "queued_at", "started_at", "ready_at", "completed_at",
            "assigned_to_email", "notes", "order_reference", "order_guest_name", "order_mode",
            "order_delivery_location", "order_table_number",
        ]
        read_only_fields = fields


class PosOrderListSerializer(serializers.ModelSerializer):
    stay_reference = serializers.CharField(source="stay.reference", read_only=True)
    folio_reference = serializers.CharField(source="folio.reference", read_only=True)
    created_by_email = serializers.CharField(source="created_by.email", read_only=True)
    line_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = PosOrder
        fields = [
            "id", "reference", "mode", "status", "settlement_status", "stay_reference", "folio_reference",
            "guest_name", "table_number", "delivery_location", "currency", "subtotal", "tax_amount",
            "total_amount", "submitted_at", "delivered_at", "created_at", "created_by_email", "line_count",
        ]
        read_only_fields = fields


class PosOrderDetailSerializer(PosOrderListSerializer):
    lines = PosOrderLineSerializer(many=True, read_only=True)
    tenders = PosTenderSerializer(many=True, read_only=True)
    events = PosOrderEventSerializer(many=True, read_only=True)
    kitchen_ticket = KitchenTicketSerializer(read_only=True)
    charge_financial_reference = serializers.CharField(source="charge_transaction.reference", read_only=True)

    class Meta(PosOrderListSerializer.Meta):
        fields = PosOrderListSerializer.Meta.fields + [
            "notes", "cancelled_at", "charge_financial_reference", "lines", "tenders", "events", "kitchen_ticket",
        ]


class PosOrderLineInputSerializer(serializers.Serializer):
    menu_item_id = serializers.IntegerField(min_value=1)
    quantity = serializers.IntegerField(min_value=1, max_value=100)
    modifier_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, allow_empty=True, max_length=20
    )
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")


class PosOrderCreateSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=PosOrder.Mode.choices)
    stay_id = serializers.IntegerField(min_value=1, required=False)
    folio_id = serializers.IntegerField(min_value=1, required=False)
    guest_name = serializers.CharField(required=False, allow_blank=True, max_length=200, default="")
    table_number = serializers.CharField(required=False, allow_blank=True, max_length=40, default="")
    delivery_location = serializers.CharField(required=False, allow_blank=True, max_length=160, default="")
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=96, default="")
    lines = PosOrderLineInputSerializer(many=True, min_length=1, max_length=100)

    def validate(self, attrs):
        mode = attrs["mode"]
        if mode == PosOrder.Mode.ROOM_SERVICE and not attrs.get("stay_id"):
            raise serializers.ValidationError({"stay_id": "Room-service orders require a stay."})
        if mode != PosOrder.Mode.ROOM_SERVICE and (attrs.get("stay_id") or attrs.get("folio_id")):
            raise serializers.ValidationError("Only room-service orders can attach a stay or folio.")
        return attrs


class PosOrderSubmitSerializer(serializers.Serializer):
    priority = serializers.IntegerField(required=False, min_value=0, max_value=100, default=0)


class PosOrderStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[
        PosOrder.Status.PREPARING, PosOrder.Status.READY, PosOrder.Status.DELIVERED, PosOrder.Status.CANCELLED,
    ])


class PosTenderCaptureSerializer(serializers.Serializer):
    method = serializers.ChoiceField(choices=PosTender.Method.choices)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    cash_session_reference = serializers.CharField(required=False, allow_blank=True, max_length=64, default="")
    external_reference = serializers.CharField(required=False, allow_blank=True, max_length=120, default="")
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")

    def validate(self, attrs):
        if attrs["method"] == PosTender.Method.CASH and not attrs.get("cash_session_reference"):
            raise serializers.ValidationError({"cash_session_reference": "Cash tender requires an open cash session."})
        return attrs


class CashSessionOpenSerializer(serializers.Serializer):
    opening_float = serializers.DecimalField(required=False, max_digits=12, decimal_places=2, min_value=Decimal("0.00"), default=Decimal("0.00"))
    location = serializers.CharField(required=False, allow_blank=True, max_length=120, default="")
    terminal = serializers.CharField(required=False, allow_blank=True, max_length=120, default="")
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class CashSessionCloseSerializer(serializers.Serializer):
    counted_cash = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.00"))
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class MenuCategoryManageSerializer(serializers.ModelSerializer):
    class Meta:
        model = MenuCategory
        fields = ["id", "name", "slug", "description", "sort_order", "is_active"]


class MenuItemManageSerializer(serializers.ModelSerializer):
    class Meta:
        model = MenuItem
        fields = [
            "id", "category", "name", "sku", "description", "base_price", "currency", "tax_rate_percent",
            "preparation_minutes", "is_available", "is_active", "sort_order",
        ]


class MenuModifierManageSerializer(serializers.ModelSerializer):
    class Meta:
        model = MenuModifier
        fields = ["id", "menu_item", "name", "price_delta", "is_active", "sort_order"]
