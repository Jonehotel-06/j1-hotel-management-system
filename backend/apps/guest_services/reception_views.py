"""Scoped reception data and retail APIs; no administrative stock serializers."""
from collections import Counter
from decimal import Decimal
from datetime import datetime, time, timedelta
from django.utils import timezone
from django.db.models import Q
from rest_framework import serializers
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError
from apps.core.permissions import HasCapability
from apps.core.responses import success_response
from apps.inventory.models import StockBalance
from apps.finance.models import CashSession
from .models import MiniMartProduct, MiniMartSale
from .minimart_service import assert_seller, complete_sale


class ReceptionGuestView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("guest_amenity.issue", "breakfast.issue")
    require_any_capability = True

    def get(self, request):
        from apps.bookings.models import Guest
        from apps.core.utils import hotel_today
        term = str(request.query_params.get("search", "")).strip()[:120]
        qs = Guest.objects.all()
        for part in term.split():
            qs = qs.filter(Q(first_name__icontains=part) | Q(last_name__icontains=part) |
                           Q(bookings__booking_reference__icontains=part) |
                           Q(bookings__room_assignments__room__room_number__icontains=part)).distinct()
        rows = []
        for g in qs.order_by("first_name", "last_name", "pk")[:50]:
            # Include booking IDs instead of guessing the latest future booking.
            bookings = g.bookings.exclude(status__in=["CANCELLED", "EXPIRED", "NO_SHOW", "CHECKED_OUT"]).filter(
                check_in__lte=hotel_today(), check_out__gte=hotel_today()
            ).prefetch_related("room_assignments__room").order_by("-check_in", "pk")
            rows.append({"id": g.pk, "full_name": g.full_name,
                         "bookings": [{"id": b.pk, "reference": b.booking_reference, "status": b.status,
                                       "check_in": b.check_in, "check_out": b.check_out,
                                       "rooms": list(b.room_assignments.values_list("room__room_number", flat=True))}
                                      for b in bookings]})
        return success_response({"results": rows, "limit": 50})


class CartLineSerializer(serializers.Serializer):
    product_id = serializers.IntegerField(min_value=1)
    quantity = serializers.IntegerField(min_value=1, max_value=999)


class CheckoutSerializer(serializers.Serializer):
    expected_total = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    idempotency_key = serializers.CharField(max_length=80)
    cash_session_reference = serializers.CharField(max_length=64)
    guest_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    booking_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    lines = CartLineSerializer(many=True, allow_empty=False, max_length=100)

    def to_internal_value(self, data):
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError({key: "This field is not accepted." for key in unknown})
        return super().to_internal_value(data)

    def validate_lines(self, value):
        ids = [line["product_id"] for line in value]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("Combine duplicate products into one cart line.")
        return sorted(value, key=lambda line: line["product_id"])


def catalogue():
    products = MiniMartProduct.objects.filter(is_active=True, item__is_active=True, location__is_active=True,
                                              item__item_type="SELLABLE_PRODUCT", item__currency="NGN").select_related("item", "location")
    rows = []
    for p in products.order_by("item__category", "item__name", "pk"):
        balance = StockBalance.objects.filter(item_id=p.item_id, location_id=p.location_id).first()
        qty = balance.quantity_on_hand if balance else Decimal("0")
        rows.append({"id": p.pk, "name": p.item.name, "sku": p.item.sku, "barcode": p.item.barcode,
                     "category": p.item.category, "price": str(p.selling_price), "quantity": str(qty),
                     "unit": p.item.base_unit, "location": p.location.name, "status": "ACTIVE",
                     "low_stock": qty <= p.item.reorder_level, "out_of_stock": qty < 1})
    return rows


def sale_payload(sale):
    order = sale.order
    tender = order.tenders.order_by("pk").first()
    return {"reference": order.reference, "created_at": sale.created_at,
            "customer_type": "GUEST" if sale.guest_id else "WALK_IN", "guest_name": order.guest_name,
            "room_number": sale.room_number, "booking_reference": sale.booking.booking_reference if sale.booking_id else None,
            "total": str(order.net_total), "payment_method": tender.method if tender else None,
            "payment_status": order.settlement_status, "staff_name": order.created_by.full_name if order.created_by else "—",
            "lines": [{"name": l.order_line.item_name, "quantity": l.order_line.quantity,
                       "price": str(l.order_line.unit_price), "total": str(l.order_line.line_total)}
                      for l in sale.lines.select_related("order_line")]}


class MiniMartProductsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        assert_seller(request.user)
        sessions = CashSession.objects.filter(cashier=request.user, status=CashSession.Status.OPEN)
        return success_response({"results": catalogue(), "payment_methods": ["CASH"],
                                 "cash_sessions": [{"reference": s.reference, "location": s.location} for s in sessions]})


class MiniMartCheckoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        assert_seller(request.user)
        serializer = CheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        sale, created = complete_sale(actor=request.user, data=serializer.validated_data)
        # Re-read order after the tender service updates its settlement status.
        sale = MiniMartSale.objects.select_related("order", "booking").get(pk=sale.pk)
        return success_response({"created": created, "sale": sale_payload(sale), "products": catalogue()})


class HistoryFilterSerializer(serializers.Serializer):
    start = serializers.DateField(required=False)
    end = serializers.DateField(required=False)
    search = serializers.CharField(required=False, allow_blank=True, max_length=120)
    page = serializers.IntegerField(required=False, default=1, min_value=1)

    def validate(self, data):
        if data.get("start") and data.get("end") and data["start"] > data["end"]:
            raise serializers.ValidationError("Start date must be before end date.")
        return data


class MiniMartHistoryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        assert_seller(request.user)
        params = HistoryFilterSerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        data = params.validated_data
        qs = MiniMartSale.objects.select_related("order__created_by", "booking").all()
        if request.user.role == "RECEPTIONIST":
            qs = qs.filter(order__created_by=request.user)
        if data.get("start"):
            qs = qs.filter(created_at__gte=timezone.make_aware(datetime.combine(data["start"], time.min)))
        if data.get("end"):
            qs = qs.filter(created_at__lt=timezone.make_aware(datetime.combine(data["end"] + timedelta(days=1), time.min)))
        if data.get("search"):
            qs = qs.filter(Q(order__reference__icontains=data["search"]) | Q(order__guest_name__icontains=data["search"]) |
                           Q(lines__order_line__item_name__icontains=data["search"])).distinct()
        # Summaries use all filtered transactions, not only the displayed page.
        total = Decimal("0")
        best = Counter()
        for sale in qs.prefetch_related("lines__order_line"):
            total += sale.order.net_total
            for line in sale.lines.all():
                best[line.order_line.item_name] += line.order_line.quantity
        page = data["page"]
        count = qs.count()
        return success_response({"results": [sale_payload(s) for s in qs[(page-1)*50:page*50]],
                                 "count": count, "page": page, "has_next": page*50 < count,
                                 "summary": {"total_sales": str(total), "transactions": count, "items_sold": sum(best.values()),
                                             "best_selling": [{"name": n, "quantity": q} for n, q in best.most_common(5)],
                                             "low_stock": [r for r in catalogue() if r["low_stock"]]}})
