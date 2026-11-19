"""Conservative media detection; unknown files remain private attachments.

Magic detection and ffprobe are not malware scanning or isolation boundaries.
Images and videos are re-encoded before public delivery.
"""
from pathlib import PurePath
import json
import shutil
import subprocess

from . import storage
from .thumbnails import make_thumbnail, ThumbnailError


def safe_name(value):
    name = PurePath(str(value or 'file').replace('\\', '/')).name
    name = ''.join(c for c in name if c.isprintable() and c not in '\r\n\x00')
    return name[:200] or 'file'


def identify(header):
    if header.startswith(b'\xff\xd8\xff'):
        return 'image', 'image/jpeg'
    if header.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image', 'image/png'
    if header.startswith(b'GIF87a') or header.startswith(b'GIF89a'):
        return 'image', 'image/gif'
    if len(header) >= 12 and header[:4] == b'RIFF' and header[8:12] == b'WEBP':
        return 'image', 'image/webp'
    if len(header) >= 12 and header[4:8] == b'ftyp':
        return 'video', 'video/mp4'
    if header.startswith(b'\x1a\x45\xdf\xa3'):
        return 'video', 'video/webm'
    if len(header) >= 12 and header[:4] == b'RIFF' and header[8:12] == b'WAVE':
        return 'audio', 'audio/wav'
    if header.startswith(b'ID3') or (len(header) >= 2 and header[0] == 0xff and header[1] & 0xe0 == 0xe0):
        return 'audio', 'audio/mpeg'
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
            valid = bool(formats.intersection({"mov", "mp4"})) and any(s.get("codec_type") == "video" and s.get("codec_name") in {"h264", "hevc", "mpeg4"} for s in streams)
        elif mime == "video/webm":
            valid = "webm" in formats and any(s.get("codec_type") == "video" and s.get("codec_name") in {"vp8", "vp9"} for s in streams)
        elif mime == "audio/wav":
            valid = "wav" in formats and any(s.get("codec_type") == "audio" and str(s.get("codec_name", "")).startswith("pcm_") for s in streams)
        elif mime == "audio/mpeg":
            valid = "mp3" in formats and any(s.get("codec_type") == "audio" and s.get("codec_name") == "mp3" for s in streams)
        else:
            valid = False
        return (True, "") if valid else (False, "Unsupported codec or container; stored as a private file.")
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
        return False, "Could not verify this media file; stored as a private file."


def prepare(upload, max_bytes):
    keys = []
    try:
        original, size = storage.save_chunks(upload.chunks(), max_bytes)
        keys.append(original)
        with storage.path_for(original).open('rb') as f:
            header = f.read(64)
        kind, mime = identify(header)
        notice = ''
        if kind in ('video', 'audio'):
            verified, notice = verify_av(storage.path_for(original), kind, mime)
            if not verified:
                kind, mime = 'other', 'application/octet-stream'
        thumb = display = ''
        if kind == 'image':
            if size > 16 * 1024 * 1024:
                raise ValueError('Image exceeds the 16 MB image processing limit.')
            data = storage.path_for(original).read_bytes()
            try:
                thumb_data = make_thumbnail(data, size=(640, 400))
                display_data = make_thumbnail(data, size=(1920, 1080), quality=88)
            except ThumbnailError as exc:
                raise ValueError(str(exc)) from exc
            thumb, _ = storage.save_chunks([thumb_data], 4 * 1024 * 1024)
            keys.append(thumb)
            display, _ = storage.save_chunks([display_data], 16 * 1024 * 1024)
            keys.append(display)
        return {'storage_key': original, 'size': size, 'kind': kind,
                'content_type': mime, 'thumbnail_key': thumb, 'display_key': display, '_notice': notice}
    except BaseException:
        for key in keys:
            try:
                storage.remove(key)
            except OSError:
                pass
        raise
