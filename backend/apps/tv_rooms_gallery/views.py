"""Independent TV gallery API. Public content is explicitly opted in.

Repair note (2026-11-20) — why every rendition answered 404 in production
=========================================================================
``_serve()`` used a single ``except (ValueError, OSError): raise Http404``
around key resolution + stat + open. That is four different faults collapsed
into "this page does not exist":

  * the storage root is misconfigured / the volume is not mounted  (503)
  * the media row points at a file that is not there  (a deployment fault)
  * the file is there but unreadable by the API process  (a permissions fault)
  * the requested rendition does not apply to this media type  (404, correct)

Production returned 404 for media ids 1–9 — every image the catalog still
lists — so the desk could not tell "the gallery is empty" from "the gallery
files are unreachable", and no repair could be targeted. ``storage.resolve()``
now reports which of the four it is and each answers accordingly. The catalog
also marks a room's media as ``available: false`` when its files cannot be
resolved, so the TV screen rotates only over media that can actually play.
"""
import os
import re
import logging
import threading

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse, JsonResponse, StreamingHttpResponse
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
            available = _rendition_present(item, rendition)
            grouped[item.room_id].append({
                'id': item.pk, 'kind': item.kind,
                'url': public_url(request, item, rendition),
                'thumbnail': public_url(request, item, 'thumb') if item.thumbnail_key else None,
                # ``available`` is advisory for the TV client (it skips media it
                # cannot fetch instead of showing a black frame). The URL stays
                # in the payload so a repaired deployment needs no client
                # change to start serving it again.
                'available': available,
            })
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


def _rendition_key(item, rendition, private=False):
    """Map a rendition name to (key, content-type), or (None, None).

    ``None`` means "this rendition does not apply to this media type" (404 is
    the right answer); an empty key for an applicable rendition means the
    rendition was never produced — a different fault, reported as such.

    Repair (2026-11-26) — the staff preview could not show an image at all
    ---------------------------------------------------------------------
    The admin page asked for ``display`` and then ``file`` for an image. The
    ``display`` rendition is missing on every record stored before thumbnails
    existed (or whose rendition file is gone), and ``file`` is not a rendition
    an image has, so both requests failed and the panel said "Preview
    unavailable - open the file" while the ORIGINAL upload was sitting in
    storage, readable the whole time. ``original`` is now a real, staff-only
    rendition: the public/TV routes still never serve an un-re-encoded upload.
    """
    if rendition == 'thumb' and item.kind in ('image', 'video'):
        return item.thumbnail_key, 'image/jpeg'
    if rendition == 'display' and item.kind == 'image':
        return item.display_key, 'image/jpeg'
    if rendition == 'file' and item.kind == 'video':
        return item.display_key, 'video/mp4'
    if rendition == 'file' and (private or item.kind == 'audio'):
        mime = 'application/octet-stream' if item.kind in ('other', 'image') else item.content_type
        return item.storage_key, mime
    if rendition == 'original' and private:
        # Staff-only, read-only view of the bytes exactly as they were uploaded.
        # The content type was detected from the file's own magic bytes at
        # upload time, never from the client's declaration, and every response
        # carries nosniff + a sandbox CSP.
        mime = item.content_type or 'application/octet-stream'
        return item.storage_key, mime
    return None, None


def _rendition_present(item, rendition):
    """True when ``rendition`` resolves to a readable file for this item."""
    key, _ = _rendition_key(item, rendition)
    if not key:
        return False
    path, outcome = storage.resolve(key)
    return outcome == storage.FILE_OK and path is not None


#: Renditions the staff preview can rebuild from an image's stored original.
_REBUILDABLE = {'thumb': (640, 400, 80), 'display': (1920, 1080, 88)}
_rebuild_locks = {}
_rebuild_lock_guard = threading.Lock()


def _rebuild_lock(item_id):
    """One lock per media row so two preview requests cannot both encode it."""
    with _rebuild_lock_guard:
        lock = _rebuild_locks.get(item_id)
        if lock is None:
            lock = threading.Lock()
            _rebuild_locks[item_id] = lock
        return lock


