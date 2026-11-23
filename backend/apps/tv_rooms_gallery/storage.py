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

Repair note (2026-11-22) — the durable home of the bytes
========================================================
A healthy, writable directory was still not enough when that directory sits on
a container's own ephemeral filesystem: every deploy replaced the container and
took the gallery's bytes with it while the database rows survived (the exact
failure mode this module's 2026-11-20 note describes). The store therefore
gained a second, deployment-independent backend — the same S3-compatible
object storage (Backblaze B2) the project already uses for all public media —
selected by ``TV_GALLERY_STORAGE_BACKEND``:

  * ``fs`` (explicit): exactly the pre-2026-11-22 behaviour. Use it wherever
    ``TV_GALLERY_STORAGE_ROOT`` is genuinely mounted (compose volumes,
    host volumes).
  * ``object``: bytes live in the configured PRIVATE bucket
    (``TV_GALLERY_BUCKET_NAME``); the directory remains the private staging /
    cache / scratch layer that binary processing (ffprobe, ffmpeg, PIL) and the
    serving layer open as real files.
  * ``auto`` (default): the directory when the persistence probe
    (:func:`fs_is_persistent`) reports a mounted volume; the object store ONLY
    when the directory shares the container's ephemeral filesystem AND the
    object store is fully configured; otherwise the directory, so an
    unconfigured deployment keeps today's honest errors.

Keys are unchanged (32-hex, server-generated, immutable), the database rows are
unchanged, and every API response is unchanged. Only WHERE the bytes live
changes, so existing records keep resolving and nothing needs migrating.
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

# ---------------------------------------------------------------------------
# Storage backend selection (2026-11-22 — Rooms Gallery media persistence)
# ---------------------------------------------------------------------------
# The gallery store used to be filesystem-only, which is durable exactly when
# TV_GALLERY_STORAGE_ROOT is a MOUNTED persistent volume. On container hosts
# (Railway/Render/... ) with no volume mounted there, every deploy replaced the
# container and silently took the gallery's bytes with it while the database
# rows survived — the documented 2026-11-20 production failure. Besides the
# explicit fs mode, the store can now keep objects in the S3-compatible
# (Backblaze B2) object storage the project already uses for public media, via
# apps/tv_rooms_gallery/object_store.py.
#
# TV_GALLERY_STORAGE_BACKEND (env, or the Django setting of the same name):
#
#   "fs"      behave exactly as before: everything lives in the resolved
#             directory. Every deployment with a real volume should use this.
#   "object"  originals/renditions live in the configured private bucket; the
#             directory remains a private staging/cache/scratch area for binary
#             processing (ffmpeg/PIL) and serving. Requires TV_GALLERY_BUCKET_NAME.
#   "auto"    (default) use the directory when the persistence probe says it is
#             on a mounted volume; switch to object storage ONLY when the
#             directory shares the container's own ephemeral filesystem AND the
#             object store is fully configured; stay on the directory otherwise,
#             so an unconfigured deployment reports today's honest errors rather
#             than failing differently. Keys, database rows and API responses
#             are identical in every mode — only where the bytes live changes.

#: Cached per process; recomputed when the inputs that decide it change.
_MODE_CACHE: Dict[str, str] = {}


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


