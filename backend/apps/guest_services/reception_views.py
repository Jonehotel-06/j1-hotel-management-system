"""Scoped reception data and retail APIs; no administrative stock serializers."""
from collections import Counter
from decimal import Decimal
from datetime import date, datetime, time, timedelta
from django.utils import timezone
from django.db.models import Exists, OuterRef, Q
from rest_framework import serializers
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, ValidationError
from apps.accounts.capabilities import has_capability
from apps.core.permissions import HasCapability
from apps.core.responses import success_response
from apps.inventory.models import StockBalance
from apps.finance.models import CashSession
from .models import MiniMartCategory, MiniMartProduct, MiniMartSale
from .minimart_service import assert_seller, complete_sale
from .services.breakfast_occupancy import (
    breakfast_bookings, breakfast_occupant_context, breakfast_room_numbers, eligible_occupants,
)


class ReceptionGuestView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("guest_amenity.issue", "breakfast.issue")
    require_any_capability = True

    #: Bookings that can never represent a guest currently staying with us.
    #: Excluded from the picker so the desk cannot book breakfast or issue an
    #: amenity against a stay that has already ended or been cancelled.
    INACTIVE_BOOKING_STATUSES = ("CANCELLED", "EXPIRED", "NO_SHOW", "CHECKED_OUT")

    def get(self, request):
        from apps.bookings.models import Booking, Guest
        from apps.core.utils import hotel_today

        # Case-insensitive, whitespace-tolerant term (``"  J1-2026…  "`` works).
        term = " ".join(str(request.query_params.get("search", "") or "").split())[:120]
        today = hotel_today()
        if request.query_params.get("scope") == "breakfast":
            if not has_capability(request.user, "breakfast.issue"):
                raise PermissionDenied("Breakfast permission is required for this roster.")
            try:
                service_date = date.fromisoformat(request.query_params.get("service_date") or today.isoformat())
            except ValueError as exc:
                raise ValidationError({"service_date": "Use a valid YYYY-MM-DD date."}) from exc
            return self.breakfast_guests(term, service_date)

        qs = Guest.objects.all()
        if term:
            # Every whitespace-separated token must match SOMETHING, so
            # "ada obi" narrows to Ada Obi while a full or partial booking
            # reference, an email or a phone number finds the same person.
            for part in term.split():
                qs = qs.filter(
                    Q(first_name__icontains=part)
                    | Q(last_name__icontains=part)
                    | Q(email__icontains=part)
                    | Q(phone__icontains=part)
                    | Q(bookings__booking_reference__icontains=part)
                    | Q(bookings__room_assignments__room__room_number__icontains=part)
                ).distinct()

        # ONE query for the guests plus ONE prefetch for their in-house stays:
        # the previous loop issued a booking query and a room query per guest
        # (an N+1 that made the picker slow exactly when a search matched many
        # rows, which the desk read as "the search does not work").
        guests = list(qs.order_by("first_name", "last_name", "pk")[:50])
        active = (
            Booking.objects.filter(
                guest_id__in=[g.pk for g in guests],
                check_in__lte=today,
                check_out__gte=today,
            )
            .exclude(status__in=self.INACTIVE_BOOKING_STATUSES)
            .prefetch_related("room_assignments__room")
            .order_by("-check_in", "pk")
        )
        stays = {}
        for booking in active:
            stays.setdefault(booking.guest_id, []).append(booking)

        rows = []
        for g in guests:
            bookings = stays.get(g.pk, [])
            rows.append({
                "id": g.pk,
                "full_name": g.full_name,
                "email": getattr(g, "email", "") or "",
                "phone": getattr(g, "phone", "") or "",
                "bookings": [
                    {
                        "id": b.pk,
                        "reference": b.booking_reference,
                        "status": b.status,
                        "check_in": b.check_in,
                        "check_out": b.check_out,
                        "rooms": [
                            assignment.room.room_number
                            for assignment in b.room_assignments.all()
                            if assignment.room_id
                        ],
                    }
                    for b in bookings
                ],
            })
        return success_response({"results": rows, "limit": 50, "search": term})


    def breakfast_guests(self, term, service_date):
        from apps.bookings.models import Guest
        from .models import BreakfastIssuance

        active = breakfast_bookings(service_date)
        guest_stays = active.filter(guest_id=OuterRef("pk"))
        qs = Guest.objects.filter(Exists(guest_stays))
        occupants = eligible_occupants(service_date).filter(booking_room__booking__guest_id=OuterRef("pk")) if term else None
        for part in term.split():
            qs = qs.filter(
                Q(first_name__icontains=part) | Q(last_name__icontains=part)
                | Q(email__icontains=part) | Q(phone__icontains=part)
                | Q(Exists(occupants.filter(Q(full_name__icontains=part) | Q(email__icontains=part))))
                | Q(Exists(guest_stays.filter(booking_reference__icontains=part)))
                | Q(Exists(breakfast_bookings(service_date, match=part).filter(guest_id=OuterRef("pk"))))
            )
        guests = list(qs.order_by("first_name", "last_name", "pk")[:50])
        guest_ids = [guest.pk for guest in guests]
        bookings = list(active.filter(guest_id__in=guest_ids).order_by("-check_in", "pk"))
        booking_ids = [b.pk for b in bookings]
        rooms = breakfast_room_numbers(booking_ids, service_date)
        context = breakfast_occupant_context(booking_ids, service_date)
        served = set(BreakfastIssuance.objects.filter(guest_id__in=guest_ids, service_date=service_date)
                     .values_list("guest_id", flat=True))
        by_guest = {}
        for booking in bookings:
            if numbers := rooms.get(booking.pk):
                by_guest.setdefault(booking.guest_id, []).append({
                    "id": booking.pk, "reference": booking.booking_reference, "status": booking.status,
                    "check_in": booking.check_in, "check_out": booking.check_out, "rooms": numbers,
                    "occupants": context.get(booking.pk, []),
                    "eligible_guests": max(booking.adults + booking.children, 1),
                })
        return success_response({"results": [
            {"id": guest.pk, "full_name": guest.full_name, "bookings": by_guest[guest.pk],
             "breakfast_served": guest.pk in served}
            for guest in guests if guest.pk in by_guest
        ], "limit": 50, "search": term})


