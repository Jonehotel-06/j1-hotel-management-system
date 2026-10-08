# apps/core/storage.py
"""Media storage helpers shared by every app that serves uploaded files.

Two responsibilities live here so no app has to reimplement them:

1. **Absolute media URLs.** The frontend is hosted separately from Django, so a
   relative ``/media/x.jpg`` is useless to it. :func:`absolute_media_url` always
   returns something a browser can load — the storage backend's own absolute URL
   when it already is one (Backblaze B2 / CDN), otherwise the URL made absolute
   against the incoming request, and failing that against
   ``MEDIA_PUBLIC_BASE_URL`` (needed when a serializer is used outside a request,
   e.g. the availability engine).

2. **Honest upload failures.** When the configured storage backend rejects a
   write, the API must say so instead of persisting a database row that points
   at a file which was never stored. :func:`save_upload` performs the write,
   logs a diagnostic (never credentials) and raises :class:`StorageUploadError`,
   which the project exception handler renders as a clean error envelope.
"""
import logging

from django.conf import settings
from django.core.files.storage import default_storage
from django.utils.functional import LazyObject, empty

logger = logging.getLogger("apps")

__all__ = [
    "StorageUploadError",
    "absolute_media_url",
    "delete_quietly",
    "storage_backend_label",
    "is_remote_storage",
]


class StorageUploadError(Exception):
    """Raised when the configured storage backend refuses to store a file.

    Carries a user-safe message; the technical cause is logged server-side.
    """

    default_message = (
        "The image could not be saved to media storage. The record was not "
        "changed. Please try again, or contact the administrator if this persists."
    )

    def __init__(self, message=None):
        super().__init__(message or self.default_message)
        self.message = message or self.default_message


def storage_backend_label(storage=None) -> str:
    """Dotted path of the storage class actually in use at runtime.

    Used by the health/diagnostic command — a configuration can *look* like B2
    while Django is still writing to the local filesystem.
    """
    storage = storage or default_storage
    # default_storage is a LazyObject whose `_wrapped` starts as the module-level
    # `empty` sentinel (a plain object()). Touching an attribute materialises the
    # real backend; unwrap so the REAL class is reported, not the proxy.
    real = storage
    for _ in range(3):
        if not isinstance(real, LazyObject):
            break
        if real._wrapped is empty:
            real._setup()
        real = real._wrapped
    cls = real.__class__
    return f"{cls.__module__}.{cls.__name__}"


def is_remote_storage(storage=None) -> bool:
    """True when uploads leave the local disk (S3-compatible / Backblaze B2)."""
    return "s3" in storage_backend_label(storage).lower()


def _public_base_url() -> str:
    return (getattr(settings, "MEDIA_PUBLIC_BASE_URL", "") or "").rstrip("/")


def absolute_media_url(file_field, request=None):
    """Return a browser-usable absolute URL for a FileField/ImageField value.

    Returns ``None`` for empty fields so serializers can emit ``null`` and the
    frontend can render its own empty state.
    """
    if not file_field:
        return None
    try:
        url = file_field.url
    except (ValueError, NotImplementedError):  # no file associated
        return None
    if not url:
        return None
    # Remote backends (B2/S3/CDN) already return an absolute URL.
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if request is not None:
        return request.build_absolute_uri(url)
    base = _public_base_url()
    if base:
        return f"{base}{url if url.startswith('/') else '/' + url}"
    return url


def delete_quietly(file_field) -> None:
    """Best-effort removal of a stored file; never raises.

    Used to avoid leaving an orphaned object in the bucket when the database
    write that should have referenced it fails.
    """
    if not file_field:
        return
    name = getattr(file_field, "name", None)
    if not name:
        return
    try:
        file_field.storage.delete(name)
    except Exception:  # pragma: no cover - cleanup must never mask the real error
        logger.warning("Could not delete orphaned media object '%s'.", name)


def save_upload(callback, *, context=""):
    """Run ``callback`` (which performs the storage write) with honest errors.

    Any backend failure (Backblaze rejection, credentials, networking) is logged
    with its exception type and message — never the credentials themselves —
    and re-raised as :class:`StorageUploadError`.
    """
    try:
        return callback()
    except StorageUploadError:
        raise
    except Exception as exc:
        logger.error(
            "Media upload failed%s using %s: %s: %s",
            f" ({context})" if context else "",
            storage_backend_label(),
            type(exc).__name__,
            exc,
        )
        raise StorageUploadError() from exc
