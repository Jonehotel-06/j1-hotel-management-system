"""Independent TV gallery API. Public content is explicitly opted in."""
import os
import re
import logging

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.db import transaction, DatabaseError
from django.core.exceptions import ImproperlyConfigured, ValidationError as DjangoValidationError
from datetime import timedelta
from uuid import uuid4
from django.utils import timezone
from django.urls import reverse
from rest_framework import serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import IsManagerOrAdmin
from apps.audit.services import log_action
from apps.rooms.models import Room
from . import media, storage
from .models import GalleryMedia, GalleryRoom, GallerySettings
from .tasks import queue_video, dispatch_video

logger = logging.getLogger(__name__)


class PublicView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []


def public_url(request, item, rendition):
    return request.build_absolute_uri(reverse('tv-gallery-public-file', args=[item.pk, rendition]))


class Catalog(PublicView):
    def get(self, request):
        config = GallerySettings.objects.filter(pk=1).select_related('music').first()
        visible = GalleryRoom.objects.filter(active=True, room__is_active=True, room__room_type__is_active=True).select_related('room__room_type').order_by('room__room_number')
        rooms = list(visible)
        grouped = {entry.room_id: [] for entry in rooms}
        for item in GalleryMedia.objects.filter(room_id__in=grouped, active=True, processing_status='ready', kind__in=['image', 'video']).order_by('-is_cover', 'display_order', 'id'):
            rendition = 'display' if item.kind == 'image' else 'file'
            grouped[item.room_id].append({'id': item.pk, 'kind': item.kind, 'url': public_url(request, item, rendition),
                                          'thumbnail': public_url(request, item, 'thumb') if item.thumbnail_key else None})
        payload = [{'id': entry.room_id, 'number': entry.room.room_number,
                    'type': entry.room.room_type.name, 'media': grouped[entry.room_id]} for entry in rooms]
        music = None
        if config and config.music_enabled and config.music and config.music.active and config.music.kind == 'audio' and config.music.room_id is None:
            music = public_url(request, config.music, 'file')
        return Response({'rooms': payload, 'slideshow_seconds': config.slideshow_seconds if config else 5,
                         'music_url': music, 'music_autoplay': config.music_autoplay if config else False,
                         'music_volume': config.music_volume if config else 35})


def _visible(item):
    if not item.active or item.processing_status != 'ready':
        return False
    if item.kind == 'audio' and item.room_id is None:
        return GallerySettings.objects.filter(pk=1, music=item, music_enabled=True).exists()
    return (item.kind in ('image', 'video') and item.room_id is not None and
            GalleryRoom.objects.filter(room_id=item.room_id, active=True, room__is_active=True,
                                       room__room_type__is_active=True).exists())


def _serve(request, item, rendition, private=False):
    if rendition == 'thumb' and item.kind in ('image', 'video'):
        key, mime = item.thumbnail_key, 'image/jpeg'
    elif rendition == 'display' and item.kind == 'image':
        key, mime = item.display_key, 'image/jpeg'
    elif rendition == 'file' and item.kind == 'video':
        key, mime = item.display_key, 'video/mp4'
    elif rendition == 'file' and (private or item.kind == 'audio'):
        key, mime = item.storage_key, item.content_type
        if item.kind in ('other', 'image'):
            mime = 'application/octet-stream'
    else:
        raise Http404
    try:
        path = storage.path_for(key)
        size = path.stat().st_size
        source = path.open('rb')
    except (ValueError, OSError):
        raise Http404
    # Single byte range support for normal HTML video/audio seeking. Invalid and
    # unsatisfiable ranges receive 416; never cache partial responses as full files.
    header = request.headers.get('Range', '')
    if header:
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', header.strip())
        if not match or (not match.group(1) and not match.group(2)):
            source.close()
            response = HttpResponse(status=416)
            response['Content-Range'] = 'bytes */%d' % size
            return response
        a, b = match.groups()
        if a:
            start = int(a); end = min(int(b), size - 1) if b else size - 1
        else:
            amount = int(b); start = max(0, size - amount); end = size - 1
        if start >= size or start > end:
            source.close()
            response = HttpResponse(status=416)
            response['Content-Range'] = 'bytes */%d' % size
            return response
        length = end - start + 1
        source.seek(start)
        def chunks():
            remaining = length
            try:
                while remaining:
                    part = source.read(min(64 * 1024, remaining))
                    if not part:
                        break
                    remaining -= len(part)
                    yield part
            finally:
                source.close()
        response = StreamingHttpResponse(chunks(), status=206, content_type=mime)
        response['Content-Range'] = 'bytes %d-%d/%d' % (start, end, size)
        response['Content-Length'] = str(length)
    else:
        response = FileResponse(source, content_type=mime)
        response['Content-Length'] = str(size)
    response['Accept-Ranges'] = 'bytes'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Content-Security-Policy'] = "default-src 'none'; sandbox"
    response['Cache-Control'] = 'private, no-store' if private else 'public, max-age=300'
    # Never let browsers render original uploaded files other than recognized AV.
    response['Content-Disposition'] = 'attachment; filename="download"' if private and rendition == 'file' and item.kind in ('other', 'image') else 'inline'
    return response


