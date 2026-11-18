# apps/events/views.py
"""Reception Event Management API (staff namespace /api/admin/events/)."""
from django.db.models import Q
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from apps.core.permissions import HasCapability
from apps.core.responses import success_response

from .models import EventLocation, EventPayment, HotelEvent
from .serializers import (
    EventCancelSerializer, EventCreateSerializer, EventLocationSerializer,
    EventPaymentCreateSerializer, EventPaymentSerializer, HotelEventDetailSerializer,
    HotelEventSerializer,
)
from . import services


class EventLocationListView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("booking.manage", "payment.capture")
    require_any_capability = True

    def get(self, request):
        qs = EventLocation.objects.filter(is_active=True).order_by("display_order", "name")
        if search := (request.query_params.get("search") or "").strip():
            qs = qs.filter(Q(name__icontains=search) | Q(code__icontains=search))
        return success_response(EventLocationSerializer(qs, many=True).data)


class EventListCreateView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("booking.manage", "payment.capture")
    require_any_capability = True

    def get(self, request):
        qs = HotelEvent.objects.select_related("guest", "location", "created_by").all()
        params = request.query_params
        if params.get("date_from"):
            qs = qs.filter(event_date__gte=params["date_from"])
        if params.get("date_to"):
            qs = qs.filter(event_date__lte=params["date_to"])
        if params.get("location_id"):
            qs = qs.filter(location_id=params["location_id"])
        if params.get("guest_id"):
            qs = qs.filter(guest_id=params["guest_id"])
        if params.get("payment_status"):
            qs = qs.filter(payment_status=params["payment_status"].upper())
        if params.get("status"):
            qs = qs.filter(status=params["status"].upper())
        if search := (params.get("search") or "").strip():
            qs = qs.filter(
                Q(reference__icontains=search) | Q(guest__first_name__icontains=search)
                | Q(guest__last_name__icontains=search) | Q(guest__phone__icontains=search)
                | Q(guest__email__icontains=search) | Q(event_type__icontains=search)
            )
        rows = list(qs.order_by("-event_date", "-start_time", "-created_at")[:200])
        for event in rows:
            services.update_event_status(event=event, actor=request.user, request=request)
        return success_response(HotelEventSerializer(rows, many=True).data)

    def post(self, request):
        serializer = EventCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        from apps.bookings.models import Guest

        guest = None
        if data.get("guest_id"):
            guest = Guest.objects.filter(pk=data["guest_id"]).first()
            if guest is None:
                raise ValidationError({"guest_id": "Unknown guest."})
        else:
            guest, _ = Guest.objects.get_or_create(
                email__iexact=(data.get("email") or "").strip().lower() if data.get("email") else None,
                defaults={
                    "first_name": (data.get("first_name") or "").strip(),
                    "last_name": (data.get("last_name") or "").strip(),
                    "phone": (data.get("phone") or "").strip(),
                    "email": (data.get("email") or "").strip().lower(),
                },
            ) if data.get("email") else (None, False)
            if guest is None:
                guest = Guest.objects.create(
                    first_name=(data.get("first_name") or "").strip(),
                    last_name=(data.get("last_name") or "").strip(),
                    phone=(data.get("phone") or "").strip(),
                    email=(data.get("email") or "").strip().lower(),
                )

        location = EventLocation.objects.filter(pk=data["location_id"]).first()
        if location is None:
            raise ValidationError({"location_id": "Unknown event location."})

        initial_payment = None
        if data.get("amount_paid") is not None and data["amount_paid"] > 0:
            initial_payment = {
                "amount": data["amount_paid"],
                "method": data.get("payment_method") or "CASH",
                "external_reference": data.get("payment_reference") or "",
                "idempotency_key": data.get("payment_idempotency_key") or "",
            }

        event, created = services.create_event(
            guest=guest, location=location,
            event_type=data.get("event_type") or "",
            event_date=data["event_date"], start_time=data["start_time"], end_time=data["end_time"],
            expected_guests=data.get("expected_guests") or 1,
            total_charged=data["total_charged"], notes=data.get("notes") or "",
            actor=request.user, idempotency_key=data.get("idempotency_key") or "",
            override=bool(data.get("override")), override_reason=data.get("override_reason") or "",
            initial_payment=initial_payment, request=request,
        )
        event = HotelEvent.objects.select_related("guest", "location", "created_by").get(pk=event.pk)
        return success_response(
            HotelEventDetailSerializer(event).data,
            message="Event registered." if created else "Event already registered.",
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class EventDetailView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("booking.manage", "payment.capture")
    require_any_capability = True

    def get(self, request, reference):
        event = HotelEvent.objects.select_related("guest", "location", "created_by").filter(reference=reference).first()
        if event is None:
            raise ValidationError({"detail": "Event not found."})
        services.update_event_status(event=event, actor=request.user, request=request)
        return success_response(HotelEventDetailSerializer(event).data)


class EventPaymentView(APIView):
    permission_classes = [HasCapability]
    required_capability = "payment.capture"

    def post(self, request, reference):
        event = HotelEvent.objects.filter(reference=reference).first()
        if event is None:
            raise ValidationError({"detail": "Event not found."})
        serializer = EventPaymentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        payment, created = services.record_event_payment(
            event=event, amount=data["amount"], method=data["method"],
            actor=request.user, external_reference=data.get("external_reference") or "",
            notes=data.get("notes") or "", idempotency_key=data.get("idempotency_key") or "",
            request=request,
        )
        return success_response(
            EventPaymentSerializer(payment).data,
            message="Payment recorded." if created else "Payment already recorded.",
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class EventCancelView(APIView):
    permission_classes = [HasCapability]
    required_capability = "payment.refund.request"

    def post(self, request, reference):
        event = HotelEvent.objects.filter(reference=reference).first()
        if event is None:
            raise ValidationError({"detail": "Event not found."})
        serializer = EventCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        event, _ = services.cancel_event(
            event=event, actor=request.user, reason=serializer.validated_data["reason"], request=request,
        )
        return success_response(HotelEventDetailSerializer(event).data, message="Event cancelled.")


class EventReceiptView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("booking.manage", "payment.capture")
    require_any_capability = True

    def get(self, request, reference):
        event = HotelEvent.objects.select_related("guest", "location", "created_by").filter(reference=reference).first()
        if event is None:
            raise ValidationError({"detail": "Event not found."})
        payments = event.payments.select_related("received_by").order_by("created_at")
        latest = payments.filter(status=EventPayment.Status.SUCCESS).order_by("-created_at").first()
        receipt = {
            "hotel": "J-ONE Hotel & Lodge",
            "event_reference": event.reference,
            "receipt_number": latest.reference if latest else event.reference,
            "guest_name": event.guest.full_name,
            "guest_phone": event.guest.phone,
            "event_location": event.location.name,
            "event_type": event.event_type,
            "event_date": str(event.event_date),
            "event_time": f"{event.start_time} – {event.end_time}",
            "total_charged": str(event.total_charged),
            "amount_paid": str(event.amount_paid),
            "outstanding_balance": str(event.outstanding_balance),
            "payment_method": latest.method if latest else "",
            "payment_status": event.payment_status,
            "event_status": event.status,
            "date_issued": event.updated_at.isoformat() if event.updated_at else "",
            "payments": EventPaymentSerializer(payments, many=True).data,
        }
        return success_response(receipt)
