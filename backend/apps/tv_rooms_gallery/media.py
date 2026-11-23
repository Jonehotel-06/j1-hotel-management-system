"""Conservative media detection; unknown files remain private attachments.

Magic detection and ffprobe are not malware scanning or isolation boundaries.
Images and videos are re-encoded before public delivery.

Repair note (2026-11-19)
========================
Two legitimate files — a modern Android JPEG (``20261029_200400.jpg``) and a
Snapchat MP4 (``Snapchat-964580537.mp4``) — were both rejected with the
identical message "Validation failed.". The message was masking two different
problems, both fixed here:

* the image path had a hard-coded 16 MB / 32 MP processing ceiling that modern
  phone cameras exceed, and Pillow reports Android "MPO" (a JPEG carrying
  several pictures) as ``MPO``, which the thumbnailer refused outright;
* the video path labelled every ``ftyp``-bearing file ``video/mp4`` and then
  accepted only H.264/HEVC/MPEG-4 — Snapchat/iOS exports that are AV1, VP9 or
  plain ``.mov`` were turned into private "other" attachments even though the
  TV pipeline transcodes everything to H.264 anyway.

The limits now come from settings (``TV_GALLERY_MAX_IMAGE_BYTES`` /
``TV_GALLERY_MAX_IMAGE_PIXELS``), which default to the 128 MB gallery ceiling
and 200 MP, and the container/codec allowlist matches what ``ffmpeg`` can
actually transcode for TV playback.
"""
from pathlib import PurePath
import hashlib
import json
import shutil
import subprocess

from django.conf import settings

from . import storage
from .thumbnails import make_thumbnail, ThumbnailError

#: MP4/MOV family box types that may legally precede ``ftyp``.
_MP4_LEAD_BOXES = {b"ftyp", b"free", b"wide", b"skip", b"moov", b"mdat", b"pnot"}
#: Video codecs the TV transcoder (libx264) can ingest.
_MP4_VIDEO_CODECS = {"h264", "hevc", "mpeg4", "av1", "vp9", "vp8", "mpeg2video", "prores", "dnxhd"}
_WEBM_VIDEO_CODECS = {"vp8", "vp9", "av1"}


def safe_name(value):
    name = PurePath(str(value or 'file').replace('\\', '/')).name
    name = ''.join(c for c in name if c.isprintable() and c not in '\r\n\x00')
    return name[:200] or 'file'


def identify(header):
    """Classify a file from its first bytes (never from its name or MIME type).

    Returns ``(kind, mime)``. Anything not recognised is ``'other'`` and stays a
    private attachment — an unrecognised file is never published.
    """
    header = bytes(header or b'')
    if header.startswith(b'\xff\xd8\xff'):
        # JPEG. Android flagships often store an "MPO" (multi-picture) JPEG
        # whose first image is a plain JPEG — the same container Pillow opens as
        # format "MPO".
        return 'image', 'image/jpeg'
    if header.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image', 'image/png'
    if header.startswith(b'GIF87a') or header.startswith(b'GIF89a'):
        return 'image', 'image/gif'
    if len(header) >= 12 and header[:4] == b'RIFF' and header[8:12] == b'WEBP':
        return 'image', 'image/webp'
    if len(header) >= 12 and header[4:8] in _MP4_LEAD_BOXES and header[4:8] != b'mdat':
        # ISO base media file format: MP4, M4V, MOV, 3GP. The ISO spec puts
        # ``ftyp`` first, but some encoders (Snapchat/Android/iOS exports) emit a
        # ``free``/``wide``/``skip`` box in front, which used to make the file
        # unidentifiable even though ffprobe plays it fine.
        return 'video', 'video/mp4'
    if header[4:8] == b'ftyp' and len(header) >= 12:
        return 'video', 'video/mp4'
    if header.startswith(b'\x1a\x45\xdf\xa3'):
        return 'video', 'video/webm'
    if len(header) >= 12 and header[:4] == b'RIFF' and header[8:12] == b'WAVE':
        return 'audio', 'audio/wav'
    if header.startswith(b'ID3') or (len(header) >= 2 and header[0] == 0xff and header[1] & 0xe0 == 0xe0):
        return 'audio', 'audio/mpeg'
    if header.startswith(b'OggS'):
        return 'audio', 'audio/ogg'
    return 'other', 'application/octet-stream'