def fs_is_persistent(directory) -> "tuple[bool | None, str]":
    """Is ``directory`` on a MOUNTED filesystem, or inside the container layer?

    Files written into a container's own filesystem disappear when that
    container is replaced by the next deploy — which is exactly how seven
    gallery records lost their files while every upload had "succeeded". A root
    that shares a mount with / and is not itself a mountpoint is reported as NOT
    persistent, because no amount of code can make such a directory survive.

    Returns ``(True | False | None, detail)``; None means the probe could not
    run (non-Linux host without /proc/mounts, stat failure) and callers must
    not treat it as either answer.
    """
    import os as _os

    try:
        if not directory:
            return None, "storage root not resolved"
        target = _os.path.realpath(str(directory))
        device = _os.stat(target).st_dev
        root_device = _os.stat("/").st_dev
        mounts = []
        try:
            with open("/proc/mounts", encoding="utf-8", errors="replace") as handle:
                mounts = [line.split(" ")[1] for line in handle if len(line.split(" ")) > 1]
        except OSError:
            mounts = []
        is_mountpoint = target.rstrip("/") in {m.rstrip("/") for m in mounts}
        persistent = bool(device != root_device or is_mountpoint)
        return persistent, (
            "separate filesystem or mountpoint" if persistent
            else "same filesystem as / and not a mountpoint: NOT on a volume, "
                 "files are lost on redeploy"
        )
    except OSError as exc:
        return None, "unavailable: %s" % exc


def _raw_backend_setting() -> str:
    raw = (os.environ.get("TV_GALLERY_STORAGE_BACKEND") or "").strip().lower()
    if raw:
        return raw
    return str(getattr(settings, "TV_GALLERY_STORAGE_BACKEND", "") or "").strip().lower()


def backend_mode() -> "tuple[str, str]":
    """The ACTIVE backend: ``("fs" | "object", reason)``. Never raises.

    The decision inputs are hashed into a cache key so an operator (or a test)
    changing the setting or the volume layout re-triggers the probe instead of
    being stuck with a stale decision, while a steady production process runs
    the (cheap) probe exactly once.
    """
    from . import object_store

    raw = _raw_backend_setting() or "auto"
    if raw not in ("fs", "object", "auto"):
        raw = "auto"
    ready, object_detail = object_store.configured()
    persistence_detail = ""
    try:
        candidate = _resolve_candidate_root()
        persistent, persistence_detail = fs_is_persistent(candidate) if candidate else (None, "")
    except Exception:  # noqa: BLE001 - the probe must never break resolution
        candidate, persistent = None, None
    cache_key = "%s|%s|%s|%s" % (raw, bool(ready), candidate, persistent)
    if _MODE_CACHE.get("key") == cache_key and _MODE_CACHE.get("mode"):
        return _MODE_CACHE["mode"], _MODE_CACHE["reason"]

    if raw == "object":
        if not ready:
            mode, reason = "fs", "object storage requested but not configured: %s" % object_detail
        else:
            mode, reason = "object", "TV_GALLERY_STORAGE_BACKEND=object"
    elif raw == "fs":
        mode, reason = "fs", "TV_GALLERY_STORAGE_BACKEND=fs"
    else:  # auto
        if persistent is True:
            mode, reason = "fs", "the resolved directory is on a mounted, persistent volume"
        elif persistent is False and ready:
            mode, reason = (
                "object",
                "the resolved directory is NOT on a persistent volume (%s), so the "
                "configured private object store is used instead" % persistence_detail,
            )
        else:
            mode, reason = "fs", "auto: no persistent-volume evidence and object storage is not configured"
    _MODE_CACHE.update({"key": cache_key, "mode": mode, "reason": reason})
    return mode, reason


def _configured_root() -> str:
    """The configured root path (env wins over the Django setting)."""
    raw = (os.environ.get("TV_GALLERY_STORAGE_ROOT") or "").strip()
    if raw:
        return raw
    return str(getattr(settings, "TV_GALLERY_STORAGE_ROOT", "") or "").strip()


