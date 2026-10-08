# apps/hotel/views.py
"""Public hotel content endpoints (no authentication required)."""
from django.core.cache import cache
from drf_spectacular.utils import extend_schema
from rest_framework import generics
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from apps.core.responses import success_response

from .models import Facility, HotelPolicy, HotelSettings
from .serializers import FacilitySerializer, HotelPolicySerializer, HotelPublicSerializer

PUBLIC_HOTEL_CACHE_KEY = "hotel:public:payload"
FACILITIES_CACHE_KEY = "hotel:facilities:list"


@extend_schema(tags=["Hotel"], summary="Public hotel information")
class HotelInfoView(APIView):
    permission_classes = [AllowAny]
    serializer_class = HotelPublicSerializer

    def get(self, request):
        payload = cache.get(PUBLIC_HOTEL_CACHE_KEY)
        if payload is None:
            payload = HotelPublicSerializer(
                HotelSettings.get_settings(), context={"request": request}
            ).data
            cache.set(PUBLIC_HOTEL_CACHE_KEY, payload, 300)
        return success_response(payload)


@extend_schema(tags=["Hotel"], summary="Active hotel policies")
class PolicyListView(generics.ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = HotelPolicySerializer
    pagination_class = None

    def get_queryset(self):
        return HotelPolicy.objects.filter(is_active=True).order_by("display_order", "title")


@extend_schema(tags=["Hotel"], summary="Active hotel facilities")
class FacilityListView(generics.ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = FacilitySerializer
    pagination_class = None

    def get_queryset(self):
        return Facility.objects.filter(is_active=True).order_by("display_order", "name")
