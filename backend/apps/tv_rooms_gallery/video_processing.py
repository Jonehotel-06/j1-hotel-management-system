"""Bounded media derivatives. Run only in an isolated, resource-limited worker.

Original files stay private. A transcoded MP4 and JPEG poster are published
only after both are verified and the generation is still current. FFmpeg and
ffprobe must be installed on the worker; see the deployment/security guide.
"""
import json
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import storage
from .thumbnails import make_thumbnail


class VideoProcessingError(Exception):
    pass


def _run(args, timeout):
    try:
        return subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=timeout, check=True).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise VideoProcessingError('Video processing failed or exceeded its time limit.') from exc


def make_video(original_key):
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        raise VideoProcessingError('Video worker requires ffmpeg and ffprobe.')
    # Fixed upper bounds avoid accepting unlimited runtime or output from staff.
    source = storage.path_for(original_key)
    payload = _run([ffprobe, '-v', 'error', '-max_streams', '32', '-protocol_whitelist', 'file,pipe',
                    '-show_entries', 'format=duration:stream=codec_name,codec_type,width,height',
                    '-of', 'json', str(source)], 15)
    if len(payload) > 262144:
        raise VideoProcessingError('Video metadata is too large.')
    try:
        parsed = json.loads(payload)
        duration = float(parsed['format']['duration'])
        streams = [s for s in parsed['streams'] if s.get('codec_type') == 'video']
        stream = streams[0]
        width, height = int(stream['width']), int(stream['height'])
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise VideoProcessingError('Video duration or dimensions could not be verified.') from exc
    if not math.isfinite(duration) or not 0 < duration <= 300 or width < 2 or height < 2 or width * height > 16_777_216:
        raise VideoProcessingError('Video must be at most five minutes and at most 16 megapixels.')
    out_key = thumb_key = ''
    # NamedTemporaryFile is inside the private directory and not addressable by
    # an API key. Cleanup occurs even when transcoding or database updates fail.
    fd, tmpname = tempfile.mkstemp(prefix='.transcode-', dir=storage.root())
    os.close(fd)
    try:
        _run([ffmpeg, '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
              '-i', str(source), '-map', '0:v:0', '-an', '-sn', '-dn', '-map_metadata', '-1',
              '-vf', "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
              '-r', '30', '-t', '300', '-c:v', 'libx264', '-profile:v', 'main', '-level:v', '3.1',
              '-pix_fmt', 'yuv420p', '-preset', 'medium', '-crf', '24', '-maxrate', '2500k',
              '-bufsize', '5000k', '-threads', '2', '-fs', str(128 * 1024 * 1024), '-movflags', '+faststart', '-f', 'mp4', '-y', tmpname], 360)
        size = Path(tmpname).stat().st_size
        if not 0 < size <= 128 * 1024 * 1024:
            raise VideoProcessingError('Processed video is empty or exceeds 128 MB.')
        checked = json.loads(_run([ffprobe, '-v', 'error', '-protocol_whitelist', 'file,pipe',
                                  '-show_entries', 'format=duration:stream=codec_name,codec_type', '-of', 'json', tmpname], 15))
        try:
            output_duration = float(checked['format']['duration'])
        except (ValueError, KeyError, TypeError) as exc:
            raise VideoProcessingError('The processed video duration could not be verified.') from exc
        if not math.isfinite(output_duration) or output_duration < duration * 0.9:
            raise VideoProcessingError('The processed video is incomplete.')
        if not any(s.get('codec_type') == 'video' and s.get('codec_name') == 'h264' for s in checked.get('streams', [])):
            raise VideoProcessingError('The processed video did not pass verification.')
        with open(tmpname, 'rb') as f:
            out_key, _ = storage.save_chunks(iter(lambda: f.read(1024 * 1024), b''), 128 * 1024 * 1024)
        poster = _run([ffmpeg, '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                       '-i', tmpname, '-frames:v', '1', '-f', 'image2pipe', '-c:v', 'mjpeg', 'pipe:1'], 30)
        thumb = make_thumbnail(poster, size=(640, 400))
        thumb_key, _ = storage.save_chunks([thumb], 4 * 1024 * 1024)
        return out_key, thumb_key
    except Exception:
        for key in (out_key, thumb_key):
            try:
                storage.remove(key)
            except OSError:
                pass
        raise
    finally:
        Path(tmpname).unlink(missing_ok=True)