def _resolve_candidate_root():
    """The root path the persistence probe inspects — WITHOUT creating anything.

    ``root()`` both creates the directory and refuses unsafe locations; the
    backend-mode probe must observe the configured location before any of that
    side-effecting work happens, so it re-implements the same resolution here
    read-only. An unresolvable candidate returns None (the probe then has no
    opinion and auto keeps the fs behaviour, with root() raising the honest
    error later if it is genuinely unusable).
    """
    raw = _configured_root()
    if not raw or not Path(raw).is_absolute():
        return None
    try:
        return Path(raw).expanduser()
    except (OSError, RuntimeError):  # pragma: no cover - defensive
        return None


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
    mode, reason = backend_mode()
    if mode == "object":
        try:
            usable = root()  # the local staging layer must also be usable
            return True, "object storage (%s) with local staging at %s" % (reason, usable)
        except GalleryStorageUnavailable as exc:
            return False, "object staging layer unusable: %s" % exc.message
        except (ImproperlyConfigured, OSError) as exc:  # pragma: no cover - defensive
            return False, "object staging layer unusable: %s" % exc
    try:
        directory = root()
        return True, "%s (fs: %s)" % (directory, reason)
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


#: Directory inside the private store that caches object-backed keys locally so
#: a rendition is downloaded from object storage once per container instead of
#: once per request. Keys are immutable in this design (save_chunks generates a
#: fresh uuid4 key per write and never overwrites), so a cache entry never goes
#: stale. Excluded from orphan cleanup by name (not a 32-hex key, not a
#: .transcode-* temp file).
OBJECT_CACHE_DIR = ".object-cache"
#: Soft cap for that cache, so a long-lived host cannot fill its disk with
#: cached media. Eviction removes the least recently used CACHED copies only —
#: never originals, never the object store.
DEFAULT_OBJECT_CACHE_MB = 1024


def _object_cache_dir() -> Path:
    """The local object cache inside the private store (created on demand).

    ONLY the object-backend write paths call this with creation intent; fs-mode
    deployments must never see the directory (the pre-existing behaviour of the
    resolved directory — "contains exactly the stored files" — is preserved).
    """
    directory = root() / OBJECT_CACHE_DIR
    if not directory.exists():
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def _object_cache_path(key: str) -> Optional[Path]:
    """A READ-ONLY lookup of a cached copy of ``key``. Never creates anything."""
    if not is_valid_key(key):
        return None
    try:
        cache_dir = root() / OBJECT_CACHE_DIR
        if not cache_dir.is_dir():
            return None
        return cache_dir / str(key)
    except GalleryStorageUnavailable:
        return None


def scratch_dir() -> Path:
    """A writable directory for local processing temp files (ffmpeg outputs).

    ``video_processing`` writes its bounded transcode output next to the store;
    the location only has to be private and writable, in every backend mode.
    """
    return root()


def _evict_object_cache() -> None:
    """Keep the local object cache under its soft cap (LRU by atime/mtime)."""
    try:
        cache_dir = _object_cache_dir()
    except GalleryStorageUnavailable:
        return
    cap_mb = DEFAULT_OBJECT_CACHE_MB
    try:
        cap_mb = int((os.environ.get("TV_GALLERY_OBJECT_CACHE_MB") or "").strip() or DEFAULT_OBJECT_CACHE_MB)
    except ValueError:
        cap_mb = DEFAULT_OBJECT_CACHE_MB
    if cap_mb <= 0:
        return
    cap_bytes = cap_mb * 1024 * 1024
    try:
        entries = [p for p in cache_dir.iterdir() if p.is_file() and not p.name.startswith(".tmp-")]
    except OSError:
        return
    total = 0
    stats = []
    for entry in entries:
        try:
            info = entry.stat()
            total += info.st_size
            stats.append((entry, info.st_size, min(info.st_atime, info.st_mtime)))
        except OSError:
            continue
    if total <= cap_bytes:
        return
    stats.sort(key=lambda item: item[2])
    for entry, size, _stamp in stats:
        if total <= cap_bytes:
            break
        try:
            entry.unlink(missing_ok=True)
            total -= size
        except OSError:
            continue