def verify_av(path, kind, mime):
    """Inspect bounded input via ffprobe; unverified media is never published.

    ffprobe is an OS-level optional tool and must be installed in the API
    environment. This does not replace malware scanning or transcoding.
    """
    executable = shutil.which("ffprobe")
    if not executable:
        return False, "Media verification is unavailable: ffprobe is not installed. Stored as a private file."
    try:
        result = subprocess.run(
            [executable, "-v", "error", "-max_streams", "32", "-protocol_whitelist", "file,pipe",
             "-probesize", "5000000", "-analyzeduration", "2000000",
             "-show_entries", "format=format_name:stream=codec_name,codec_type",
             "-of", "json", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15, check=True,
        )
        if len(result.stdout) > 262144:
            raise ValueError("metadata too large")
        info = json.loads(result.stdout)
        streams = info.get("streams", [])
        if len(streams) > 32:
            raise ValueError("too many streams")
        formats = set(info.get("format", {}).get("format_name", "").split(","))
        if mime == "video/mp4":
            # The TV transcoder re-encodes every video to H.264, so any codec it
            # can ingest is acceptable input (a Snapchat/iOS AV1 or VP9 export
            # is not a reason to reject the upload).
            valid = bool(formats.intersection({"mov", "mp4", "m4a", "3gp", "3g2", "mj2"})) and any(
                s.get("codec_type") == "video" and s.get("codec_name") in _MP4_VIDEO_CODECS for s in streams
            )
        elif mime == "video/webm":
            valid = "webm" in formats and any(
                s.get("codec_type") == "video" and s.get("codec_name") in _WEBM_VIDEO_CODECS for s in streams
            )
        elif mime == "audio/wav":
            valid = "wav" in formats and any(s.get("codec_type") == "audio" and str(s.get("codec_name", "")).startswith("pcm_") for s in streams)
        elif mime == "audio/mpeg":
            valid = "mp3" in formats and any(s.get("codec_type") == "audio" and s.get("codec_name") == "mp3" for s in streams)
        else:
            valid = False
        return (True, "") if valid else (False, "Unsupported codec or container; stored as a private file.")
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
        return False, "Could not verify this media file; stored as a private file."


def _hashing_chunks(chunks, hasher):
    """Yield chunks unchanged while feeding them to a digest."""
    for chunk in chunks:
        hasher.update(chunk)
        yield chunk


def prepare(upload, max_bytes):
    """Store the upload privately and return the GalleryMedia field values.

    The returned ``upload_fingerprint`` is the SHA-256 of the exact bytes that
    were written. The API uses it to recognise a retried upload instead of
    creating a duplicate gallery entry.
    """
    keys = []
    try:
        digest = hashlib.sha256()
        original, size = storage.save_chunks(_hashing_chunks(upload.chunks(), digest), max_bytes)
        keys.append(original)
        # One staged local view of the stored original, whichever backend holds
        # the bytes (see storage.local_file). The header read, the ffprobe
        # verification and the thumbnail source all use it; in fs mode it is the
        # stored file itself, in object mode a read-only staged copy.
        with storage.local_file(original) as stored:
            with stored.open('rb') as f:
                header = f.read(64)
            kind, mime = identify(header)
            notice = ''
            if kind in ('video', 'audio'):
                verified, notice = verify_av(stored, kind, mime)
                if not verified:
                    kind, mime = 'other', 'application/octet-stream'
            thumb = display = ''
            if kind == 'image':
                # Limits come from settings and default to the gallery ceiling, so a
                # 50 MP / 12 MB phone photo is processed instead of being rejected.
                max_image_bytes = int(getattr(settings, 'TV_GALLERY_MAX_IMAGE_BYTES', max_bytes) or max_bytes)
                max_image_pixels = int(getattr(settings, 'TV_GALLERY_MAX_IMAGE_PIXELS', 200_000_000) or 200_000_000)
                if size > max_image_bytes:
                    raise ValueError(
                        'Image exceeds the %d MB image processing limit. Resize it and try again.'
                        % max(1, max_image_bytes // (1024 * 1024))
                    )
                data = stored.read_bytes()
                try:
                    thumb_data = make_thumbnail(data, size=(640, 400), max_bytes=max_image_bytes,
                                                max_pixels=max_image_pixels)
                    display_data = make_thumbnail(data, size=(1920, 1080), quality=88,
                                                  max_bytes=max_image_bytes, max_pixels=max_image_pixels)
                except ThumbnailError as exc:
                    raise ValueError(str(exc)) from exc
                thumb, _ = storage.save_chunks([thumb_data], 4 * 1024 * 1024)
                keys.append(thumb)
                display, _ = storage.save_chunks([display_data], 16 * 1024 * 1024)
                keys.append(display)
        return {'storage_key': original, 'size': size, 'kind': kind,
                'content_type': mime, 'thumbnail_key': thumb, 'display_key': display,
                'upload_fingerprint': digest.hexdigest(), '_notice': notice}
    except BaseException:
        for key in keys:
            try:
                storage.remove(key)
            except OSError:
                pass
        raise
