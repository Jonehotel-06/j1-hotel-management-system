"""Private store for TV rooms-gallery media, deliberately separate from MEDIA_ROOT.

Design
======
* Originals, thumbnails and transcoded renditions live in a private directory
  that no web server publishes. ``MEDIA_ROOT`` is served at ``/media/`` and via
  the Backblaze B2/CDN domain, so gallery files must never be written there:
  an uploaded video or an unrecognised file must stay private until it has been
  verified, re-encoded and explicitly published through the gallery API.
* Keys are random 32-hex strings generated server-side; a client never supplies
  a path (so no traversal is possible).
* The directory is resolved from, in order:
      1. the ``TV_GALLERY_STORAGE_ROOT`` environment variable (operational
         override, evaluated per call so a container can be fixed without a
         code change),
      2. the ``TV_GALLERY_STORAGE_ROOT`` Django setting (config/settings/base.py,
         which itself defaults to a private directory derived from MEDIA_ROOT),
      3. nothing else — a missing configuration raises
         :class:`GalleryStorageUnavailable`, never a silent fallback to /tmp.

Repair note (2026-11-19)
========================
Previously *only* the environment variable was read, and the directory had to
already exist. Neither the Dockerfile, ``docker-cmd.sh``, ``compose.yaml`` nor
the settings modules ever set it, so ``root()`` raised ``ImproperlyConfigured``
on every upload — which ``views.AdminMedia.post`` converted into HTTP 400
"Validation failed.". Image and video uploads therefore failed for every staff
member, for every file, while the message blamed the file. The configuration is
now resolved, created when missing, and reported as its own failure class
(HTTP 503 with a configuration code) instead of a validation error.

Repair note (2026-11-20) — RESOLUTION, not relocation
=====================================================
A storage key is a filename inside whatever directory
``TV_GALLERY_STORAGE_ROOT`` resolves to *in the process that wrote the file*.
Database rows store only the key, never the directory. Production evidence
(2026-11-20): the public catalog still lists media 1–9 for active rooms, while
``/api/rooms-gallery/media/<id>/<rendition>/`` answers 404 for every one of
them. The records are visible and marked ready, so the failure is between the
row and the bytes: the same key does not resolve to a readable file in the
process that is serving requests (a storage root that changed between releases,
a volume that is no longer mounted, or files that never reached the volume that
outlived the deploy).

Nothing here re-interprets an existing key — that would orphan every file the
gallery already holds. Instead :func:`resolve` reports *why* a key cannot be
read (``missing`` / ``unreadable`` / ``storage_unavailable``) so the API can
answer 404 only for a record that genuinely is not there, 503 when the storage
configuration itself is broken, and 500 with an actionable diagnosis for a row
whose files are gone. ``manage.py check_gallery_storage`` then names the files
each record needs, so the volume can be restored or re-uploaded deliberately.
The 2026-11-20 404s were indistinguishable from a deleted page, which is why
several repairs never found them.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)

_KEY = re.compile(r"^[0-9a-f]{32}$")

#: Written by ``manage.py check_gallery_storage --write-marker``; its presence is
#: the only proof that this exact directory survives a deploy.
VOLUME_MARKER = ".jone-gallery-volume"


class GalleryStorageUnavailable(ImproperlyConfigured):
    """Gallery storage cannot be used; the operator must fix the deployment.

    Distinct from a validation failure: the uploaded file is not at fault, so
    the API answers 503 with this code and an actionable message instead of
    telling the staff member their image is invalid.
    """

    code = "GALLERY_STORAGE_UNAVAILABLE"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _configured_root() -> str:
    """The configured root path (env wins over the Django setting)."""
    raw = (os.environ.get("TV_GALLERY_STORAGE_ROOT") or "").strip()
    if raw:
        return raw
    return str(getattr(settings, "TV_GALLERY_STORAGE_ROOT", "") or "").strip()


def _describe_problem(directory: Path) -> str:
    return (
        "TV gallery storage is not usable: {path}. Set TV_GALLERY_STORAGE_ROOT to an "
        "absolute, writable directory on a persistent volume (for the Docker image: "
        "/var/lib/jone-gallery) and restart the service. Uploads stay disabled until "
        "this is fixed — no file is stored anywhere else."
    ).format(path=directory)


def root() -> Path:
    """Return the absolute private gallery directory, creating it if needed."""
    raw = _configured_root()
    if not raw:
        raise GalleryStorageUnavailable(
            "TV gallery storage is not configured. Set TV_GALLERY_STORAGE_ROOT to an "
            "absolute, writable directory on a persistent volume "
            "(for the Docker image: /var/lib/jone-gallery)."
        )
    if not Path(raw).is_absolute():
        raise GalleryStorageUnavailable(
            _describe_problem(Path(raw)) + " The path must be absolute."
        )
    directory = Path(raw).expanduser()

    # 1. Create it when missing. A fresh deployment (or a freshly mounted empty
    #    volume) has no directory yet, and refusing to create it turned a normal
    #    first-run condition into "every upload fails".
    if not directory.exists():
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise GalleryStorageUnavailable(
                _describe_problem(directory) + " It could not be created: {err}.".format(err=exc)
            ) from exc
    directory = directory.resolve()

    media = Path(settings.MEDIA_ROOT).resolve()
    base = Path(settings.BASE_DIR).resolve()
    # Source checkouts use <project>/backend; the Docker image uses /app.
    project = base.parent if base.name == "backend" else base
    static_root = Path(getattr(settings, "STATIC_ROOT", "") or base / "staticfiles").resolve()

    # 2. Refuse locations that are actually public or that would swallow the
    #    whole tree. MEDIA_ROOT and STATIC_ROOT are served by the web server, so
    #    private gallery files must not live inside (or above) them.
    if directory == Path("/") or directory == media or media in directory.parents or directory in media.parents:
        raise GalleryStorageUnavailable(
            _describe_problem(directory) + " It must not overlap the public MEDIA_ROOT ({media}).".format(media=media)
        )
    if directory == static_root or static_root in directory.parents or directory in static_root.parents:
        raise GalleryStorageUnavailable(
            _describe_problem(directory) + " It must not overlap STATIC_ROOT ({root}).".format(root=static_root)
        )
    if directory == project or project in directory.parents:
        raise GalleryStorageUnavailable(
            _describe_problem(directory) + " It must not be an ancestor of the project directory."
        )

    # 3. The process must be able to use it. A read-only volume is a deployment
    #    fault that must be reported as such, not as a bad upload.
    if not directory.is_dir():
        raise GalleryStorageUnavailable(_describe_problem(directory) + " It is not a directory.")
    if not os.access(directory, os.W_OK | os.X_OK):
        raise GalleryStorageUnavailable(
            _describe_problem(directory) + " It is not writable by the API process (uid {uid}).".format(uid=os.getuid())
        )
    return directory


def is_configured() -> tuple[bool, str]:
    """Cheap health probe: ``(True, path)`` or ``(False, reason)``; never raises."""
    try:
        return True, str(root())
    except GalleryStorageUnavailable as exc:
        return False, exc.message
    except (ImproperlyConfigured, OSError) as exc:  # pragma: no cover - defensive
        return False, str(exc)


def is_valid_key(key: str) -> bool:
    return bool(_KEY.fullmatch(key or ""))


def path_for(key: str) -> Path:
    """Filesystem path for a stored key.

    Keys are opaque 32-hex names generated server-side and stored flat in the
    gallery root. The shape is validated before any join, so a key can never
    traverse — and, equally important, an existing key keeps resolving to the
    same file forever (see the repair note above).
    """
    if not _KEY.fullmatch(str(key or "")):
        raise ValueError("Invalid gallery storage key")
    return root() / str(key)


#: Outcomes of :func:`resolve`.
FILE_OK = "ok"
FILE_MISSING = "missing"
FILE_UNREADABLE = "unreadable"
FILE_STORAGE_UNAVAILABLE = "storage_unavailable"


def resolve(key: str):
    """Locate a stored key and say honestly whether it can be served.

    Returns ``(path_or_None, outcome)``. ``outcome`` is one of ``ok`` /
    ``missing`` / ``unreadable`` / ``storage_unavailable`` so the API can answer
    404, 500-with-a-reason or 503 instead of collapsing all three into 404.
    """
    try:
        path = path_for(key)
    except ValueError:
        return None, FILE_MISSING
    except GalleryStorageUnavailable:
        return None, FILE_STORAGE_UNAVAILABLE
    try:
        if not path.is_file():
            return None, FILE_MISSING
        size = path.stat().st_size
    except GalleryStorageUnavailable:
        return None, FILE_STORAGE_UNAVAILABLE
    except OSError:
        return None, FILE_UNREADABLE
    if size <= 0:
        return None, FILE_UNREADABLE
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except GalleryStorageUnavailable:
        return None, FILE_STORAGE_UNAVAILABLE
    except OSError:
        return None, FILE_UNREADABLE
    return path, FILE_OK


def describe_root() -> dict:
    """Diagnostics for ``manage.py check_gallery_storage``; never raises."""
    try:
        directory = root()
    except GalleryStorageUnavailable as exc:
        return {"configured": False, "error": exc.message}
    marker = directory / VOLUME_MARKER
    return {
        "configured": True,
        "path": str(directory),
        "usable": os.access(directory, os.W_OK | os.X_OK),
        "volume_marker": marker.is_file(),
        "files": len([p for p in directory.rglob('*') if p.is_file()]),
    }


def save_chunks(chunks, max_size):
    key = uuid4().hex
    path = path_for(key)
    total = 0
    created = False
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(fd, "wb") as target:
            for chunk in chunks:
                total += len(chunk)
                if total > max_size:
                    raise ValueError("File exceeds the configured upload size limit.")
                target.write(chunk)
        if total == 0:
            raise ValueError("File is empty.")
        return key, total
    except BaseException:
        if created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def remove(key):
    if key:
        try:
            path_for(key).unlink(missing_ok=True)
        except ValueError:
            # A malformed key names no file; removal is a no-op, never a path.
            return
