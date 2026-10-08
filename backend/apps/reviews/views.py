# apps/reviews/views.py
"""Review endpoints.

Public (guest-facing, heavily throttled, verification-gated):
    POST /api/reviews/verify/   -> safe eligible-stay summary
    POST /api/reviews/          -> submit the review

Administrator-only management (the project's top role — role == ADMIN — is
the "super admin"; managers and receptionists are rejected server-side):
    GET    /api/admin/reviews/            list + search/filter/sort (paginated)
    GET    /api/admin/reviews/stats/      aggregate statistics
    GET    /api/admin/reviews/{id}/       detail
    PATCH  /api/admin/reviews/{id}/       status / internal notes
    DELETE /api/admin/reviews/{id}/       remove a review (audited)

There is deliberately NO public listing endpoint: submitted reviews are never
displayed on the website.
"""
import logging

from django.db.models import Avg, Count, Q
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.permissions import AllowAny
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.core.permissions import IsAdminRole
from apps.core.responses import success_response

from . import services
from .models import Review
from .serializers import (
    EligibleStaySerializer,
    ReviewAdminSerializer,
    ReviewSubmitSerializer,
    ReviewVerifySerializer,
)

logger = logging.getLogger("apps")


# ---------------------------------------------------------------------------
# Public guest flow
# ---------------------------------------------------------------------------
@extend_schema(tags=["Reviews"], summary="Verify a completed stay for review eligibility")
class ReviewVerifyView(APIView):
    """POST so the reference/email pair never lands in server access logs."""

    permission_classes = [AllowAny]
    serializer_class = ReviewVerifySerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "review_verify"

    def post(self, request):
        serializer = ReviewVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        booking = services.get_verified_booking(
            booking_reference=serializer.validated_data["booking_reference"],
            email=serializer.validated_data.get("email", ""),
            request=request,
        )
        return success_response(
            EligibleStaySerializer(services.eligible_stay_payload(booking)).data
        )


@extend_schema(tags=["Reviews"], summary="Submit a review for a completed stay")
class ReviewSubmitView(APIView):
    permission_classes = [AllowAny]
    serializer_class = ReviewSubmitSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "review_submit"

    def post(self, request):
        serializer = ReviewSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if serializer.is_honeypot_filled():
            # Silently "accept" spam: no row, no signal to the bot.
            logger.info("Honeypot review dropped from %s", request.META.get("REMOTE_ADDR"))
            return success_response(
                message="Thank you for your feedback.", status=status.HTTP_201_CREATED
            )
        booking = services.get_verified_booking(
            booking_reference=serializer.validated_data["booking_reference"],
            email=serializer.validated_data.get("email", ""),
            request=request,
        )
        review = services.create_review(
            booking=booking,
            rating=serializer.validated_data["rating"],
            comment=serializer.validated_data["comment"],
            request=request,
        )
        return success_response(
            {"rating": review.rating, "booking_reference": booking.booking_reference},
            message="Thank you for your feedback.",
            status=status.HTTP_201_CREATED,
        )


# ---------------------------------------------------------------------------
# Administrator-only management
# ---------------------------------------------------------------------------
_ADMIN_QS = Review.objects.select_related(
    "booking", "booking__room_type", "guest", "reviewed_by"
)

_ORDERINGS = {
    "newest": "-created_at",
    "oldest": "created_at",
    "highest": "-rating",
    "lowest": "rating",
}


@extend_schema(tags=["Admin · Reviews"], summary="List customer reviews (administrator only)")
class ReviewAdminListView(generics.ListAPIView):
    permission_classes = [IsAdminRole]
    serializer_class = ReviewAdminSerializer

    def get_queryset(self):
        qs = _ADMIN_QS
        params = self.request.query_params
        if rating := params.get("rating"):
            if str(rating).isdigit():
                qs = qs.filter(rating=int(rating))
        if status_param := params.get("status"):
            qs = qs.filter(status=status_param.upper())
        if date_from := params.get("date_from"):
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to := params.get("date_to"):
            qs = qs.filter(created_at__date__lte=date_to)
        if search := params.get("search"):
            qs = qs.filter(
                Q(guest_name__icontains=search)
                | Q(guest__email__icontains=search)
                | Q(guest__phone__icontains=search)
                | Q(booking__booking_reference__icontains=search)
                | Q(comment__icontains=search)
            )
        ordering = _ORDERINGS.get(params.get("ordering", "newest"), "-created_at")
        # Stable secondary sort for equal ratings.
        return qs.order_by(ordering, "-created_at" if ordering not in ("-created_at", "created_at") else ordering)


@extend_schema(tags=["Admin · Reviews"], summary="Review statistics (administrator only)")
class ReviewStatsView(APIView):
    permission_classes = [IsAdminRole]
    serializer_class = ReviewAdminSerializer  # schema placeholder

    def get(self, request):
        # One aggregate query — never load every row to count in Python.
        agg = Review.objects.aggregate(
            total=Count("id"),
            average=Avg("rating"),
            new_count=Count("id", filter=Q(status=Review.Status.NEW)),
            r5=Count("id", filter=Q(rating=5)),
            r4=Count("id", filter=Q(rating=4)),
            r3=Count("id", filter=Q(rating=3)),
            r2=Count("id", filter=Q(rating=2)),
            r1=Count("id", filter=Q(rating=1)),
        )
        return success_response({
            "total": agg["total"],
            # null (not a fake 0.0) when no reviews exist.
            "average_rating": round(agg["average"], 2) if agg["average"] is not None else None,
            "new_count": agg["new_count"],
            "by_rating": {"5": agg["r5"], "4": agg["r4"], "3": agg["r3"], "2": agg["r2"], "1": agg["r1"]},
        })


@extend_schema(tags=["Admin · Reviews"], summary="Review detail / update / delete (administrator only)")
class ReviewAdminDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAdminRole]
    serializer_class = ReviewAdminSerializer
    http_method_names = ["get", "patch", "delete", "head", "options"]
    queryset = _ADMIN_QS

    def retrieve(self, request, *args, **kwargs):
        return success_response(self.get_serializer(self.get_object()).data)

    def partial_update(self, request, *args, **kwargs):
        review = self.get_object()
        serializer = self.get_serializer(review, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        review = services.mark_review_handled(
            review,
            staff_user=request.user,
            status=serializer.validated_data.get("status"),
            internal_notes=serializer.validated_data.get("internal_notes"),
            request=request,
        )
        return success_response(self.get_serializer(review).data, message="Review updated.")

    def destroy(self, request, *args, **kwargs):
        review = self.get_object()
        services.delete_review(review, staff_user=request.user, request=request)
        return success_response(message="Review deleted.", status=status.HTTP_200_OK)
