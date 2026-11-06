# apps/offers/views.py
from django.db.models import Count, Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import generics, viewsets
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from apps.core.permissions import IsStaffReadOnlyManagerWrite
from apps.core.responses import success_response

from .models import GuestDiscount, Offer, OfferGuestRestriction
from .serializers import (
    GuestDiscountSerializer,
    OfferAdminSerializer,
    OfferGuestRestrictionSerializer,
    OfferPublicSerializer,
)


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


@extend_schema(tags=["Admin · Offers"], summary="Activate a promotional offer")
class OfferActivateView(APIView):
    """Switch a promotion ON.

    A dedicated endpoint (rather than a bare ``PATCH``) so the decision is a
    named, audited event with its own audit action, and so the dashboard can
    offer one obvious "Activate offer" control. Permissions mirror the rest of
    the offers administration: every staff role may read, only the manager tier
    may change the state.
    """

    permission_classes = [IsStaffReadOnlyManagerWrite]

    def post(self, request, pk):
        offer = Offer.objects.filter(pk=pk).first()
        if offer is None:
            raise NotFound("Offer not found.")
        if offer.is_active:
            return success_response(
                _offer_state_payload(offer), message="This offer is already active."
            )
        from apps.audit.services import log_action

        offer.is_active = True
        offer.save(update_fields=["is_active", "updated_at"])
        log_action(
            actor=request.user,
            action="OFFER_ACTIVATED",
            instance=offer,
            changes={"is_active": ["False", "True"]},
            metadata={"reference": offer.slug, "title": offer.title},
            request=request,
            summary=f"Offer activated: {offer.title}",
        )
        return success_response(
            _offer_state_payload(offer),
            message=f"Offer “{offer.title}” is active and will apply to eligible bookings.",
        )


@extend_schema(tags=["Admin · Offers"], summary="Deactivate a promotional offer")
class OfferDeactivateView(APIView):
    """Switch a promotion OFF without deleting it.

    Deactivation is the whole point of keeping the row: the offer stays in the
    database for historical bookings (which keep the price they were given) and
    for future re-activation, but every eligibility query filters on
    ``is_active`` — so from this moment a NEW booking is quoted at the
    configured room price instead of the promotional price.
    """

    permission_classes = [IsStaffReadOnlyManagerWrite]

    def post(self, request, pk):
        offer = Offer.objects.filter(pk=pk).first()
        if offer is None:
            raise NotFound("Offer not found.")
        if not offer.is_active:
            return success_response(
                _offer_state_payload(offer), message="This offer is already inactive."
            )
        from apps.audit.services import log_action

        offer.is_active = False
        offer.save(update_fields=["is_active", "updated_at"])
        log_action(
            actor=request.user,
            action="OFFER_DEACTIVATED",
            instance=offer,
            changes={"is_active": ["True", "False"]},
            metadata={"reference": offer.slug, "title": offer.title},
            request=request,
            summary=(
                f"Offer deactivated: {offer.title} — new bookings use the standard room price"
            ),
        )
        return success_response(
            _offer_state_payload(offer),
            message=(
                f"Offer “{offer.title}” is inactive. New bookings will use the "
                "standard room price; existing bookings keep the price they were given."
            ),
        )


def _offer_state_payload(offer):
    return {
        "id": offer.pk,
        "title": offer.title,
        "code": offer.code,
        "is_active": offer.is_active,
    }


