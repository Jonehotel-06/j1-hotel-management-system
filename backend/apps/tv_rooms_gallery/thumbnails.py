"""Bounded thumbnail generation; not an upload authorization or malware scanner.

Call only after authenticated upload handling, with server-enforced limits. The
output is a newly encoded JPEG; the original must be stored separately and
served safely. This module does not create or publish any files.
"""
from io import BytesIO
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError


class ThumbnailError(ValueError):
    """The supplied bytes cannot safely be converted to a thumbnail."""


def make_thumbnail(data, *, max_bytes=16 * 1024 * 1024, max_pixels=32_000_000,
                   size=(640, 400), quality=80):
    """Return JPEG bytes for a bounded JPEG, PNG, WebP or GIF image.

    Animated files use their first frame. Metadata is not copied to the output.
    Input is limited before decoding; dimensions are checked before pixel load.
    Callers must treat other input types as unsupported, not necessarily invalid.
    """
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if max_bytes < 1 or max_pixels < 1 or len(size) != 2 or min(size) < 1 or not 1 <= quality <= 95:
        raise ValueError("invalid thumbnail limits")
    if not data or len(data) > max_bytes:
        raise ThumbnailError("Image is empty or exceeds the thumbnail byte limit.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as source:
                if source.format not in {"JPEG", "PNG", "WEBP", "GIF"}:
                    raise ThumbnailError("Unsupported image format for thumbnails.")
                width, height = source.size
                if width < 1 or height < 1 or width * height > max_pixels:
                    raise ThumbnailError("Image dimensions exceed the thumbnail pixel limit.")
                source.seek(0)
                source.load()
                oriented = ImageOps.exif_transpose(source)
                # Composite transparency onto a neutral light background.
                rgba = oriented.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                background.thumbnail(size, Image.Resampling.LANCZOS)
                result = BytesIO()
                background.save(result, format="JPEG", quality=quality, optimize=True)
                return result.getvalue()
    except ThumbnailError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError,
            Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise ThumbnailError("Image could not be decoded safely.") from exc