class PublicFile(PublicView):
    def get(self, request, pk, rendition):
        item = get_object_or_404(GalleryMedia, pk=pk)
        if not _visible(item):
            raise Http404
        return _serve(request, item, rendition)


class AdminView(APIView):
    permission_classes = [IsManagerOrAdmin]


def admin_media(item):
    return {'id': item.id, 'room_id': item.room_id, 'kind': item.kind, 'name': item.original_name,
            'size': item.size, 'active': item.active, 'is_cover': item.is_cover, 'order': item.display_order,
            'processing_status': item.processing_status, 'processing_error': item.processing_error}


class AdminRooms(AdminView):
    def get(self, request):
        rooms = Room.objects.select_related('room_type', 'tv_gallery').order_by('room_number')
        return Response([{'id': room.id, 'number': room.room_number, 'type': room.room_type.name,
                          'eligible': room.is_active and room.room_type.is_active,
                          'active': getattr(getattr(room, 'tv_gallery', None), 'active', False)} for room in rooms])

    def patch(self, request):
        s = RoomChange(data=request.data); s.is_valid(raise_exception=True)
        room = get_object_or_404(Room, pk=s.validated_data['room_id'])
        item, _ = GalleryRoom.objects.update_or_create(room=room, defaults={'active': s.validated_data['active']})
        log_action(actor=request.user, action='TV_GALLERY_ROOM_CHANGED', instance=item, metadata={'active': item.active}, request=request)
        return Response({'room_id': room.pk, 'active': item.active})


class RoomChange(serializers.Serializer):
    room_id = serializers.IntegerField(min_value=1)
    active = serializers.BooleanField()