@extend_schema(tags=["Admin · Offer restrictions"])
class OfferGuestRestrictionAdminViewSet(viewsets.ModelViewSet):
    """Guest-specific offer restrictions.

    Permissions mirror the rest of the offers administration exactly
    (``IsStaffReadOnlyManagerWrite``): the whole staff team may READ the list so
    the front desk can explain a price, but only managers and administrators
    may impose or lift a restriction.

    * ``GET  /api/admin/offer-restrictions/``      — list (``?offer=``, ``?guest=``,
      ``?is_active=``, ``?search=``)
    * ``POST /api/admin/offer-restrictions/``      — restrict a guest (idempotent per
      offer+guest: re-restricting lifts any previous restriction on that pair)
    * ``PATCH /api/admin/offer-restrictions/{id}/``— lift/restore or edit the reason
    * ``DELETE`` is deliberately NOT offered: lifting a restriction is a
      state change we keep (``is_active=False``) so the audit trail survives.
    """

    permission_classes = [IsStaffReadOnlyManagerWrite]
    serializer_class = OfferGuestRestrictionSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        qs = OfferGuestRestriction.objects.select_related(
            "offer", "guest", "created_by", "removed_by"
        ).order_by("-is_active", "-created_at")
        params = self.request.query_params
        if offer := params.get("offer"):
            qs = qs.filter(offer_id=offer) if str(offer).isdigit() else qs.none()
        if guest := params.get("guest"):
            qs = qs.filter(guest_id=guest) if str(guest).isdigit() else qs.none()
        if active := params.get("is_active"):
            qs = qs.filter(is_active=active.lower() in ("1", "true", "yes"))
        if search := params.get("search"):
            qs = qs.filter(
                Q(guest__first_name__icontains=search)
                | Q(guest__last_name__icontains=search)
                | Q(guest__email__icontains=search)
                | Q(guest__phone__icontains=search)
                | Q(offer__title__icontains=search)
                | Q(offer__code__icontains=search)
                | Q(reason__icontains=search)
            )
        return qs

    def perform_create(self, serializer):
        from apps.audit.services import log_action

        offer = serializer.validated_data["offer"]
        guest = serializer.validated_data["guest"]
        # One live row per (offer, guest) is enforced by a database constraint.
        # Re-restricting the same pair is therefore an UPDATE of the existing
        # row (re-activating it and recording the new reason) rather than an
        # IntegrityError the desk would see as a failed save.
        existing = OfferGuestRestriction.objects.filter(offer=offer, guest=guest).first()
        if existing is not None:
            was_active = existing.is_active
            existing.is_active = True
            existing.reason = serializer.validated_data.get("reason", "") or ""
            existing.created_by = self.request.user
            existing.removed_by = None
            existing.removed_at = None
            existing.save(update_fields=[
                "is_active", "reason", "created_by", "removed_by", "removed_at",
                "updated_at",
            ])
            log_action(
                actor=self.request.user,
                action="OFFER_GUEST_RESTRICTED",
                instance=existing,
                changes={"is_active": [str(was_active), "True"]},
                metadata={
                    "offer": offer.title,
                    "guest": guest.email,
                    "reason": existing.reason,
                    "reactivated": not was_active,
                },
                request=self.request,
                summary=f"{guest.email} restricted from “{offer.title}”",
            )
            serializer.instance = existing
            return existing
        instance = serializer.save(created_by=self.request.user)
        log_action(
            actor=self.request.user,
            action="OFFER_GUEST_RESTRICTED",
            instance=instance,
            metadata={
                "offer": instance.offer.title,
                "guest": instance.guest.email,
                "reason": instance.reason,
            },
            request=self.request,
            summary=f"{instance.guest.email} restricted from “{instance.offer.title}”",
        )
        return instance

    def perform_update(self, serializer):
        from apps.audit.services import log_action
        from django.utils import timezone as _tz

        before_active = serializer.instance.is_active
        before_reason = serializer.instance.reason
        instance = serializer.save()
        # Lifting a restriction is a recorded decision, not a wipe: who removed
        # it and when are kept on the row that stays in the database.
        if before_active and not instance.is_active:
            instance.removed_by = self.request.user
            instance.removed_at = _tz.now()
            instance.save(update_fields=["removed_by", "removed_at", "updated_at"])
        if not before_active and instance.is_active:
            instance.removed_by = None
            instance.removed_at = None
            instance.save(update_fields=["removed_by", "removed_at", "updated_at"])
        changes = {}
        if before_active != instance.is_active:
            changes["is_active"] = [str(before_active), str(instance.is_active)]
        if before_reason != instance.reason:
            changes["reason"] = [before_reason, instance.reason]
        if changes:
            log_action(
                actor=self.request.user,
                action="OFFER_GUEST_RESTRICTION_UPDATED",
                instance=instance,
                changes=changes,
                metadata={
                    "offer": instance.offer.title,
                    "guest": instance.guest.email,
                },
                request=self.request,
                summary=(
                    f"Restriction on {instance.guest.email} for “{instance.offer.title}” "
                    + ("lifted" if not instance.is_active else "restored")
                ),
            )
        return instance


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
