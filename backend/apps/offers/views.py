# apps/offers/views.py
from django.db.models import Count, Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import generics, viewsets
from rest_framework.permissions import AllowAny

from apps.core.permissions import IsStaffReadOnlyManagerWrite

from .models import GuestDiscount, Offer
from .serializers import GuestDiscountSerializer, OfferAdminSerializer, OfferPublicSerializer


@extend_schema(tags=["Offers"], summary="Currently running offers")
class OfferPublicListView(generics.ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = OfferPublicSerializer
    pagination_class = None

    def get_queryset(self):
        # Upcoming offers are listed too: the page shows each offer's validity
        # window and criteria, so a guest can plan a stay around one that has
        # not started. Only ended or deactivated offers are hidden.
        today = timezone.localdate()
        return (
            Offer.objects.filter(is_active=True, end_date__gte=today)
            .prefetch_related("room_types")
            .order_by("-is_featured", "-start_date", "title")
        )


@extend_schema(tags=["Admin · Offers"])
class OfferAdminViewSet(viewsets.ModelViewSet):
    permission_classes = [IsStaffReadOnlyManagerWrite]
    serializer_class = OfferAdminSerializer

    def get_queryset(self):
        qs = Offer.objects.prefetch_related("room_types").order_by("-is_featured", "-start_date")
        if active := self.request.query_params.get("is_active"):
            qs = qs.filter(is_active=active.lower() in ("1", "true", "yes"))
        return qs

    def perform_create(self, serializer):
        from apps.audit.services import log_action

        instance = serializer.save()
        log_action(actor=self.request.user, action="OFFER_CREATED", instance=instance,
                   request=self.request)

    def perform_update(self, serializer):
        from apps.audit.services import log_action

        before = {
            f: str(getattr(serializer.instance, f))
            for f in ("is_active", "discount_type", "discount_value", "start_date", "end_date")
        }
        instance = serializer.save()
        changes = {
            f: [before[f], str(getattr(instance, f))]
            for f in before
            if before[f] != str(getattr(instance, f))
        }
        log_action(actor=self.request.user, action="OFFER_UPDATED", instance=instance,
                   changes=changes, request=self.request)


@extend_schema(tags=["Admin · Guest discounts"])
class GuestDiscountAdminViewSet(viewsets.ModelViewSet):
    """Individual guest discounts.

    Permissions mirror offers exactly (IsStaffReadOnlyManagerWrite): every staff
    member may READ a guest's discount so the front desk can explain a price,
    but only managers/administrators may create, change or deactivate one.
    Receptionist permissions are unchanged.
    """

    permission_classes = [IsStaffReadOnlyManagerWrite]
    serializer_class = GuestDiscountSerializer

    def get_queryset(self):
        qs = (
            GuestDiscount.objects.select_related("guest", "created_by")
            .annotate(applications_count=Count("applications", distinct=True))
            .order_by("-created_at")
        )
        params = self.request.query_params
        if guest := params.get("guest"):
            qs = qs.filter(guest_id=guest) if str(guest).isdigit() else qs.none()
        if active := params.get("is_active"):
            qs = qs.filter(is_active=active.lower() in ("1", "true", "yes"))
        if params.get("valid_now", "").lower() in ("1", "true", "yes"):
            today = timezone.localdate()
            qs = qs.filter(is_active=True, start_date__lte=today).filter(
                Q(end_date__isnull=True) | Q(end_date__gte=today)
            )
        if search := params.get("search"):
            qs = qs.filter(
                Q(guest__first_name__icontains=search)
                | Q(guest__last_name__icontains=search)
                | Q(guest__email__icontains=search)
                | Q(guest__phone__icontains=search)
                | Q(reason__icontains=search)
            )
        return qs

    def perform_create(self, serializer):
        from apps.audit.services import log_action

        instance = serializer.save(created_by=self.request.user)
        log_action(
            actor=self.request.user,
            action="GUEST_DISCOUNT_CREATED",
            instance=instance,
            metadata={
                "guest": instance.guest.email,
                "discount_type": instance.discount_type,
                "discount_value": str(instance.discount_value),
                "start_date": str(instance.start_date),
                "end_date": str(instance.end_date or ""),
                "reason": instance.reason,
            },
            request=self.request,
            summary=f"Guest discount created for {instance.guest.email}",
        )

    def perform_update(self, serializer):
        from apps.audit.services import log_action

        tracked = ("is_active", "discount_type", "discount_value", "start_date",
                   "end_date", "reason")
        before = {f: str(getattr(serializer.instance, f)) for f in tracked}
        instance = serializer.save()
        changes = {
            f: [before[f], str(getattr(instance, f))]
            for f in tracked
            if before[f] != str(getattr(instance, f))
        }
        log_action(
            actor=self.request.user,
            action="GUEST_DISCOUNT_UPDATED",
            instance=instance,
            changes=changes,
            metadata={"guest": instance.guest.email},
            request=self.request,
            summary=f"Guest discount updated for {instance.guest.email}",
        )

    def perform_destroy(self, instance):
        """Deactivate rather than delete.

        Historical bookings reference this row through their immutable
        application snapshot; deactivating preserves the audit trail while
        stopping all future use.
        """
        from apps.audit.services import log_action

        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])
        log_action(
            actor=self.request.user,
            action="GUEST_DISCOUNT_DEACTIVATED",
            instance=instance,
            metadata={"guest": instance.guest.email},
            request=self.request,
            summary=f"Guest discount deactivated for {instance.guest.email}",
        )