class AdminMedia(AdminView):
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request):
        return Response([admin_media(x) for x in GalleryMedia.objects.all().order_by('room_id', 'display_order', 'id')])

    def post(self, request):
        upload = request.FILES.get('file')
        if upload is None:
            raise ValidationError({'file': 'Select a file.'})
        if not upload.size:
            raise ValidationError({'file': 'The selected file is empty (0 bytes).'})
        room_id = request.data.get('room_id')
        if room_id and (not str(room_id).isdigit() or int(room_id) < 1):
            raise ValidationError({'room_id': 'Provide a valid room ID.'})
        room = get_object_or_404(Room, pk=room_id) if room_id else None
        config = GallerySettings.load()
        # Hard upper bound independent of any setting or client declaration, and
        # never above the deployment ceiling (TV_GALLERY_MAX_UPLOAD_MB, 128 MB).
        deployment_ceiling = int(getattr(settings, 'TV_GALLERY_MAX_UPLOAD_MB', 128) or 128)
        effective_mb = min(int(config.max_upload_mb or deployment_ceiling), deployment_ceiling, 512)
        limit = effective_mb * 1024 * 1024
        if upload.size > limit:
            raise ValidationError({'file': 'File exceeds the maximum allowed size of %d MB.' % effective_mb})
        try:
            values = media.prepare(upload, limit)
        except storage.GalleryStorageUnavailable as exc:
            # A deployment/configuration problem is NOT the staff member's file
            # being invalid. Before this repair it was converted into HTTP 400
            # "Validation failed." for every upload, which is exactly the
            # message the team reported for both the JPG and the MP4.
            logger.error('Gallery storage is unavailable during upload: %s', exc)
            return self._storage_unavailable(exc)
        except ValueError as exc:
            raise ValidationError({'file': str(exc)}) from exc
        except (OSError, ImproperlyConfigured) as exc:
            logger.exception('Gallery storage error during upload')
            return self._storage_unavailable(exc)
        notice = values.pop('_notice', '')
        fingerprint = values.get('upload_fingerprint', '')

        # Retry safety: the same bytes for the same room is the same gallery
        # item. A lost response (or a double click on Retry) must never create a
        # second row or a second stored file.
        if room is not None and fingerprint:
            existing = GalleryMedia.objects.filter(
                room=room, upload_fingerprint=fingerprint, active=True
            ).order_by('id').first()
            if existing is not None:
                for key in (values.get('storage_key'), values.get('thumbnail_key'), values.get('display_key')):
                    try:
                        storage.remove(key)
                    except OSError:
                        pass
                logger.info('Duplicate gallery upload ignored (media %s, room %s)', existing.pk, room.pk)
                payload = admin_media(existing)
                payload['notice'] = 'This file is already in this room\'s gallery; it was not uploaded twice.'
                payload['duplicate'] = True
                return Response(payload, status=status.HTTP_200_OK)

        if values['kind'] == 'video':
            values.update(processing_status='queued', processing_token=uuid4().hex, processing_updated_at=timezone.now())
        try:
            item = GalleryMedia.objects.create(room=room, original_name=media.safe_name(upload.name),
                                               uploaded_by=request.user, **values)
        except DatabaseError as exc:
            logger.exception('Gallery database error during upload')
            for key in (values.get('storage_key'), values.get('thumbnail_key'), values.get('display_key')):
                try:
                    storage.remove(key)
                except OSError:
                    pass
            raise ValidationError({'file': 'Upload succeeded but media record could not be saved. Please try again.'}) from exc
        except Exception as exc:
            logger.exception('Gallery unexpected error during upload')
            for key in (values.get('storage_key'), values.get('thumbnail_key'), values.get('display_key')):
                try:
                    storage.remove(key)
                except OSError:
                    pass
            raise
        if item.kind == 'video':
            transaction.on_commit(lambda: dispatch_video(item))
            item.refresh_from_db()
        log_action(actor=request.user, action='TV_GALLERY_MEDIA_UPLOADED', instance=item, metadata={'kind': item.kind, 'size': item.size}, request=request)
        payload = admin_media(item)
        if notice: payload['notice'] = notice
        return Response(payload, status=status.HTTP_201_CREATED)

    @staticmethod
    def _storage_unavailable(exc):
        """HTTP 503 with a configuration code — never a fake validation error."""
        from rest_framework.response import Response
        message = getattr(exc, 'message', '') or (
            'TV gallery storage is unavailable. An administrator must configure '
            'TV_GALLERY_STORAGE_ROOT and restart the API.'
        )
        return Response(
            {
                'success': False,
                'code': 'GALLERY_STORAGE_UNAVAILABLE',
                'message': message,
                'errors': {'storage': [message]},
                'data': {'retryable': True},
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


class MediaChange(serializers.Serializer):
    active = serializers.BooleanField(required=False)
    is_cover = serializers.BooleanField(required=False)
    order = serializers.IntegerField(required=False, min_value=0, max_value=1000000)


class AdminMediaItem(AdminView):
    def patch(self, request, pk):
        s = MediaChange(data=request.data); s.is_valid(raise_exception=True)
        values = s.validated_data
        with transaction.atomic():
            # Lock the common room before changing a cover so concurrent writes
            # serialize. Lock the media row second for a consistent lock order.
            initial = get_object_or_404(GalleryMedia, pk=pk)
            if initial.room_id:
                Room.objects.select_for_update().get(pk=initial.room_id)
            item = get_object_or_404(GalleryMedia.objects.select_for_update(), pk=pk)
            if values.get('is_cover') and (item.kind != 'image' or not item.room_id):
                raise ValidationError({'is_cover': 'Only room images can be covers.'})
            if values.get('is_cover'):
                GalleryMedia.objects.filter(room_id=item.room_id, kind='image', is_cover=True).exclude(pk=item.pk).update(is_cover=False)
            for field in ('active', 'is_cover'):
                if field in values: setattr(item, field, values[field])
            if 'order' in values: item.display_order = values['order']
            item.save()
        log_action(actor=request.user, action='TV_GALLERY_MEDIA_CHANGED', instance=item, request=request)
        return Response(admin_media(item))

    def delete(self, request, pk):
        with transaction.atomic():
            item = get_object_or_404(GalleryMedia.objects.select_for_update(), pk=pk)
            keys = (item.storage_key, item.thumbnail_key, item.display_key)
            item_id = item.pk
            item.delete()
        failures = False
        for key in keys:
            try:
                storage.remove(key)
            except (OSError, ImproperlyConfigured):
                failures = True
        log_action(actor=request.user, action='TV_GALLERY_MEDIA_DELETED', metadata={'media_id': item_id, 'cleanup_failed': failures}, request=request)
        if failures:
            return Response({'deleted': True, 'warning': 'File cleanup incomplete; an operator should run the orphan cleanup command.'})
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminRetryVideo(AdminView):
    def post(self, request, pk):
        with transaction.atomic():
            item = get_object_or_404(GalleryMedia.objects.select_for_update(), pk=pk, kind='video')
            if item.processing_status == 'ready':
                raise ValidationError({'processing': 'This video is already ready.'})
            if item.processing_status in ('queued', 'processing') and item.processing_updated_at and item.processing_updated_at > timezone.now() - timedelta(minutes=10):
                raise ValidationError({'processing': 'Video is already queued or processing; retry after 10 minutes if stuck.'})
            queue_video(item)
            transaction.on_commit(lambda: dispatch_video(item))
        item.refresh_from_db()
        log_action(actor=request.user, action='TV_GALLERY_VIDEO_RETRY', instance=item, request=request)
        return Response(admin_media(item))


class SettingsChange(serializers.Serializer):
    slideshow_seconds = serializers.IntegerField(required=False, min_value=2, max_value=60)
    music_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    music_enabled = serializers.BooleanField(required=False)
    music_autoplay = serializers.BooleanField(required=False)
    music_volume = serializers.IntegerField(required=False, min_value=0, max_value=100)
    max_upload_mb = serializers.IntegerField(required=False, min_value=1, max_value=512)


class AdminSettings(AdminView):
    def get(self, request):
        return Response(self.serialize(GallerySettings.load()))

    def patch(self, request):
        s = SettingsChange(data=request.data); s.is_valid(raise_exception=True)
        values = s.validated_data
        config = GallerySettings.load()
        if 'music_id' in values:
            pk = values.pop('music_id')
            config.music = get_object_or_404(GalleryMedia, pk=pk, kind='audio', room__isnull=True) if pk else None
        for name, value in values.items(): setattr(config, name, value)
        config.save()
        log_action(actor=request.user, action='TV_GALLERY_SETTINGS_CHANGED', instance=config, request=request)
        return Response(self.serialize(config))

    @staticmethod
    def serialize(config):
        payload = {key: getattr(config, key) for key in ('slideshow_seconds', 'music_id', 'music_enabled', 'music_autoplay', 'music_volume', 'max_upload_mb')}
        # The API enforces the smaller of the stored setting and the deployment
        # ceiling; the dashboard shows this value so the two cannot disagree.
        ceiling = int(getattr(settings, 'TV_GALLERY_MAX_UPLOAD_MB', 128) or 128)
        payload['effective_max_upload_mb'] = min(int(config.max_upload_mb or ceiling), ceiling)
        payload['storage_ready'], payload['storage_detail'] = storage.is_configured()
        return payload


class AdminFile(AdminView):
    def get(self, request, pk, rendition):
        return _serve(request, get_object_or_404(GalleryMedia, pk=pk), rendition, private=True)