def _download_to_cache(key: str) -> tuple:
    """Fetch one key from object storage into the local cache; returns resolve-style tuple."""
    from . import object_store

    destination = _object_cache_path(key)
    if destination is None:
        return None, FILE_MISSING
    temp = destination.with_name(".tmp-" + key)
    try:
        with open(temp, "wb") as sink:
            object_store.download(key, sink)
        os.replace(temp, destination)
        try:
            os.chmod(destination, 0o600)
        except OSError:  # pragma: no cover - best effort
            pass
        _evict_object_cache()
        return destination, FILE_OK
    except FileNotFoundError:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        return None, FILE_MISSING
    except OSError as exc:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        logger.error("Gallery object %s could not be staged locally: %s", key, exc)
        return None, FILE_UNREADABLE
    except Exception as exc:  # noqa: BLE001 - connection/config faults (503 path)
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        logger.error("Gallery object store unreachable while staging %s: %s", key, exc)
        return None, FILE_STORAGE_UNAVAILABLE


def local_file(key: str):
    """Context manager yielding a REAL local path for one stored key.

    ``media.prepare`` and ``video_processing`` need actual files (ffprobe,
    ffmpeg, PIL), and the API serving layer opens a path. On the filesystem
    backend that is simply the stored file. On the object backend the key is
    staged into the local cache first — keys are immutable, so this is a
    download-once cache, not a copy-per-call. The yielded path must NOT be
    moved, replaced or written to by the caller.
    """
    from contextlib import contextmanager

    @contextmanager
    def _local_file():
        if backend_mode()[0] == "object":
            path, outcome = resolve(key)
            if outcome != FILE_OK or path is None:
                raise FileNotFoundError("Gallery media %s is not available (%s)." % (key, outcome))
            yield path
            return
        path = path_for(key)
        if not path.is_file():
            raise FileNotFoundError(str(path))
        yield path

    return _local_file()


def resolve(key: str):
    """Locate a stored key and say honestly whether it can be served.

    Returns ``(path_or_None, outcome)``. ``outcome`` is one of ``ok`` /
    ``missing`` / ``unreadable`` / ``storage_unavailable`` so the API can answer
    404, 500-with-a-reason or 503 instead of collapsing all three into 404.

    On the object backend the returned path is a local, read-only staged copy of
    the immutable object (see :func:`local_file`): the serving layer keeps
    opening real files while the durable bytes live in the private bucket. An
    object-backed deployment additionally falls back to a file still present in
    the local directory, so records written before a backend switch keep
    resolving within the container that still holds them.
    """
    if backend_mode()[0] == "object":
        cache_path = _object_cache_path(key)
        if cache_path is not None:
            try:
                if cache_path.is_file() and cache_path.stat().st_size > 0:
                    return cache_path, FILE_OK
            except OSError:
                return None, FILE_UNREADABLE
        # Records written before the switch may still have their bytes in the
        # (same-container) directory; prefer the object store as the durable
        # source, but never lose access to what is still readable locally.
        try:
            path = path_for(key)
            if path.is_file() and path.stat().st_size > 0:
                return path, FILE_OK
        except (ValueError, GalleryStorageUnavailable, OSError):
            pass
        try:
            from . import object_store

            size = object_store.stat(key)
            if size is None:
                return None, FILE_MISSING
            if size <= 0:
                return None, FILE_UNREADABLE
        except ValueError:
            return None, FILE_MISSING
        except Exception as exc:  # noqa: BLE001 - connection/config faults (503 path)
            logger.error("Gallery object store unreachable while resolving %s: %s", key, exc)
            return None, FILE_STORAGE_UNAVAILABLE
        return _download_to_cache(key)

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
    """Diagnostics for ``manage.py check_gallery_storage``; never raises.

    Reports the ACTIVE backend plus the local directory. On the object backend
    the directory is the private staging/cache/scratch layer, not the durable
    home — ``durable`` names where the bytes actually survive a deploy.
    """
    from . import object_store

    mode, reason = backend_mode()
    try:
        directory = root()
    except GalleryStorageUnavailable as exc:
        return {
            "configured": False,
            "error": exc.message,
            "backend": mode,
            "backend_reason": reason,
        }
    marker = directory / VOLUME_MARKER
    persistent, persistence_detail = fs_is_persistent(directory)
    payload = {
        "configured": True,
        "backend": mode,
        "backend_reason": reason,
        "durable": ("private object storage" if mode == "object"
                    else ("persistent volume" if persistent else "NOT persistent (container filesystem)")),
        "path": str(directory),
        "usable": os.access(directory, os.W_OK | os.X_OK),
        "volume_marker": marker.is_file(),
        "files": len([p for p in directory.rglob('*') if p.is_file()
                      and OBJECT_CACHE_DIR not in p.parts]),
        "fs_persistence_probe": {"persistent": persistent, "detail": persistence_detail},
    }
    object_report = object_store.describe()
    payload["object_store"] = object_report
    if mode == "object":
        cache_dir = directory / OBJECT_CACHE_DIR
        cached = [p for p in cache_dir.rglob('*') if p.is_file()] if cache_dir.is_dir() else []
        payload["cached_objects"] = len(cached)
        payload["cached_bytes"] = sum(p.stat().st_size for p in cached if p.stat().st_size)
    return payload


