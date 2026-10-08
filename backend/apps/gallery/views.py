# apps/gallery/views.py
from drf_spectacular.utils import extend_schema
from rest_framework import generics, viewsets
from rest_framework.permissions import AllowAny

from apps.core.permissions import IsStaffReadOnlyManagerWrite

from .models import GalleryItem
from .serializers import GalleryItemAdminSerializer, GalleryItemSerializer


@extend_schema(tags=["Gallery"], summary="Public gallery (filter by ?category=ROOMS)")
class GalleryPublicListView(generics.ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = GalleryItemSerializer

    def get_queryset(self):
        qs = GalleryItem.objects.filter(is_active=True)
        category = self.request.query_params.get("category")
        if category:
            qs = qs.filter(category=category.upper())
        return qs.order_by("display_order", "-created_at")

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page, many=True)
        response = self.get_paginated_response(serializer.data)
        response.data["data"] = {
            "categories": list(GalleryItem.Category.values),
            "items": response.data["data"],
        }
        return response


@extend_schema(tags=["Admin · Gallery"])
class GalleryAdminViewSet(viewsets.ModelViewSet):
    permission_classes = [IsStaffReadOnlyManagerWrite]
    serializer_class = GalleryItemAdminSerializer

    def get_queryset(self):
        qs = GalleryItem.objects.all().order_by("display_order", "-created_at")
        if category := self.request.query_params.get("category"):
            qs = qs.filter(category=category.upper())
        if active := self.request.query_params.get("is_active"):
            qs = qs.filter(is_active=active.lower() in ("1", "true", "yes"))
        return qs
