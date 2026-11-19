"""Gallery-only data. Never changes physical room or booking state."""
from django.conf import settings
from django.db import models


class GalleryRoom(models.Model):
    room = models.OneToOneField("rooms.Room", on_delete=models.CASCADE, related_name="tv_gallery")
    active = models.BooleanField(default=False)


class GalleryMedia(models.Model):
    class Kind(models.TextChoices):
        IMAGE = "image", "Image"
        VIDEO = "video", "Video"
        AUDIO = "audio", "Audio"
        OTHER = "other", "Other"

    class Processing(models.TextChoices):
        QUEUED = "queued", "Queued"
        PROCESSING = "processing", "Processing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    room = models.ForeignKey("rooms.Room", null=True, blank=True, on_delete=models.CASCADE, related_name="tv_gallery_media")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    original_name = models.CharField(max_length=200)
    storage_key = models.CharField(max_length=32, unique=True)
    # SHA-256 of the uploaded bytes, computed while the file is written. Used to
    # recognise a RETRY of the same file (same room + same bytes) and answer with
    # the existing record instead of storing a duplicate gallery entry.
    upload_fingerprint = models.CharField(max_length=64, blank=True, default="", db_index=True)
    thumbnail_key = models.CharField(max_length=32, blank=True)
    display_key = models.CharField(max_length=32, blank=True)
    content_type = models.CharField(max_length=100, default="application/octet-stream")
    size = models.PositiveBigIntegerField()
    display_order = models.IntegerField(default=0)
    is_cover = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    processing_status = models.CharField(max_length=12, choices=Processing.choices, default=Processing.READY)
    processing_token = models.CharField(max_length=32, blank=True, default="")
    processing_updated_at = models.DateTimeField(null=True, blank=True)
    processing_error = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["display_order", "id"]


class GallerySettings(models.Model):
    # Exactly one row: app-managed, not writable through Django's public API.
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    slideshow_seconds = models.PositiveSmallIntegerField(default=5)
    music = models.ForeignKey(GalleryMedia, null=True, blank=True, on_delete=models.SET_NULL, related_name="music_settings")
    music_enabled = models.BooleanField(default=False)
    music_autoplay = models.BooleanField(default=True)
    music_volume = models.PositiveSmallIntegerField(default=35)
    max_upload_mb = models.PositiveSmallIntegerField(default=128)

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]