class CartLineSerializer(serializers.Serializer):
    product_id = serializers.IntegerField(min_value=1)
    quantity = serializers.IntegerField(min_value=1, max_value=999)


class CheckoutSerializer(serializers.Serializer):
    expected_total = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    idempotency_key = serializers.CharField(max_length=80)
    # Cash sales require the acting cashier's open session; electronic sales
    # do not (they settle through bank/card clearing, not the cash drawer).
    cash_session_reference = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    guest_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    booking_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    lines = CartLineSerializer(many=True, allow_empty=False, max_length=100)
    # Payment method + amount actually received. Defaults preserve the
    # historical cash-only contract for older clients. ROOM_CHARGE settles to
    # the guest's folio instead of a tender (no cash changes hands at the desk).
    payment_method = serializers.ChoiceField(
        choices=["CASH", "CARD", "BANK_TRANSFER", "PAYSTACK", "ROOM_CHARGE"], required=False, default="CASH"
    )
    payment_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0.01"), required=False, default=None
    )
    transaction_reference = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")

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

    def validate(self, attrs):
        method = attrs.get("payment_method") or "CASH"
        if method == "CASH" and not str(attrs.get("cash_session_reference") or "").strip():
            raise serializers.ValidationError({"cash_session_reference": "Cash sales require your open cash session."})
        if method in ("CARD", "BANK_TRANSFER", "PAYSTACK") and not str(attrs.get("transaction_reference") or "").strip():
            raise serializers.ValidationError({"transaction_reference": "A transaction reference is required for this payment method."})
        if method == "ROOM_CHARGE":
            # A room charge is a folio receivable, never a tender: it needs a
            # guest with an in-house stay and must not carry tender fields.
            if not attrs.get("guest_id"):
                raise serializers.ValidationError({"guest_id": "Select the hotel guest for a room charge."})
            if not attrs.get("booking_id"):
                raise serializers.ValidationError({"booking_id": "Select the guest's current booking for a room charge."})
            if str(attrs.get("cash_session_reference") or "").strip():
                raise serializers.ValidationError({"cash_session_reference": "A room charge does not use a cash session."})
            if str(attrs.get("transaction_reference") or "").strip():
                raise serializers.ValidationError({"transaction_reference": "A room charge does not use a transaction reference."})
            if attrs.get("payment_amount") is not None:
                raise serializers.ValidationError({"payment_amount": "A room charge collects no payment at the desk."})
        amount = attrs.get("payment_amount")
        if amount is not None and amount > attrs["expected_total"]:
            raise serializers.ValidationError({"payment_amount": "Amount received cannot exceed the sale total."})
        return attrs