def _rebuild_image_rendition(item, rendition):
    """Produce a missing thumb/display rendition from the stored original.

    Repair (2026-11-26): every image uploaded before the thumbnail pipeline (or
    whose rendition object was lost) had an empty ``thumbnail_key`` /
    ``display_key``, so both the card thumbnail and the preview answered with a
    storage fault. The ORIGINAL file was present and readable the whole time,
    and re-deriving a bounded JPEG from it repairs the record without touching
    the original: ``storage_key`` is never modified, renamed or overwritten, and
    every previously stored object keeps its key, so no existing URL breaks.

    Returns the new key, or ``''`` when the rendition cannot be rebuilt (which
    leaves the honest storage-fault response in place).
    """
    from .thumbnails import make_thumbnail, ThumbnailError

    spec = _REBUILDABLE.get(rendition)
    if spec is None or item.kind != 'image':
        return ''
    path, outcome = storage.resolve(item.storage_key)
    if outcome != storage.FILE_OK or path is None:
        return ''
    try:
        data = path.read_bytes()
        rendered = make_thumbnail(data, size=(spec[0], spec[1]), quality=spec[2],
                                  max_bytes=max(getattr(settings, 'TV_GALLERY_MAX_IMAGE_BYTES', item.size), item.size),
                                  max_pixels=int(getattr(settings, 'TV_GALLERY_MAX_IMAGE_PIXELS', 200_000_000)))
    except (OSError, ThumbnailError, ValueError):
        logger.exception('TV gallery media %s: could not rebuild %s rendition', item.pk, rendition)
        return ''
    try:
        key, _size = storage.save_chunks([rendered], 16 * 1024 * 1024)
    except (OSError, ImproperlyConfigured, ValueError):
        logger.exception('TV gallery media %s: could not store rebuilt %s rendition', item.pk, rendition)
        return ''
    field = 'thumbnail_key' if rendition == 'thumb' else 'display_key'
    # Only this item's rendition fields are written, under the row lock, and the
    # old empty/stale key is recorded in the audit entry.
    with transaction.atomic():
        locked = GalleryMedia.objects.select_for_update().filter(pk=item.pk).first()
        if locked is None:
            return ''
        previous = getattr(locked, field)
        setattr(locked, field, key)
        locked.save(update_fields=[field])
    setattr(item, field, key)
    log_action(actor=None, action='TV_GALLERY_RENDITION_REBUILT', instance=item,
               metadata={'rendition': rendition, 'previous_key': previous, 'storage_key': item.storage_key},
               summary='Rebuilt the %s rendition from the stored original for media %s' % (rendition, item.pk))
    logger.info('TV gallery media %s: rebuilt %s rendition (was %r)', item.pk, rendition, previous)
    return key


def _storage_fault_response(request, item, rendition, outcome):
    """Answer a genuine storage fault honestly instead of a bare 404.

    The response body names the media id, rendition, storage key and outcome so
    an operator can act on it without database access, but never leaks the
    filesystem path (served to anonymous visitors).
    """
    logger.error(
        'TV gallery media %s rendition=%s kind=%s key=%r could not be served: %s',
        item.pk, rendition, item.kind, _rendition_key(item, rendition)[0], outcome,
    )
    if outcome == storage.FILE_STORAGE_UNAVAILABLE:
        available, detail = storage.is_configured()
        return JsonResponse({
            'success': False,
            'code': 'GALLERY_STORAGE_UNAVAILABLE',
            'message': ('Gallery storage is unavailable on the API server, so stored media '
                        'cannot be read. An administrator must restore the media volume '
                        '(TV_GALLERY_STORAGE_ROOT) and retry.'),
            'data': {'retryable': True, 'media_id': item.pk, 'rendition': rendition,
                     'storage_configured': available, 'storage_detail': detail},
        }, status=503)
    key = _rendition_key(item, rendition)[0] or ''
    return JsonResponse({
        'success': False,
        'code': 'GALLERY_MEDIA_FILE_MISSING' if outcome == storage.FILE_MISSING else 'GALLERY_MEDIA_FILE_UNREADABLE',
        'message': ('The media record exists but its stored file could not be read. '
                    'This is a deployment/storage fault, not a deleted gallery item: '
                    'run `python manage.py check_gallery_storage` and restore the gallery '
                    'volume from backup if the file is gone.'),
        'data': {'media_id': item.pk, 'rendition': rendition, 'kind': item.kind,
                 'storage_key': key, 'outcome': outcome, 'retryable': False},
    }, status=500)


