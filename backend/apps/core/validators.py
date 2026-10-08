# apps/core/validators.py
"""Upload validation — never trust the client's file claims."""
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email as _django_validate_email
from PIL import Image

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}


def validate_email_address(value):
    """Return True when ``value`` is a syntactically valid email address.

    Used server-side before queueing transactional mail so a blank or malformed
    recipient can never turn into a fake "sent" success.
    """
    value = str(value or "").strip()
    if not value or "\n" in value or "\r" in value:
        return False
    try:
        _django_validate_email(value)
    except ValidationError:
        return False
    return True


def validate_image_upload(file):
    """Shared image validator used by models and serializers.

    Enforces extension, size and *actual* image content (via Pillow), which
    blocks disguised executables masquerading under an image extension.
    """
    ext = Path(file.name).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValidationError(
            f"Unsupported file extension '{ext}'. Allowed: {', '.join(sorted(ALLOWED_IMAGE_EXTENSIONS))}."
        )
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    if file.size > max_bytes:
        raise ValidationError(f"Image exceeds the maximum size of {settings.MAX_UPLOAD_MB} MB.")
    try:
        with Image.open(file) as img:
            img.verify()
            if img.format not in ALLOWED_IMAGE_FORMATS:
                raise ValidationError("Unsupported image content type.")
    except ValidationError:
        raise
    except Exception:
        raise ValidationError("The uploaded file is not a valid image.")
    file.seek(0)
