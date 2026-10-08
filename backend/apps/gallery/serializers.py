# apps/gallery/serializers.py
from rest_framework import serializers

from apps.core.storage import absolute_media_url

from .models import GalleryItem


class GalleryItemSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = GalleryItem
        fields = [
            "id", "title", "description", "category", "alt_text",
            "image_url", "display_order", "is_active", "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def get_image_url(self, obj):
        return absolute_media_url(obj.image, self.context.get("request"))


class GalleryItemAdminSerializer(GalleryItemSerializer):
    class Meta(GalleryItemSerializer.Meta):
        fields = GalleryItemSerializer.Meta.fields + ["image"]
        extra_kwargs = {"image": {"write_only": True, "required": False}}
