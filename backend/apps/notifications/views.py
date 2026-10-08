# apps/notifications/views.py
from django.db.models import Q
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.core.pagination import StandardPagination
from apps.core.permissions import IsStaffRole
from apps.core.responses import success_response

from .models import EmailLog, Notification
from .serializers import (
    EmailLogSerializer,
    NotificationDetailSerializer,
    NotificationSerializer,
    UnreadCountSerializer,
)
from .services import unread_count


@extend_schema(tags=["Notifications"], summary="List my notifications (paginated)")
class NotificationListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = NotificationSerializer

    def get_queryset(self):
        qs = Notification.objects.filter(recipient=self.request.user)
        if self.request.query_params.get("unread") in ("1", "true"):
            qs = qs.filter(is_read=False)
        return qs.order_by("-created_at")

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page, many=True)
        response = self.get_paginated_response(serializer.data)
        response.data["data"] = {"unread_count": unread_count(request.user), "notifications": response.data["data"]}
        return response


@extend_schema(tags=["Notifications"], summary="Read one notification (owns only)")
class NotificationDetailView(generics.RetrieveAPIView):
    """Full notification for `notification-details.html`.

    A user can only ever read their OWN notification — the queryset is scoped
    to the recipient, so someone else's id is a 404, never a leak. Opening the
    detail marks it read (the response carries the fresh unread count so the
    sidebar badge can update without another request).
    """

    permission_classes = [IsAuthenticated]
    serializer_class = NotificationDetailSerializer

    def get_queryset(self):
        return Notification.objects.filter(recipient=self.request.user)

    def retrieve(self, request, *args, **kwargs):
        notification = self.get_object()
        data = self.get_serializer(notification).data
        if not notification.is_read:
            Notification.objects.filter(pk=notification.pk, is_read=False).update(is_read=True)
            data["is_read"] = True
        data["unread_count"] = unread_count(request.user)
        return success_response(data)


@extend_schema(tags=["Notifications"], summary="Unread notification count")
class UnreadCountView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = UnreadCountSerializer

    def get(self, request):
        return success_response({"unread_count": unread_count(request.user)})


@extend_schema(tags=["Notifications"], summary="Mark one notification as read")
class MarkReadView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = UnreadCountSerializer

    def post(self, request, pk):
        updated = Notification.objects.filter(recipient=request.user, pk=pk, is_read=False).update(is_read=True)
        if not updated and not Notification.objects.filter(recipient=request.user, pk=pk).exists():
            from rest_framework.exceptions import NotFound

            raise NotFound()
        return success_response({"unread_count": unread_count(request.user)}, message="Notification marked as read.")


@extend_schema(tags=["Notifications"], summary="Mark all notifications as read")
class MarkAllReadView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = UnreadCountSerializer

    def post(self, request):
        Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True)
        return success_response({"unread_count": 0}, message="All notifications marked as read.")


@extend_schema(tags=["Notifications"], summary="Email delivery log (staff/admin)")
class EmailLogListView(generics.ListAPIView):
    """Staff visibility into transactional email delivery.

    Supports ``?status=FAILED`` and ``?booking=<ref>`` filters so staff can see
    exactly whether a receipt/confirmation truly SENT or FAILED — the whole
    point of the fix: the dashboard can now show real delivery state instead of
    assuming a queued task succeeded.
    """

    permission_classes = [IsStaffRole]
    serializer_class = EmailLogSerializer
    # Explicit — this list is unbounded and staff-filterable, so it pages with
    # the same StandardPagination contract as every other admin list.
    pagination_class = StandardPagination

    def get_queryset(self):
        # Delivery is synchronous, so nothing can legitimately stay
        # PENDING/SENDING beyond one request: resolve interrupted rows to
        # FAILED before listing, so staff always see a truthful terminal state
        # instead of a phantom "sending" that no worker will ever finish.
        EmailLog.resolve_stale()
        qs = EmailLog.objects.all()
        status_param = (self.request.query_params.get("status") or "").upper()
        if status_param in EmailLog.Status.values:
            qs = qs.filter(status=status_param)
        kind_param = (self.request.query_params.get("kind") or "").upper()
        if kind_param in EmailLog.Kind.values:
            qs = qs.filter(kind=kind_param)
        booking = (self.request.query_params.get("booking") or "").strip()
        if booking:
            qs = qs.filter(booking_reference=booking)
        return qs.order_by("-created_at")


@extend_schema(tags=["Notifications"], summary="Email delivery status (staff/admin)")
class EmailLogDetailView(generics.RetrieveAPIView):
    """Read a single email's recorded delivery status (SENT / FAILED)."""

    permission_classes = [IsStaffRole]
    serializer_class = EmailLogSerializer

    def get_queryset(self):
        return EmailLog.objects.all()

    def retrieve(self, request, *args, **kwargs):
        return success_response(self.get_serializer(self.get_object()).data)