def catalogue(search="", category_id="", category=""):
    """Active, inventory-backed products with server-side name/category search."""
    products = MiniMartProduct.objects.filter(
        is_active=True, item__is_active=True, location__is_active=True,
        item__item_type="SELLABLE_PRODUCT", item__currency="NGN",
    ).select_related("item", "location", "category")
    # An inactive configured category hides its products; legacy rows with no
    # category FK keep their historical item.category text.
    products = products.filter(Q(category__isnull=True) | Q(category__is_active=True))
    if category_id:
        products = products.filter(category_id=category_id)
    if category:
        products = products.filter(Q(category__name__iexact=category) | Q(item__category__iexact=category))
    if search:
        products = products.filter(
            Q(item__name__icontains=search) | Q(item__sku__icontains=search)
            | Q(item__barcode__iexact=search) | Q(category__name__icontains=search)
            | Q(item__category__icontains=search) | Q(description__icontains=search)
        ).distinct()
    rows = []
    for p in products.order_by("item__name", "pk"):
        balance = StockBalance.objects.filter(item_id=p.item_id, location_id=p.location_id).first()
        qty = balance.quantity_on_hand if balance else Decimal("0")
        category_name = p.category.name if p.category_id else p.item.category
        rows.append({
            "id": p.pk, "name": p.item.name, "sku": p.item.sku, "barcode": p.item.barcode,
            "category": category_name, "category_id": p.category_id,
            "description": p.description, "purchase_cost": str(p.item.standard_cost),
            "price": str(p.selling_price), "quantity": str(qty),
            "unit": p.item.base_unit, "location": p.location.name,
            "location_id": p.location_id, "reorder_level": str(p.item.reorder_level),
            "status": "ACTIVE", "low_stock": qty <= p.item.reorder_level,
            "out_of_stock": qty < 1,
        })
    return rows


def active_categories():
    return [
        {"id": row.pk, "name": row.name}
        for row in MiniMartCategory.objects.filter(is_active=True).order_by("name")
    ]


def sale_payload(sale):
    order = sale.order
    tender = order.tenders.order_by("pk").first()
    # A room-charge sale has no tender by design: it is a folio receivable.
    # Report it honestly instead of as an unpaid tender-less sale.
    if tender:
        payment_method = tender.method
        payment_reference = tender.external_reference or tender.reference
        amount_paid = str(tender.amount)
    elif order.folio_id:
        payment_method = "ROOM_CHARGE"
        payment_reference = order.folio.reference
        amount_paid = "0.00"
    else:
        payment_method = None
        payment_reference = None
        amount_paid = "0.00"
    return {"reference": order.reference, "created_at": sale.created_at,
            "customer_type": "GUEST" if sale.guest_id else "WALK_IN", "guest_name": order.guest_name,
            "room_number": sale.room_number, "booking_reference": sale.booking.booking_reference if sale.booking_id else None,
            "total": str(order.net_total), "payment_method": payment_method,
            "payment_reference": payment_reference,
            "amount_paid": amount_paid,
            "payment_status": order.settlement_status, "staff_name": order.created_by.full_name if order.created_by else "—",
            "lines": [{"name": l.order_line.item_name, "quantity": l.order_line.quantity,
                       "price": str(l.order_line.unit_price), "total": str(l.order_line.line_total)}
                      for l in sale.lines.select_related("order_line")]}


class MiniMartProductsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        assert_seller(request.user)
        sessions = CashSession.objects.filter(cashier=request.user, status=CashSession.Status.OPEN)
        search = str(request.query_params.get("search", "")).strip()[:120]
        category_id = str(request.query_params.get("category_id", "")).strip()
        category = str(request.query_params.get("category", "")).strip()
        return success_response({
            "results": catalogue(search=search, category_id=category_id, category=category),
            "categories": active_categories(),
            "payment_methods": ["CASH", "CARD", "BANK_TRANSFER", "PAYSTACK", "ROOM_CHARGE"],
            "cash_sessions": [{"reference": s.reference, "location": s.location} for s in sessions],
        })


class MiniMartCheckoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        assert_seller(request.user)
        serializer = CheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        sale, created = complete_sale(actor=request.user, data=serializer.validated_data)
        # Re-read order after the tender service updates its settlement status.
        sale = MiniMartSale.objects.select_related("order", "booking", "order__folio").get(pk=sale.pk)
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
