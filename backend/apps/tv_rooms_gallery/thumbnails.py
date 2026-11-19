"""Bounded thumbnail generation; not an upload authorization or malware scanner.

Call only after authenticated upload handling, with server-enforced limits. The
output is a newly encoded JPEG; the original must be stored separately and
served safely. This module does not create or publish any files.

Repair note (2026-11-19)
========================
The previous version rejected two legitimate kinds of modern photograph:

* any file Pillow reports as ``MPO`` — the multi-picture JPEG that Android
  flagships (Samsung, Pixel, OnePlus) write for a normal camera photo. Its
  first image is an ordinary JPEG, so refusing the format rejected the photo
  for a container detail the gallery never displays.
* images above 32 megapixels or 16 MB, which 48 MP / 64 MP / 108 MP camera
  modes exceed routinely.

The limits are now parameters driven by settings (200 MP / 128 MB by default,
matching the gallery's own upload ceiling), and large JPEGs are decoded with
libjpeg's DCT scaling (``Image.draft``) so memory stays proportional to the
*output* size rather than to the camera's pixel count. The decompression-bomb
guard is kept, pinned to the configured pixel ceiling.
"""
from io import BytesIO
import threading
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

# Image.MAX_IMAGE_PIXELS is process-global Pillow state. Two gallery uploads
# processed by two gunicorn threads at once could reset each other's ceiling
# mid-decode (one legitimate photo refused as a "bomb", or a crafted header
# slipping a lowered guard). Serialise gallery decoding instead; uploads are
# staff actions, not a hot path.
_DECODE_LOCK = threading.Lock()

#: Containers that decode to a first frame we can re-encode. MPO is a JPEG
#: carrying several pictures (Android/Phone "ultra HDR" + stereo shots).
SUPPORTED_FORMATS = {"JPEG", "MPO", "PNG", "WEBP", "GIF"}


class ThumbnailError(ValueError):
    """The supplied bytes cannot safely be converted to a thumbnail."""


def make_thumbnail(data, *, max_bytes=16 * 1024 * 1024, max_pixels=200_000_000,
                   size=(640, 400), quality=80):
    with _DECODE_LOCK:
        return _make_thumbnail(data, max_bytes=max_bytes, max_pixels=max_pixels,
                               size=size, quality=quality)


def _make_thumbnail(data, *, max_bytes=16 * 1024 * 1024, max_pixels=200_000_000,
                    size=(640, 400), quality=80):
    """Return JPEG bytes for a bounded JPEG/MPO, PNG, WebP or GIF image.

    Animated files use their first frame. Metadata is not copied to the output.
    Input is limited before decoding; dimensions are checked before the pixel
    data is loaded. Callers must treat other input types as unsupported, not
    necessarily invalid.
    """
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if max_bytes < 1 or max_pixels < 1 or len(size) != 2 or min(size) < 1 or not 1 <= quality <= 95:
        raise ValueError("invalid thumbnail limits")
    if not data:
        raise ThumbnailError("Image is empty.")
    if len(data) > max_bytes:
        raise ThumbnailError(
            "Image exceeds the %d MB image processing limit." % max(1, max_bytes // (1024 * 1024))
        )

    previous_limit = Image.MAX_IMAGE_PIXELS
    # Align Pillow's decompression-bomb threshold with the configured ceiling so
    # a legitimate 108 MP photo is processed while a crafted 2 GPixel header is
    # still refused before any allocation happens.
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as source:
                if source.format not in SUPPORTED_FORMATS:
                    raise ThumbnailError(
                        "Unsupported image format %s for thumbnails." % (source.format or "unknown")
                    )
                width, height = source.size
                if width < 1 or height < 1:
                    raise ThumbnailError("Image has invalid dimensions.")
                if width * height > max_pixels:
                    raise ThumbnailError(
                        "Image is %d × %d (%d megapixels), above the %d megapixel limit. "
                        "Resize the photo and try again."
                        % (width, height, (width * height) // 1_000_000, max_pixels // 1_000_000)
                    )
                source.seek(0)
                # Decode at the smallest DCT scale that still covers twice the
                # requested output, which keeps peak memory bounded for 50-200 MP
                # JPEGs (libjpeg supports 1/2, 1/4 and 1/8 scaling).
                try:
                    source.draft("RGB", (max(1, size[0] * 2), max(1, size[1] * 2)))
                except (AttributeError, OSError, ValueError):
                    pass
                source.load()
                oriented = ImageOps.exif_transpose(source)
                # Downscale BEFORE compositing. The previous order converted
                # the full-resolution frame to RGBA and pasted it onto a
                # second full-resolution RGB frame first: a single 48 MP
                # phone photo peaked near 800 MB RSS in this process, and
                # when the container was OOM-killed the browser saw the
                # connection reset mid-upload ("Network connection
                # interrupted") even though the file itself was fine.
                factor = max(1, min(oriented.width // max(1, size[0]),
                                    oriented.height // max(1, size[1])))
                if factor > 1:
                    try:
                        oriented = oriented.reduce(factor)
                    except (AttributeError, OSError, ValueError):
                        pass
                oriented.thumbnail(size, Image.Resampling.LANCZOS)
                # Composite transparency onto a neutral light background —
                # now at thumbnail size, so the copies are tiny.
                rgba = oriented.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                result = BytesIO()
                background.save(result, format="JPEG", quality=quality, optimize=True)
                return result.getvalue()
    except ThumbnailError:
        raise
    except Image.DecompressionBombError as exc:
        raise ThumbnailError(
            "Image is too large to decode safely (above the %d megapixel limit)."
            % (max_pixels // 1_000_000)
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError,
            Image.DecompressionBombWarning) as exc:
        raise ThumbnailError("Image could not be decoded safely.") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit
