"""Explicit POS API representations; money is always server-derived."""
from decimal import Decimal

from rest_framework import serializers

from apps.stays.models import Stay

from .models import (
    KitchenTicket, MenuCategory, MenuItem, MenuModifier, PosOrder, PosOrderEvent, PosOrderLine, PosOrderReturn,
    PosOrderReturnLine, PosTender, RestaurantTable, RestaurantTableSession,
)


class MenuModifierSerializer(serializers.ModelSerializer):
    class Meta:
        model = MenuModifier
        fields = ["id", "name", "price_delta", "is_active", "sort_order"]
        read_only_fields = fields


class MenuItemSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)
    modifiers = MenuModifierSerializer(many=True, read_only=True)
    stock = serializers.SerializerMethodField()

    class Meta:
        model = MenuItem
        fields = [
            "id", "category", "category_name", "name", "sku", "barcode", "description", "base_price", "currency",
            "tax_rate_percent", "preparation_minutes", "is_available", "is_active", "sort_order", "modifiers",
            "stock", "portion_tracking", "prepared_portions",
        ]
        read_only_fields = fields

    def get_stock(self, obj):
        """Live stock source and availability for order entry (read-only).

        Computed from the inventory service's shared resolver, which is built
        on the same recipe/direct-sale mapping the delivery-time deduction
        uses, so ``stock.location_name``/``location_code`` is exactly the store
        the sale will be deducted from. The menu view supplies pre-computed
        summaries through the serializer context (one bulk resolution for the
        whole menu); the fallback keeps the field correct for any caller that
        serializes items without that context.
        """
        summaries = (self.context or {}).get("pos_stock_summaries") or {}
        summary = summaries.get(obj.pk)
        if summary is None:
            from .services.barcode_service import stock_summary_for_menu_item

            summary = stock_summary_for_menu_item(obj)
        return summary


class BarcodeLookupSerializer(serializers.Serializer):
    """Server-authoritative answer to one scanned barcode.

    Price and identity always come from the stored menu item; stock figures
    are read from the existing recipe/balance projection and never imply a
    sale has happened.
    """

    menu_item_id = serializers.IntegerField()
    name = serializers.CharField()
    sku = serializers.CharField()
    barcode = serializers.CharField(allow_null=True)
    matched_by = serializers.ChoiceField(choices=["barcode", "sku", "menu_item"])
    category_id = serializers.IntegerField()
    category_name = serializers.CharField()
    service_area = serializers.CharField()
    base_price = serializers.DecimalField(max_digits=12, decimal_places=2)
    currency = serializers.CharField()
    is_available = serializers.BooleanField()
    modifiers = MenuModifierSerializer(many=True)
    stock = serializers.DictField()


class MenuCategoryMenuSerializer(serializers.ModelSerializer):
    items = serializers.SerializerMethodField()

    class Meta:
        model = MenuCategory
        fields = ["id", "name", "slug", "service_area", "description", "sort_order", "items"]
        read_only_fields = fields

    def get_items(self, obj):
        # View prefetches filtered available items into this attr; the fallback
        # makes the serializer safe in admin/shell use without N+1 in hot APIs.
        items = getattr(obj, "_available_items", None)
        if items is None:
            items = obj.items.filter(is_active=True, is_available=True).prefetch_related("modifiers")
        # Forward the context so the per-item `stock` field can read the bulk
        # stock summaries the view resolved for the whole menu.
        return MenuItemSerializer(items, many=True, context=self.context).data


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
    order_lines = serializers.SerializerMethodField()

    def get_order_lines(self, obj):
        return [
            {
                "item_name": line.item_name,
                "quantity": line.quantity,
                "notes": line.notes,
                "modifiers": line.modifiers_snapshot,
            }
            for line in obj.order.lines.all()
        ]

    class Meta:
        model = KitchenTicket
        fields = [
            "id", "station", "status", "priority", "queued_at", "started_at", "ready_at", "completed_at",
            "assigned_to_email", "notes", "order_reference", "order_guest_name", "order_mode",
            "order_delivery_location", "order_table_number", "order_lines",
        ]
        read_only_fields = fields


class KitchenTicketStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[KitchenTicket.Status.PREPARING, KitchenTicket.Status.READY])


class TableSummarySerializerMixin(serializers.Serializer):
    """Read-only per-table order/money summaries shared by the table surfaces.

    The values are computed by annotated querysets (see ``views``); a bare model
    instance (for example the object returned right after a create or a PATCH)
    simply renders zeroes instead of raising.
    """

    guest_order_count = serializers.SerializerMethodField()
    total_ordered_amount = serializers.SerializerMethodField()
    returned_amount = serializers.SerializerMethodField()
    captured_amount = serializers.SerializerMethodField()
    unpaid_balance = serializers.SerializerMethodField()

    @staticmethod
    def _money(obj, name):
        value = getattr(obj, name, None)
        if value is None:
            return "0.00"
        return str(Decimal(value).quantize(Decimal("0.01")))

    @staticmethod
    def _unpaid_balance(obj):
        total = getattr(obj, "total_ordered_amount", None)
        if total is None:
            return "0.00"
        returned = getattr(obj, "returned_amount", None) or Decimal("0.00")
        captured = getattr(obj, "captured_amount", None) or Decimal("0.00")
        outstanding = Decimal(total) - Decimal(returned) - Decimal(captured)
        if outstanding < Decimal("0.00"):
            outstanding = Decimal("0.00")
        return str(outstanding.quantize(Decimal("0.01")))

    def get_guest_order_count(self, obj):
        value = getattr(obj, "guest_order_count", None)
        return 0 if value is None else int(value)

    def get_total_ordered_amount(self, obj):
        return self._money(obj, "total_ordered_amount")

    def get_returned_amount(self, obj):
        return self._money(obj, "returned_amount")

    def get_captured_amount(self, obj):
        return self._money(obj, "captured_amount")

    def get_unpaid_balance(self, obj):
        return self._unpaid_balance(obj)


class RestaurantTableSerializer(TableSummarySerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = RestaurantTable
        fields = [
            "id", "code", "name", "section", "seats", "is_active", "created_at", "updated_at",
            "guest_order_count", "total_ordered_amount", "returned_amount", "captured_amount", "unpaid_balance",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_code(self, value):
        value = str(value or "").strip().upper()
        allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
        if not value or len(value) > 32 or any(char not in allowed for char in value):
            raise serializers.ValidationError("Use up to 32 letters, numbers, dots, underscores, or hyphens.")
        matches = RestaurantTable.objects.filter(code=value)
        if self.instance is not None:
            matches = matches.exclude(pk=self.instance.pk)
        if matches.exists():
            raise serializers.ValidationError("A restaurant table with this code already exists.")
        return value


class RestaurantTableSessionCreateSerializer(serializers.Serializer):
    table_id = serializers.IntegerField(min_value=1)
    covers = serializers.IntegerField(min_value=1, max_value=60, default=1)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")
    idempotency_key = serializers.CharField(min_length=8, max_length=96, trim_whitespace=True)


class RestaurantTableSessionCloseSerializer(serializers.Serializer):
    close_note = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")


class RestaurantTableSessionTransferSerializer(serializers.Serializer):
    destination_table_id = serializers.IntegerField(min_value=1)


class RestaurantTableSessionSerializer(serializers.ModelSerializer):
    table_id = serializers.IntegerField(source="table.id", read_only=True)
    table_code = serializers.CharField(source="table.code", read_only=True)
    table_name = serializers.CharField(source="table.name", read_only=True)
    table_section = serializers.CharField(source="table.section", read_only=True)
    table_seats = serializers.IntegerField(source="table.seats", read_only=True)
    opened_by_email = serializers.CharField(source="opened_by.email", read_only=True, allow_null=True)
    closed_by_email = serializers.CharField(source="closed_by.email", read_only=True, allow_null=True)
    order_count = serializers.IntegerField(read_only=True)
    active_order_count = serializers.IntegerField(read_only=True)
    unpaid_order_count = serializers.IntegerField(read_only=True)
    guest_order_count = serializers.IntegerField(read_only=True)
    total_ordered_amount = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    returned_amount = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    captured_amount = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    unpaid_balance = serializers.SerializerMethodField()

    def get_unpaid_balance(self, obj):
        """Outstanding amount for the session, kept separate from the ordered total.

        Ordered total is what the guest was charged; the unpaid balance is what is
        still owed after returns and captured tenders. It is never folded into the
        ordered total so a reviewer can tell "how much was ordered" from "how much
        is still to pay".
        """
        total = getattr(obj, "total_ordered_amount", None)
        if total is None:
            return None
        returned = getattr(obj, "returned_amount", None) or Decimal("0.00")
        captured = getattr(obj, "captured_amount", None) or Decimal("0.00")
        outstanding = total - returned - captured
        if outstanding < Decimal("0.00"):
            outstanding = Decimal("0.00")
        return str(outstanding.quantize(Decimal("0.01")))

    class Meta:
        model = RestaurantTableSession
        fields = [
            "reference", "table_id", "table_code", "table_name", "table_section", "table_seats", "covers",
            "status", "opened_at", "opened_by_email", "closed_at", "closed_by_email", "notes", "close_note",
            "order_count", "active_order_count", "unpaid_order_count", "guest_order_count",
            "total_ordered_amount", "returned_amount", "captured_amount", "unpaid_balance",
        ]
        read_only_fields = fields


class PosOrderListSerializer(serializers.ModelSerializer):
    stay_reference = serializers.CharField(source="stay.reference", read_only=True)
    folio_reference = serializers.CharField(source="folio.reference", read_only=True)
    created_by_email = serializers.CharField(source="created_by.email", read_only=True)
    line_count = serializers.IntegerField(read_only=True)
    table_session_reference = serializers.CharField(source="table_session.reference", read_only=True, allow_null=True)
    table_session_code = serializers.CharField(source="table_session.table.code", read_only=True, allow_null=True)
    service_request_reference = serializers.CharField(source="service_request.reference", read_only=True, allow_null=True)
    # --- Server-derived settlement figures (read-only) ---------------------
    # The POS screen must never compute what a guest owes. These four values
    # come from the same rules ``capture_direct_tender`` enforces: the charge
    # ceiling is ``net_total`` (order total minus unopened-item returns), what
    # was collected is the sum of CAPTURED tenders, and what is left is the
    # difference. ``captured_amount`` is annotated by the views so listing
    # orders stays a single query; the fallback keeps the field correct for any
    # caller that serialises an order without that annotation.
    net_total = serializers.SerializerMethodField()
    returned_amount = serializers.SerializerMethodField()
    amount_paid = serializers.SerializerMethodField()
    outstanding_balance = serializers.SerializerMethodField()
    #: True only when a direct tender may legally be captured right now.
    settlement_action_available = serializers.SerializerMethodField()

    class Meta:
        model = PosOrder
        fields = [
            "id", "reference", "mode", "status", "settlement_status", "stay_reference", "folio_reference",
            "guest_name", "table_number", "table_session_reference", "table_session_code", "service_request_reference",
            "delivery_location", "currency", "subtotal", "tax_amount",
            "total_amount", "submitted_at", "delivered_at", "created_at", "created_by_email", "line_count",
            "net_total", "returned_amount", "amount_paid", "outstanding_balance", "settlement_action_available",
        ]
        read_only_fields = fields

    @staticmethod
    def _captured(obj):
        """Captured total: from the view's annotation when present, else computed."""
        annotated = getattr(obj, "captured_amount", None)
        if annotated is not None:
            return Decimal(annotated).quantize(Decimal("0.01"))
        from .services.order_service import tender_captured_amount

        return tender_captured_amount(obj)

    def get_net_total(self, obj):
        return str(Decimal(obj.net_total).quantize(Decimal("0.01")))

    def get_returned_amount(self, obj):
        return str(Decimal(obj.returned_amount or 0).quantize(Decimal("0.01")))

    def get_amount_paid(self, obj):
        return str(self._captured(obj).quantize(Decimal("0.01")))

    def get_outstanding_balance(self, obj):
        from .services.order_service import order_outstanding_balance

        return str(order_outstanding_balance(obj, captured=self._captured(obj)).quantize(Decimal("0.01")))

    def get_settlement_action_available(self, obj):
        """Whether a tender can be captured for this order right now.

        Mirrors every server-side precondition of ``capture_direct_tender`` so
        the button cannot offer a collection the API will refuse: delivered,
        charged, a direct-sale mode (room service settles on the folio), and a
        positive outstanding balance.
        """
        if obj.mode == PosOrder.Mode.ROOM_SERVICE:
            return False
        if obj.status != PosOrder.Status.DELIVERED or not obj.charge_transaction_id:
            return False
        return self.get_outstanding_balance(obj) != "0.00"


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
    table_session_reference = serializers.CharField(required=False, allow_blank=True, max_length=64, default="")
    service_request_reference = serializers.CharField(required=False, allow_blank=True, max_length=64, default="")
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
        if attrs.get("table_session_reference") and mode != PosOrder.Mode.RESTAURANT:
            raise serializers.ValidationError({"table_session_reference": "Registered table sessions are for restaurant dine-in orders only."})
        if attrs.get("service_request_reference") and mode not in {PosOrder.Mode.RESTAURANT, PosOrder.Mode.BAR}:
            raise serializers.ValidationError({"service_request_reference": "Table QR requests can create only restaurant or bar drafts."})
        return attrs


class PosOrderSubmitSerializer(serializers.Serializer):
    priority = serializers.IntegerField(required=False, min_value=0, max_value=100, default=0)


class PosOrderStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[
        PosOrder.Status.PREPARING, PosOrder.Status.READY, PosOrder.Status.DELIVERED, PosOrder.Status.CANCELLED,
    ])


class PosTenderCaptureSerializer(serializers.Serializer):
    """Input for one direct POS collection.

    ``amount`` stays required and is the amount the cashier actually took, but
    it is never trusted as a balance: the service recomputes what is owed from
    the order and its captured tenders and refuses anything above it.

    The optional fields are additive and backwards compatible:

    * ``currency`` — when supplied it must match the order's currency;
    * ``idempotency_key`` — one key per tender form; a repeated key returns the
      tender already recorded instead of collecting twice (double click,
      network retry, lost response);
    * ``expected_outstanding_balance`` — the balance the screen displayed. A
      mismatch refuses the collection so a stale form cannot bank an amount that
      another cashier already changed.
    """

    method = serializers.ChoiceField(choices=PosTender.Method.choices)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    cash_session_reference = serializers.CharField(required=False, allow_blank=True, max_length=64, default="")
    external_reference = serializers.CharField(required=False, allow_blank=True, max_length=120, default="")
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")
    currency = serializers.CharField(required=False, allow_blank=True, max_length=3, default="")
    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=96, default="")
    expected_outstanding_balance = serializers.DecimalField(
        required=False, allow_null=True, max_digits=12, decimal_places=2, min_value=Decimal("0.00")
    )

    def validate(self, attrs):
        if attrs["method"] == PosTender.Method.CASH and not attrs.get("cash_session_reference"):
            raise serializers.ValidationError({"cash_session_reference": "Cash tender requires an open cash session."})
        currency = (attrs.get("currency") or "").strip().upper()
        if currency and len(currency) != 3:
            raise serializers.ValidationError({"currency": "Currency must be a three-letter ISO code."})
        attrs["currency"] = currency
        attrs["idempotency_key"] = (attrs.get("idempotency_key") or "").strip()
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
    """Editable menu category plus the counts the management table shows.

    ``item_count`` is annotated by the view (one aggregate query); when the
    serializer is used without that annotation it falls back to a count of the
    related items so the field is never wrong, only slower.
    """

    item_count = serializers.SerializerMethodField()

    class Meta:
        model = MenuCategory
        fields = [
            "id", "name", "slug", "service_area", "description", "sort_order", "is_active",
            "item_count", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "item_count", "created_at", "updated_at"]

    def get_item_count(self, obj):
        annotated = getattr(obj, "item_count", None)
        if annotated is not None:
            return annotated
        return obj.items.count()


class MenuItemManageSerializer(serializers.ModelSerializer):
    """Editable menu item plus the read-only context its table row needs."""

    category_name = serializers.CharField(source="category.name", read_only=True)
    service_area = serializers.CharField(source="category.service_area", read_only=True)
    stock_linkage = serializers.SerializerMethodField()
    margin = serializers.SerializerMethodField()

    class Meta:
        model = MenuItem
        fields = [
            "id", "category", "category_name", "service_area", "name", "sku", "barcode", "description",
            "base_price", "currency", "tax_rate_percent", "preparation_minutes", "is_available",
            "is_active", "sort_order", "stock_linkage", "margin", "created_at", "updated_at", "portion_tracking", "prepared_portions",
        ]
        read_only_fields = [
            "id", "category_name", "service_area", "stock_linkage", "margin", "created_at", "updated_at", "portion_tracking", "prepared_portions",
        ]

    def get_stock_linkage(self, obj):
        """How this sale reaches the stock ledger — evidence, never an inference.

        ``recipe`` is an active stock recipe (ingredient BOM); ``direct_sale``
        is an explicit one-product-per-menu-unit configuration; ``none`` means
        nothing is configured, so a delivered sale deducts nothing and the
        inventory hand-off is parked for review.
        """
        prefetched = getattr(obj, "_stock_linkage", None)
        if prefetched is not None:
            return prefetched
        active_recipe = (
            obj.stock_recipes.filter(is_active=True).order_by("-version").first()
            if hasattr(obj, "stock_recipes")
            else None
        )
        direct_sale = None
        if hasattr(obj, "direct_sale_stock_items"):
            direct_sale = obj.direct_sale_stock_items.first()
        if active_recipe is not None:
            lines = active_recipe.lines.count()
            return {
                "type": "recipe" if active_recipe.tracking_mode == "COMPONENTS" else "non_stock",
                "reference": active_recipe.reference,
                "version": active_recipe.version,
                "component_count": lines,
                "location": active_recipe.location.code if active_recipe.location_id else "",
            }
        if direct_sale is not None:
            return {
                "type": "direct_sale",
                "reference": direct_sale.sku,
                "version": 0,
                "component_count": 1,
                "location": direct_sale.direct_sale_location.code if direct_sale.direct_sale_location_id else "",
                "quantity_per_menu_unit": str(direct_sale.direct_sale_quantity),
            }
        return {"type": "none", "reference": "", "version": 0, "component_count": 0, "location": ""}

    def get_margin(self, obj):
        """Selling price against the recorded supplier cost of ONE sale.

        Computed only when the caller asks for it (``?with_margin=true`` on the
        management list, or on a single-item read): the cost basis reads recipes
        and goods-receipt averages per row, which is worth paying for on a
        bounded page an operator is reading, but must not be paid silently by
        every consumer of the catalogue.

        The same ``menu_item_cost_basis`` the save-time price/cost guard uses, so
        the number an operator sees in the table is the number the guard
        enforces — never a second, drifting calculation. ``None`` fields mean
        "nothing honest to compare": no cost on record, or a cost in a different
        currency. The menu screen shows "cost not recorded" rather than a made-up
        margin, because a guessed margin is worse than no margin at all.
        """
        if not self.context.get("include_margin"):
            return None
        from apps.inventory.services.valuation_service import menu_item_cost_basis

        price = getattr(obj, "base_price", None)
        currency = str(getattr(obj, "currency", "") or "NGN").upper()
        payload = {
            "unit_cost": None,
            "cost_basis": "",
            "currency": currency,
            "margin_amount": None,
            "margin_percent": None,
            "below_cost": False,
            "comparable": False,
            "note": "",
        }
        if price is None:
            payload["note"] = "No selling price recorded."
            return payload
        basis = menu_item_cost_basis(obj)
        if not basis:
            payload["note"] = "No supplier cost on record for one sale, so margin cannot be shown."
            return payload
        if currency not in {(code or "NGN").upper() for code in (basis.get("currencies") or [])}:
            payload["note"] = "The recorded cost is in a different currency from the selling price."
            return payload
        from decimal import Decimal, ROUND_HALF_UP

        cost = basis["cost"]
        margin = Decimal(price) - cost
        percent = (margin / Decimal(price) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if price else None
        payload.update({
            "unit_cost": str(cost),
            "cost_basis": basis.get("basis", ""),
            "margin_amount": str(margin),
            "margin_percent": str(percent) if percent is not None else None,
            "below_cost": margin < 0,
            "comparable": True,
            "note": "" if margin >= 0 else "Selling price is below the recorded supplier cost.",
        })
        return payload

    def validate(self, attrs):
        attrs = super().validate(attrs)
        # Selling below supplier cost loses money on every sale, so a menu
        # price may only be saved when it covers the known purchase cost of
        # what one sale consumes (recipe components, or the stock item that is
        # the product itself). When no supplier cost is on record there is
        # nothing honest to compare against and the price is left to staff.
        # Only a create or an actual price change is checked: editing an
        # unrelated field (barcode, availability, ...) must not be blocked by
        # a price that was set before the cost was recorded.
        if self.instance is not None and "base_price" not in attrs:
            return attrs
        price = attrs.get("base_price", getattr(self.instance, "base_price", None))
        if price is None:
            return attrs
        probe = self.instance or MenuItem(
            sku=attrs.get("sku", ""), name=attrs.get("name", ""),
        )
        if self.instance is not None:
            probe = MenuItem(
                pk=self.instance.pk,
                sku=attrs.get("sku", self.instance.sku),
                name=attrs.get("name", self.instance.name),
            )
        from apps.inventory.services.valuation_service import menu_item_cost_basis

        basis = menu_item_cost_basis(probe)
        if basis is None:
            return attrs
        currency = str(attrs.get("currency", getattr(self.instance, "currency", "NGN")) or "NGN").upper()
        if basis["currencies"] and basis["currencies"] != {currency}:
            return attrs  # never compare prices across currencies
        cost = basis["cost"]
        if price < cost:
            raise serializers.ValidationError({
                "base_price": (
                    f"Selling price {price} is below the supplier cost {cost} for this product. "
                    "Enter a price equal to or higher than the purchase cost."
                )
            })
        return attrs

    def validate_barcode(self, value):
        from .barcode import validate_barcode

        try:
            return validate_barcode(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc


class MenuModifierManageSerializer(serializers.ModelSerializer):
    class Meta:
        model = MenuModifier
        fields = ["id", "menu_item", "name", "price_delta", "is_active", "sort_order"]


class PosOrderReturnLineInputSerializer(serializers.Serializer):
    order_line_id = serializers.IntegerField(min_value=1)
    quantity_returned = serializers.IntegerField(min_value=1, max_value=1000)


class PosOrderReturnCreateSerializer(serializers.Serializer):
    """Validated shape of an unopened-item return request.

    Money is never accepted from the client: ``refund_method`` chooses how an
    already-computed refund is paid, and every amount is derived server-side.
    """

    lines = PosOrderReturnLineInputSerializer(many=True, allow_empty=False, max_length=100)
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    notes = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")
    refund_method = serializers.ChoiceField(
        choices=PosOrderReturn.RefundMethod.choices, required=False, default=PosOrderReturn.RefundMethod.NONE
    )
    cash_session_reference = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    restock_location = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    received_by = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    idempotency_key = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")


class PosOrderReturnRefundConfirmSerializer(serializers.Serializer):
    refund_method = serializers.ChoiceField(choices=PosOrderReturn.RefundMethod.choices, required=False)
    provider_reference = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    cash_session_reference = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    succeeded = serializers.BooleanField(required=False, default=True)
    failure_reason = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


class PosOrderReturnLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = PosOrderReturnLine
        fields = [
            "id", "order_line", "item_name", "item_sku", "quantity_returned", "unit_net_price",
            "line_net_amount", "restock_quantity",
        ]
        read_only_fields = fields


class PosOrderReturnSerializer(serializers.ModelSerializer):
    order_reference = serializers.CharField(source="order.reference", read_only=True)
    order_mode = serializers.CharField(source="order.mode", read_only=True)
    order_status = serializers.CharField(source="order.status", read_only=True)
    guest_name = serializers.CharField(source="order.guest_name", read_only=True)
    table_number = serializers.CharField(source="order.table_number", read_only=True)
    currency = serializers.CharField(source="order.currency", read_only=True)
    settlement_status = serializers.CharField(source="order.settlement_status", read_only=True)
    recorded_by_email = serializers.CharField(source="recorded_by.email", read_only=True)
    refund_recorded_by_email = serializers.CharField(source="refund_recorded_by.email", read_only=True)
    restock_location_code = serializers.CharField(source="restock_location.code", read_only=True)
    approval_reference = serializers.CharField(source="approval.reference", read_only=True)
    approval_status = serializers.CharField(source="approval.status", read_only=True)
    lines = PosOrderReturnLineSerializer(many=True, read_only=True)
    outstanding_balance = serializers.SerializerMethodField()

    class Meta:
        model = PosOrderReturn
        fields = [
            "id", "reference", "order", "order_reference", "order_mode", "order_status", "guest_name",
            "table_number", "currency", "settlement_status", "reason", "notes", "received_by",
            "restock_location", "restock_location_code", "original_total", "returned_amount",
            "final_total", "amount_paid", "refund_due", "outstanding_balance", "refund_method",
            "refund_status", "refund_reference", "refund_failure_reason", "refund_recorded_by_email",
            "refund_recorded_at", "recorded_by_email", "approval_reference", "approval_status",
            "created_at", "lines",
        ]
        read_only_fields = fields

    def get_outstanding_balance(self, obj):
        from decimal import Decimal

        balance = Decimal(obj.final_total or 0) - Decimal(obj.amount_paid or 0)
        return str(balance.quantize(Decimal("0.01")) if balance > 0 else Decimal("0.00"))


class PosReturnableOrderSerializer(serializers.ModelSerializer):
    """One bar order as the return screen lists it."""

    staff_email = serializers.CharField(source="created_by.email", read_only=True)
    amount_paid = serializers.SerializerMethodField()
    net_total = serializers.SerializerMethodField()
    outstanding_balance = serializers.SerializerMethodField()
    returned_amount = serializers.SerializerMethodField()
    return_count = serializers.IntegerField(read_only=True)
    line_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = PosOrder
        fields = [
            "id", "reference", "mode", "status", "settlement_status", "guest_name", "table_number",
            "staff_email", "currency", "total_amount", "returned_amount", "net_total", "amount_paid",
            "outstanding_balance", "return_count", "line_count", "delivered_at", "created_at",
        ]
        read_only_fields = fields

    def get_amount_paid(self, obj):
        paid = getattr(obj, "paid_total", None)
        return str(paid if paid is not None else "0.00")

    def get_net_total(self, obj):
        return str(obj.net_total)

    def get_returned_amount(self, obj):
        from .services.return_service import returned_amount_total

        cached = getattr(obj, "_returned_amount_cache", None)
        if cached is None:
            cached = returned_amount_total(obj)
            obj._returned_amount_cache = cached
        return str(cached)

    def get_outstanding_balance(self, obj):
        from decimal import Decimal

        paid = Decimal(str(getattr(obj, "paid_total", 0) or 0))
        balance = obj.net_total - paid
        return str(balance if balance > Decimal("0.00") else Decimal("0.00"))
