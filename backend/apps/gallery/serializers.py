# apps/gallery/serializers.py
from rest_framework import serializers

from apps.core.storage import StorageUploadError, absolute_media_url, save_upload, verify_stored_upload

from .models import GalleryItem


class GalleryItemSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = GalleryItem
        fields = [
            "id", "title", "description", "category", "alt_text", "homepage_section",
            "image_url", "display_order", "is_active", "created_at",
        ]
        read_only_fields = ["id", "created_at", "homepage_section"]

    def get_image_url(self, obj):
        return absolute_media_url(obj.image, self.context.get("request"))


class GalleryItemAdminSerializer(GalleryItemSerializer):
    class Meta(GalleryItemSerializer.Meta):
        fields = GalleryItemSerializer.Meta.fields + ["image"]
        extra_kwargs = {"image": {"write_only": True, "required": False}}

    def create(self, validated_data):
        # A storage rejection must surface as an error, never as a gallery row
        # that points at an object the bucket never received.
        instance = save_upload(
            lambda: super(GalleryItemAdminSerializer, self).create(validated_data),
            context="gallery image",
        )
        return self._verify(instance)

    def update(self, instance, validated_data):
        instance = save_upload(
            lambda: super(GalleryItemAdminSerializer, self).update(instance, validated_data),
            context="gallery image",
        )
        return self._verify(instance)

    def _verify(self, instance):
        if "image" not in self.initial_data:
            return instance
        stored, warning = verify_stored_upload(instance.image)
        if not stored:
            # Remove the unusable row/object so the next upload starts clean.
            instance.image.delete(save=False)
            instance.delete()
            raise StorageUploadError(warning)
        return instance
