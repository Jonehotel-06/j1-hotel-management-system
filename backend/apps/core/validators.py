# apps/core/validators.py
"""Upload validation — never trust the client's file claims."""
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email as _django_validate_email
from PIL import Image

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}

#: Receipts and invoices are documents, not gallery images: a PDF scan is the
#: normal case, a phone photo of a paper slip is the other. Executables, HTML
#: and archives are refused outright.
ALLOWED_DOCUMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_DOCUMENT_FORMATS = {"PDF", "JPEG", "PNG", "WEBP"}


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


def validate_document_upload(file):
    """Validate an optional receipt / invoice attachment.

    Accepts PDF plus the image formats the hotel actually receives (a phone
    photo of a supplier slip). The file's real content is checked, not just its
    name: a PDF must start with the PDF signature and an image must decode with
    Pillow. Size is bounded by ``MAX_UPLOAD_MB``.
    """
    ext = Path(file.name).suffix.lower()
    if ext not in ALLOWED_DOCUMENT_EXTENSIONS:
        raise ValidationError(
            "Unsupported file type. Attach a PDF or an image "
            f"({', '.join(sorted(ALLOWED_DOCUMENT_EXTENSIONS))})."
        )
    max_mb = getattr(settings, "MAX_DOCUMENT_UPLOAD_MB", settings.MAX_UPLOAD_MB)
    if file.size > max_mb * 1024 * 1024:
        raise ValidationError(f"The attachment exceeds the maximum size of {max_mb} MB.")
    file.seek(0)
    if ext == ".pdf":
        signature = file.read(5)
        file.seek(0)
        if signature != b"%PDF-":
            raise ValidationError("The uploaded file is not a valid PDF.")
        return
    try:
        with Image.open(file) as img:
            img.verify()
            if img.format not in ALLOWED_DOCUMENT_FORMATS:
                raise ValidationError("Unsupported image content type.")
    except ValidationError:
        raise
    except Exception:
        raise ValidationError("The uploaded file is not a valid image or PDF.")
    finally:
        file.seek(0)
