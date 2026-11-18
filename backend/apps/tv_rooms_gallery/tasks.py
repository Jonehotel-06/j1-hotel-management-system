"""Gallery-specific tasks use a dedicated queue, never the operational queue."""
import logging
from uuid import uuid4
from celery import shared_task
from django.utils import timezone
from . import storage
from .models import GalleryMedia
from .video_processing import make_video, VideoProcessingError

logger = logging.getLogger(__name__)


def dispatch_video(item):
    """Queue only the current generation. Never fail an already saved upload."""
    try:
        process_video.apply_async(args=[item.pk, item.processing_token], queue='tv_gallery', retry=False)
    except Exception:
        logger.exception('Could not enqueue gallery video %s', item.pk)
        GalleryMedia.objects.filter(pk=item.pk, processing_token=item.processing_token, processing_status='queued').update(
            processing_status='failed', processing_error='The video worker queue is unavailable. Retry later.', processing_updated_at=timezone.now())


def queue_video(item):
    item.processing_token = uuid4().hex
    item.processing_status = GalleryMedia.Processing.QUEUED
    item.processing_error = ''
    item.processing_updated_at = timezone.now()
    item.save(update_fields=['processing_token', 'processing_status', 'processing_error', 'processing_updated_at'])
    return item.processing_token


@shared_task(name='apps.tv_rooms_gallery.tasks.process_video', ignore_result=True)
def process_video(media_id, token):
    claimed = GalleryMedia.objects.filter(pk=media_id, kind='video', processing_token=token, processing_status='queued').update(
        processing_status='processing', processing_updated_at=timezone.now())
    if not claimed:
        return
    item = GalleryMedia.objects.filter(pk=media_id, processing_token=token).first()
    if not item:
        return
    generated = ()
    try:
        display_key, thumbnail_key = make_video(item.storage_key)
        generated = (display_key, thumbnail_key)
        updated = GalleryMedia.objects.filter(pk=media_id, processing_token=token, processing_status='processing').update(
            display_key=display_key, thumbnail_key=thumbnail_key, processing_status='ready',
            processing_error='', processing_updated_at=timezone.now())
        if updated:
            generated = ()
            # A retry may replace older derivatives. Never delete the original.
            for old in (item.display_key, item.thumbnail_key):
                if old and old not in (display_key, thumbnail_key):
                    try: storage.remove(old)
                    except OSError: logger.exception('Gallery derivative cleanup failed')
    except Exception as exc:
        logger.exception('Gallery video processing failed for %s', media_id)
        message = str(exc)[:255] if isinstance(exc, VideoProcessingError) else 'Video processing failed. Contact an administrator.'
        GalleryMedia.objects.filter(pk=media_id, processing_token=token, processing_status='processing').update(
            processing_status='failed', processing_error=message, processing_updated_at=timezone.now())
    finally:
        for key in generated:
            try: storage.remove(key)
            except OSError: logger.exception('Gallery stale derivative cleanup failed')
