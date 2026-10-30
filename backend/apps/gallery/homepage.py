"""Four singleton homepage slots backed by the existing gallery storage."""
from django.db import transaction
from django.core.files.base import ContentFile
from pathlib import Path
from uuid import uuid4
from io import BytesIO
from PIL import Image, ImageOps
from django.http import Http404
from rest_framework import serializers, status
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import IsManagerOrAdmin
from apps.core.storage import StorageUploadError, delete_quietly, verify_stored_upload
from apps.core.validators import validate_image_upload
from .models import GalleryItem
from .serializers import GalleryItemSerializer


class HomepageUploadSerializer(serializers.Serializer):
    image = serializers.ImageField(required=False)
    alt_text = serializers.CharField(max_length=200, required=False, allow_blank=True)

    def validate_image(self, value):
        validate_image_upload(value)
        return value


def optimized_image(upload):
    """Bound homepage bandwidth and preserve orientation without a new dependency."""
    upload.seek(0)
    with Image.open(upload) as source:
        image = ImageOps.exif_transpose(source)
        image.thumbnail((1600, 1000), Image.Resampling.LANCZOS)
        image = image.convert('RGBA' if 'A' in image.getbands() else 'RGB')
        output = BytesIO()
        image.save(output, format='WEBP', quality=82, method=4)
    upload.seek(0)
    return ContentFile(output.getvalue(), name=Path(upload.name).stem[:60] + '-' + uuid4().hex + '.webp')


class HomepagePublicView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        items = GalleryItem.objects.filter(homepage_section__isnull=False, is_active=True)
        response = Response({item.homepage_section: GalleryItemSerializer(item, context={'request': request}).data
                             for item in items})
        response["Cache-Control"] = "no-store"
        return response


class HomepageAdminView(APIView):
    permission_classes = [IsManagerOrAdmin]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def _section(self, section):
        if section not in GalleryItem.HomepageSection.values:
            raise Http404

    def get(self, request, section):
        self._section(section)
        item = GalleryItem.objects.filter(homepage_section=section).first()
        return Response(GalleryItemSerializer(item, context={'request': request}).data if item else None)

    def put(self, request, section):
        return self._save(request, section)

    def patch(self, request, section):
        return self._save(request, section)

    def _save(self, request, section):
        self._section(section)
        serializer = HomepageUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        image = values.get('image')
        with transaction.atomic():
            item = GalleryItem.objects.select_for_update().filter(homepage_section=section).first()
            if item is None and image is None:
                raise serializers.ValidationError({'image': 'Choose an image for this section.'})
            if item is None:
                item = GalleryItem(homepage_section=section, title=GalleryItem.HomepageSection(section).label)
            previous = item.image.name if item.pk else None
            if image is not None:
                image = optimized_image(image)
                # Write and read back before changing the DB reference. Old image
                # stays live if storage rejects the replacement.
                try:
                    name = item.image.storage.save(item.image.field.generate_filename(item, image.name), image)
                    candidate = item.image.storage
                    if not candidate.exists(name):
                        raise StorageUploadError()
                    with candidate.open(name, 'rb') as fh:
                        if not fh.read(1):
                            raise StorageUploadError()
                    item.image.name = name
                    valid, warning = verify_stored_upload(item.image)
                    if not valid:
                        raise StorageUploadError(warning)
                except Exception as exc:
                    if 'name' in locals():
                        try:
                            candidate.delete(name)
                        except Exception:
                            pass
                    if isinstance(exc, StorageUploadError):
                        raise
                    raise StorageUploadError() from exc
            if 'alt_text' in values:
                item.alt_text = values['alt_text']
            item.is_active = True
            try:
                item.save()
            except Exception:
                if image is not None:
                    delete_quietly(item.image)
                raise
        # Only delete the previous object once the new DB reference is committed.
        if image is not None and previous and previous != item.image.name:
            storage = item.image.storage
            def remove_old():
                try:
                    storage.delete(previous)
                except Exception:
                    pass  # The live replacement must not fail because orphan cleanup failed.
            transaction.on_commit(remove_old)
        return Response(GalleryItemSerializer(item, context={'request': request}).data,
                        status=status.HTTP_201_CREATED if not previous else status.HTTP_200_OK)

    def delete(self, request, section):
        self._section(section)
        item = GalleryItem.objects.filter(homepage_section=section).first()
        if item:
            field = item.image
            item.delete()
            transaction.on_commit(lambda: delete_quietly(field))
        return Response(status=status.HTTP_204_NO_CONTENT)
