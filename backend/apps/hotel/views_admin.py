# apps/hotel/views_admin.py
"""Staff hotel content management: settings, policies, facilities."""
import logging

from django.core.cache import cache
from drf_spectacular.utils import extend_schema
from rest_framework import generics, viewsets
from rest_framework.response import Response

from apps.audit.services import log_action
from apps.core.permissions import IsManagerOrAdmin, IsStaffReadOnlyManagerWrite
from apps.core.responses import success_response

from .models import Facility, HotelPolicy, HotelSettings
from .serializers import FacilitySerializer, HotelPolicySerializer, HotelSettingsAdminSerializer
from .views import FACILITIES_CACHE_KEY, PUBLIC_HOTEL_CACHE_KEY

logger = logging.getLogger("apps")


@extend_schema(tags=["Admin · Settings"])
class HotelSettingsAdminView(generics.RetrieveUpdateAPIView):
    """Hotel settings are ADMIN-only — read AND write. Non-admin staff never
    see protected business rules; public hotel facts come from /api/hotel/."""

    serializer_class = HotelSettingsAdminSerializer
    http_method_names = ["get", "patch", "put", "head", "options"]

    def get_object(self):
        return HotelSettings.get_settings()

    def get_permissions(self):
        from apps.core.permissions import IsAdminRole

        return [IsAdminRole()]

    def retrieve(self, request, *args, **kwargs):
        return success_response(self.get_serializer(self.get_object()).data)

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        before = {f: getattr(instance, f) for f in self.get_serializer_class().Meta.fields if f != "updated_at"}
        partial = request.method == "PATCH"
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        instance = serializer.save()
        cache.delete(PUBLIC_HOTEL_CACHE_KEY)
        changes = {
            f: [str(before[f]), str(getattr(instance, f))]
            for f in before
            if str(before[f]) != str(getattr(instance, f))
        }
        if changes:
            log_action(actor=request.user, action="SETTINGS_CHANGED", instance=instance,
                       changes=changes, request=request)
            logger.info("Hotel settings changed by user %s: %s", request.user.id, list(changes))
        return success_response(self.get_serializer(instance).data, message="Settings updated.")


class _CacheInvalidatingViewSet(viewsets.ModelViewSet):
    cache_keys = ()

    def _invalidate(self):
        for key in self.cache_keys:
            cache.delete(key)

    def perform_create(self, serializer):
        instance = serializer.save()
        self._invalidate()
        log_action(
            actor=self.request.user,
            action=f"{instance._meta.model_name.upper()}_CREATED",
            instance=instance,
            request=self.request,
        )

    def perform_update(self, serializer):
        instance = serializer.save()
        self._invalidate()
        log_action(
            actor=self.request.user,
            action=f"{instance._meta.model_name.upper()}_UPDATED",
            instance=instance,
            request=self.request,
        )

    def perform_destroy(self, instance):
        log_action(
            actor=self.request.user,
            action=f"{instance._meta.model_name.upper()}_DELETED",
            instance=instance,
            request=self.request,
        )
        self._invalidate()
        instance.delete()


@extend_schema(tags=["Admin · Facilities"])
class FacilityAdminViewSet(_CacheInvalidatingViewSet):
    permission_classes = [IsStaffReadOnlyManagerWrite]
    serializer_class = FacilitySerializer
    queryset = Facility.objects.all().order_by("display_order", "name")
    pagination_class = None
    cache_keys = (FACILITIES_CACHE_KEY,)


@extend_schema(tags=["Admin · Policies"])
class PolicyAdminViewSet(_CacheInvalidatingViewSet):
    permission_classes = [IsStaffReadOnlyManagerWrite]
    serializer_class = HotelPolicySerializer
    queryset = HotelPolicy.objects.all().order_by("display_order", "title")
    pagination_class = None