def save_chunks(chunks, max_size):
    """Write a bounded stream into the active backend; returns ``(key, size)``.

    The key shape is identical in every backend mode (a fresh 32-hex name), so
    database rows and API responses never change. On the object backend the
    bytes are staged in a local temp file (which enforces ``max_size``), uploaded
    under the private gallery prefix, and the staged file is kept as the first
    cache entry for the immutable key.
    """
    from . import object_store

    key = uuid4().hex
    mode, reason = backend_mode()
    if mode == "object":
        ready, detail = object_store.configured()
        if not ready:
            raise GalleryStorageUnavailable(detail)
        cache_dir = _object_cache_dir()
        temp = cache_dir / (".tmp-" + key)
        total = 0
        try:
            with open(temp, "wb") as target:
                for chunk in chunks:
                    total += len(chunk)
                    if total > max_size:
                        raise ValueError("File exceeds the configured upload size limit.")
                    target.write(chunk)
            if total == 0:
                raise ValueError("File is empty.")
            with open(temp, "rb") as source:
                object_store.put(key, source)
            os.replace(temp, cache_dir / key)  # seed the immutable cache entry
            try:
                os.chmod(cache_dir / key, 0o600)
            except OSError:  # pragma: no cover - best effort
                pass
            return key, total
        except BaseException:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                object_store.delete(key)  # never leave a partial object behind
            except Exception:  # noqa: BLE001 - cleanup is best effort
                pass
            raise

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
    """Delete one stored key from EVERY layer it may live in. Never raises.

    On the object backend that is the object plus its local cache copy; the
    local directory is also cleaned so a record deleted after a backend switch
    does not leave pre-switch bytes behind. A malformed key names no file and no
    object; removal is a no-op, never a path.
    """
    if not key:
        return
    try:
        mode, _reason = backend_mode()
    except Exception:  # noqa: BLE001 - the mode probe must never block removal
        mode = "fs"
    try:
        path_for(key).unlink(missing_ok=True)
    except (ValueError, GalleryStorageUnavailable):
        # A malformed key names no file, and an unusable store has nothing to
        # remove from the filesystem layer; removal is a no-op, never a path.
        pass
    except OSError:
        pass
    if mode == "object":
        # The local cache can only exist when the object backend is active;
        # probing for it on the filesystem backend would create the cache
        # directory where none belongs (fs-mode deployments keep containing
        # exactly the stored files, as they always have).
        try:
            cache_path = _object_cache_path(key)
            if cache_path is not None:
                cache_path.unlink(missing_ok=True)
        except OSError:
            pass
        from . import object_store

        try:
            object_store.delete(key)
        except Exception as exc:  # noqa: BLE001 - surface in logs, never in the request
            logger.error("Gallery object %s could not be deleted from object storage: %s", key, exc)
