# apps/audit/views.py
"""Read-only audit log access (ADMIN only). No write/delete endpoints exist."""
from drf_spectacular.utils import extend_schema
from rest_framework import generics
from rest_framework.views import APIView

from apps.core.permissions import IsAdminRole
from apps.core.responses import success_response

from .models import AuditLog
from .serializers import AuditLogSerializer


@extend_schema(tags=["Admin · Audit"])
class AuditLogListView(generics.ListAPIView):
    permission_classes = [IsAdminRole]
    serializer_class = AuditLogSerializer

    def get_queryset(self):
        qs = AuditLog.objects.select_related("actor")
        params = self.request.query_params
        if action := params.get("action"):
            qs = qs.filter(action=action)
        if object_type := params.get("object_type"):
            qs = qs.filter(object_type=object_type)
        if actor := params.get("actor"):
            qs = qs.filter(actor__email__icontains=actor)
        if date_from := params.get("date_from"):
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to := params.get("date_to"):
            qs = qs.filter(created_at__date__lte=date_to)
        return qs.order_by("-created_at")


@extend_schema(tags=["Admin · Audit"])
class AuditLogDetailView(generics.RetrieveAPIView):
    permission_classes = [IsAdminRole]
    serializer_class = AuditLogSerializer
    queryset = AuditLog.objects.select_related("actor")


def humanise_action(action: str) -> str:
    """"BOOKING_RESCHEDULED" -> "Booking rescheduled"."""
    return action.replace("_", " ").capitalize() if action else ""


@extend_schema(
    tags=["Admin · Audit"],
    summary="Action types present in the audit log",
    description=(
        "Distinct action values that have actually been recorded, so the "
        "dashboard filter always matches what the backend logs instead of a "
        "hand-maintained list that silently goes stale when new actions are added."
    ),
)
class AuditLogActionListView(APIView):
    permission_classes = [IsAdminRole]

    def get(self, request):
        actions = (
            AuditLog.objects.order_by("action")
            .values_list("action", flat=True)
            .distinct()
        )
        return success_response([
            {"value": action, "label": humanise_action(action)}
            for action in actions if action
        ])
