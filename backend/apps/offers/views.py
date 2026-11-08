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

#: Authored 404 detail for both state-change endpoints. It names the thing that
#: is missing and the identifier that was used, so "which record is gone" is
#: answerable from the screen, the log and the support call.
OFFER_NOT_FOUND_MESSAGE = (
    "This offer no longer exists, so its state cannot be changed. "
    "Refresh the offer list and try again."
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


def _positive_int_param(value, name: str):
    """Validate an id filter (``?offer=``, ``?guest=``) as a database id.

    Returns ``None`` when the parameter is absent (no filter), the parsed
    integer when it is a real id, and raises a 400 naming the parameter when it
    is not. Guessing — treating an unparseable value as "no results" — hides
    the caller's bug behind an empty list, which is how a broken identifier
    reached production looking like "there are no restricted guests".
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if not raw.isdigit() or int(raw) < 1:
        from rest_framework.exceptions import ValidationError

        raise ValidationError({name: ["Expected the numeric id of an existing record."]})
    return int(raw)


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
            # A specific, actionable sentence: the id that came back in the
            # response is no longer in the database (deleted, or the offer was
            # listed from another deployment). The frontend can now say which,
            # instead of the generic "the requested item could not be found".
            raise NotFound(OFFER_NOT_FOUND_MESSAGE)
        if offer.is_active:
            # Idempotent: an offer that is already ON is a completed request,
            # not an error. Answering 200 with the CURRENT state is what makes a
            # double tap (or two people at two terminals) safe — it can never
            # produce an inconsistent audit trail or a spurious failure toast.
            return success_response(
                _offer_state_payload(offer),
                message="This offer is already active.",
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
            raise NotFound(OFFER_NOT_FOUND_MESSAGE)
        if not offer.is_active:
            # Idempotent (see OfferActivateView.post): already OFF is success
            # with the current state, never a duplicate write or a new audit row.
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
        # Identifiers are DATABASE PRIMARY KEYS and nothing else: never an array
        # index, never a slug, never a title. A value that is not a positive
        # integer cannot identify a row, so it is answered with an honest 400
        # instead of an empty 200 — an empty list made a mistyped/undefined id
        # look like "this offer has no restricted guests", which is exactly how
        # the desk concluded the feature was broken.
        if (offer := _positive_int_param(params.get("offer"), "offer")) not in (None, ""):
            qs = qs.filter(offer_id=offer)
        if (guest := _positive_int_param(params.get("guest"), "guest")) not in (None, ""):
            qs = qs.filter(guest_id=guest)
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
            desired_active = serializer.validated_data.get("is_active", True)
            # Re-posting an EXISTING (offer, guest) pair updates that one row; it
            # never creates a second live restriction for the same guest and
            # never touches another offer's rows. An explicit is_active=False in
            # the request is honoured instead of being overwritten with True, so
            # a form that sends the field cannot silently re-impose a bar the
            # caller just lifted.
            existing.is_active = bool(desired_active)
            existing.reason = serializer.validated_data.get("reason", "") or ""
            if existing.is_active:
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
                changes={
                    "is_active": [str(was_active), str(existing.is_active)],
                    "reason": ["", existing.reason],
                } if was_active or existing.reason else {},
                metadata={
                    "offer": offer.title,
                    "offer_id": offer.pk,
                    "guest": guest.email,
                    "guest_id": guest.pk,
                    "reason": existing.reason,
                    "reactivated": not was_active,
                },
                request=self.request,
                summary=(
                    f"{guest.email} restricted from “{offer.title}”"
                    if existing.is_active
                    else f"{guest.email} restriction on “{offer.title}” cleared"
                ),
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
                "offer_id": instance.offer_id,
                "guest": instance.guest.email,
                "guest_id": instance.guest_id,
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
                    "offer_id": instance.offer_id,
                    "guest": instance.guest.email,
                    "guest_id": instance.guest_id,
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