def _serve(request, item, rendition, private=False):
    key, mime = _rendition_key(item, rendition, private=private)
    if key is None:
        # The rendition does not apply to this media type: a genuine 404.
        raise Http404
    path, outcome = storage.resolve(key) if key else (None, storage.FILE_MISSING)
    if (outcome != storage.FILE_OK or path is None) and private and rendition in _REBUILDABLE:
        # Staff preview of a legacy/partially processed image: derive the
        # bounded rendition from the stored original instead of failing.
        with _rebuild_lock(item.pk):
            rebuilt = _rebuild_image_rendition(item, rendition)
        if rebuilt:
            key, mime = rebuilt, 'image/jpeg'
            path, outcome = storage.resolve(key)
    if outcome != storage.FILE_OK or path is None:
        # A rendition that was never produced (empty key) or whose file has
        # gone is a processing/storage fault on an existing record, and the
        # operator needs to be told which — see the module docstring.
        return _storage_fault_response(request, item, rendition, outcome)
    size = path.stat().st_size
    try:
        source = path.open('rb')
    except OSError:
        return _storage_fault_response(request, item, rendition, storage.FILE_UNREADABLE)
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
    download = private and rendition == 'file' and item.kind in ('other', 'image')
    if private and rendition == 'original' and item.kind == 'other':
        download = True
    response['Content-Disposition'] = 'attachment; filename="download"' if download else 'inline'
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
            'processing_status': item.processing_status, 'processing_error': item.processing_error,
            # Repair (2026-11-26): the page asked for a rendition and discovered it
            # was missing only by failing. Reporting which renditions the record
            # actually has (additive field) lets the preview choose a candidate
            # that will answer, and tells the operator when a repair is possible.
            'renditions': {
                'thumb': bool(item.thumbnail_key),
                'display': bool(item.display_key),
                'original': bool(item.storage_key),
            }}


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
        if fingerprint:
            # room=None is the background-music collection; a retried audio
            # upload deserves the same duplicate protection as a room file.
            existing = GalleryMedia.objects.filter(
                room=room, upload_fingerprint=fingerprint, active=True
            ).order_by('id').first()
            if existing is not None:
                for key in (values.get('storage_key'), values.get('thumbnail_key'), values.get('display_key')):
                    try:
                        storage.remove(key)
                    except OSError:
                        pass
                logger.info('Duplicate gallery upload ignored (media %s, room %s)',
                            existing.pk, room.pk if room is not None else 'background-music')
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


class AdminRebuildRenditions(AdminView):
    """Rebuild an image's thumbnail/display rendition from its stored original.

    Repair (2026-11-26): the staff preview could report "Preview unavailable"
    for an image whose ORIGINAL file was present and readable, because the
    derived rendition had never been produced (media stored before the
    thumbnail pipeline existed). The manager needs a supported way to repair
    such a record without re-uploading it — re-uploading would change the media
    id, the order, the cover flag and the TV URLs. This rebuilds only the
    derived renditions; ``storage_key`` and every other stored object keep their
    existing keys.
    """

    def post(self, request, pk):
        item = get_object_or_404(GalleryMedia, pk=pk)
        if item.kind != 'image':
            raise ValidationError({'rendition': 'Only image renditions can be rebuilt.'})
        built = {}
        with _rebuild_lock(item.pk):
            for rendition in ('thumb', 'display'):
                produced = _rebuild_image_rendition(item, rendition)
                built[rendition] = bool(produced)
        log_action(actor=request.user, action='TV_GALLERY_RENDITIONS_REBUILT', instance=item,
                   metadata={'rebuild_requested': True, 'result': built, 'storage_key': item.storage_key},
                   request=request,
                   summary='Rebuilt image renditions for media %s' % item.pk)
        payload = admin_media(item)
        payload['rebuilt'] = built
        if not any(built.values()):
            return Response({
                'success': False,
                'message': ('The original file could not be read, so its renditions were not rebuilt. '
                            'Run `python manage.py check_gallery_storage` and restore the gallery volume '
                            'from backup if the original is gone.'),
                'data': payload,
            }, status=status.HTTP_409_CONFLICT)
        return Response(payload)


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
