# apps/core/validators.py
"""Upload validation — never trust the client's file claims."""
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email as _django_validate_email
from PIL import Image

#: Every extension the hotel's own cameras, phones and staff actually produce.
#: ``.jpe``/``.jfif``/``.pjpeg`` are the same JPEG container under the names
#: Windows, some Android file managers and older scanners give it.
ALLOWED_IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".pjpeg", ".png", ".webp",
}
ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}

#: The ONE extension a stored file gets for each real format. Normalising on the
#: detected format (never on the client's claim) is what makes a served photo's
#: Content-Type match its bytes.
FORMAT_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}

#: MIME types a browser may report for an allowed format. ``image/jpg`` and
#: ``image/pjpeg`` are not registered types but are emitted by Windows/IE-era
#: mappings and by several Android file providers; an empty string means the
#: browser could not work the type out at all. None of these are ever *trusted* —
#: the bytes are decoded below — but they must never cause a valid photo to be
#: rejected before it is even read.
RECOGNISED_IMAGE_MIME_TYPES = {
    "image/jpeg", "image/jpg", "image/pjpeg", "image/x-citrix-jpeg",
    "image/png", "image/x-png", "image/webp",
}


def normalise_image_name(name: str, image_format: str) -> str:
    """Return ``name`` with the extension its REAL format requires.

    ``holiday.JPEG`` -> ``holiday.jpg``, ``scan.jfif`` -> ``scan.jpg``,
    ``blob`` -> ``blob.jpg``. The stem is stripped of path separators so a
    caller-supplied name can never influence where the file is written.
    """
    raw = str(name or "")
    stem = Path(raw.replace("\\", "/")).stem
    stem = "".join(ch for ch in stem if ch not in "/\\\x00").strip() or "image"
    return f"{stem[:80]}{FORMAT_EXTENSIONS.get(image_format, '.jpg')}"

#: Receipts and invoices are documents, not gallery images: a PDF scan is the
#: normal case, a phone photo of a paper slip is the other. Executables, HTML
#: and archives are refused outright.
ALLOWED_DOCUMENT_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".jpe", ".jfif", ".pjpeg", ".png", ".webp",
}
ALLOWED_DOCUMENT_FORMATS = {"PDF", "JPEG", "PNG", "WEBP"}


def is_recognised_image_mime(content_type) -> bool:
    """True for a MIME type a browser may legitimately report for an image.

    Informational only: acceptance is always decided by decoding the bytes.
    Kept as one list so the backend rule and the dashboard's client-side
    pre-check (``JONE.dashboard.imageFileIssue``) can never drift apart.
    """
    return str(content_type or "").split(";")[0].strip().lower() in RECOGNISED_IMAGE_MIME_TYPES


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

    The rule is CONTENT FIRST, name second: the file is decoded with Pillow and
    the format it actually contains decides whether it is accepted. Because of
    that, a photo whose name carries an unusual or missing extension
    (``IMG_2041.JFIF``, ``scan.jpe``, the ``blob`` name a mobile browser sends)
    is still a perfectly good JPG/JPEG and is accepted — its stored name is
    rewritten to match the real format, so what is served always has the right
    Content-Type.

    A renamed *non-image* is still refused: the bytes must decode to JPEG, PNG
    or WebP. Extension alone never admits a file, and the size ceiling is
    reported with the actual upload size so the message can be acted on.

    The previous implementation refused anything whose file name lacked one of
    exactly four extensions, before looking at the bytes at all. JPG/JPEG
    uploads from phones and Windows Explorer (which routinely name JPEGs
    ``.jfif``/``.jpe``, or send no extension at all) were rejected as
    "Unsupported file extension", while PNG — which always arrives as
    ``.png`` — worked, which is exactly the reported asymmetry.
    """
    name = str(getattr(file, "name", "") or "")
    ext = Path(name.replace("\\", "/")).suffix.lower()

    size = getattr(file, "size", None)
    max_mb = settings.MAX_UPLOAD_MB
    max_bytes = max_mb * 1024 * 1024
    if size is not None and size > max_bytes:
        found = f"{size / (1024 * 1024):.1f} MB"
        raise ValidationError(
            f"This image is {found}; the maximum size is {max_mb} MB "
            "(MAX_UPLOAD_MB). Please resize the photo and try again."
        )
    if size == 0:
        raise ValidationError("That file is empty (0 bytes). Please choose the image again.")

    try:
        with Image.open(file) as img:
            img.verify()
            image_format = img.format
    except Image.DecompressionBombError:
        raise ValidationError(
            "That image is too large to process (its pixel dimensions exceed the "
            "safe limit). Please resize it and try again."
        )
    except ValidationError:
        raise
    except Exception:
        raise ValidationError(
            "That file is not a readable image. Choose a JPG, PNG or WebP photo."
        )
    finally:
        try:
            file.seek(0)
        except Exception:  # pragma: no cover - a stream that cannot seek is already unusable
            pass

    if image_format not in ALLOWED_IMAGE_FORMATS:
        raise ValidationError(
            f"That file is a {'/'.join(sorted(str(image_format or 'unknown').split()))} "
            "image; only JPG, JPEG, PNG and WebP are accepted."
            if image_format else
            "That file is not a readable image. Choose a JPG, PNG or WebP photo."
        )

    # The content is a real, accepted image. Give it the canonical extension for
    # what it actually is: a valid photo must never be refused because its file
    # name was odd, and a stored file must never claim an extension its bytes do
    # not have.
    canonical = FORMAT_EXTENSIONS[image_format]
    if ext != canonical or Path(name).suffix != canonical:
        file.name = normalise_image_name(name, image_format)
    return file


def validate_document_upload(file):
    """Validate an optional receipt / invoice attachment.

    Accepts PDF plus the image formats the hotel actually receives (a phone
    photo of a supplier slip). The file's real content is checked, not just its
    name: a PDF must start with the PDF signature and an image must decode with
    Pillow. Size is bounded by ``MAX_UPLOAD_MB``.
    """
    ext = Path(str(getattr(file.file if hasattr(file, "file") else file, "name", "") or
                   getattr(file, "name", "")).replace("\\", "/")).suffix.lower()
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
            image_format = img.format
    except ValidationError:
        raise
    except Exception:
        raise ValidationError("The uploaded file is not a valid image or PDF.")
    finally:
        file.seek(0)
    if image_format not in ALLOWED_DOCUMENT_FORMATS:
        raise ValidationError("Unsupported image content type.")
    # Same rule as photos: the extension follows the real format, so a receipt
    # photographed as "IMG_0042.JFIF" is stored (and served) as a JPEG.
    canonical = FORMAT_EXTENSIONS.get(image_format)
    if canonical and Path(str(getattr(file, "name", ""))).suffix.lower() != canonical:
        file.name = normalise_image_name(getattr(file, "name", ""), image_format)
