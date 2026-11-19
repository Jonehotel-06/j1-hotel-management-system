"""ISO/IEC 19794-2 / ANSI INCITS 378 finger minutiae record (FMR) parser.

Why this module exists
======================
``fingerprint_bridge_service.py`` asks the DigitalPersona SDK (``dpfj.dll``)
for a minutiae template and must turn that template into the plain point list
``[{x, y, angle, type, quality}, ...]`` that the J-ONE API validates
(``backend/apps/staff_operations/services/fingerprint_service.py``). The first
shipped revision of the bridge imported this module but the file was never
committed, so the bridge process died with ``ModuleNotFoundError`` the moment
it started — the workstation reported "the fingerprint bridge is not
reachable" no matter what was plugged in. This module is that missing piece.
It is pure standard library: the bridge must run on a reception PC with
nothing installed beyond Python itself.

Formats parsed
==============
Both template formats the bridge asks ``dpfj.dll`` for share the FMR record
layout; they differ only in the version string and in how the one-byte
minutiae angle is scaled:

* ISO/IEC 19794-2:2005 — version ``" 20\\0"``, angle in units of 2 degrees
  (byte value 0-179 maps to 0-358 degrees).
* ANSI INCITS 378-2004 — version ``" 30\\0"``, angle in units of 360/256
  degrees.

Record layout (all integers big-endian):

* bytes 0-3   ``"FMR\\0"`` format identifier
* bytes 4-7   version string
* bytes 8-11  total record length (uint32)
* bytes 12-13 capture device identifier
* bytes 14-15 image width, 16-17 image height
* bytes 18-19 X resolution, 20-21 Y resolution
* byte  22    number of finger views
* byte  23    reserved

Then, per finger view:

* finger position (1 byte), view/impression (1 byte),
  finger quality (1 byte), minutiae count (1 byte)
* count x 6-byte minutiae: X (14 bit, top 2 bits of the first byte are the
  minutiae type), Y (14 bit), angle (1 byte), quality (1 byte, 0 = unknown)
* an optional extended-data block: uint16 length followed by that many bytes

Minutiae type codes: 0 = other, 1 = ridge ending, 2 = bifurcation,
3 = other. Returned points use the API's vocabulary (``ending`` /
``bifurcation`` / ``other``), integer coordinates, angle in degrees
(0-359) and the template's own quality byte unchanged (the API clamps it
into 1-100 itself; inventing a quality here would falsify the capture).

Anything malformed raises :class:`TemplateParseError` — the bridge treats
that as "this SDK format candidate did not work" and never fabricates
points to fill the gap.
"""
from __future__ import annotations

from typing import Dict, List

__all__ = ["TemplateParseError", "parse_template"]


class TemplateParseError(ValueError):
    """The supplied bytes are not a parseable FMR minutiae template."""


_TYPE_NAMES = {0: "other", 1: "ending", 2: "bifurcation", 3: "other"}
_HEADER_BYTES = 24
_MINUTIA_BYTES = 6
_MAX_VIEWS = 10
_MAX_MINUTIAE_PER_VIEW = 250


def _u16(data: bytes, offset: int) -> int:
    return (data[offset] << 8) | data[offset + 1]


def _u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "big")


def parse_template(data: bytes) -> List[Dict[str, object]]:
    """Parse an FMR template into API minutiae points.

    Returns one ``{x, y, angle, type, quality}`` dict per minutia, across
    all finger views in the record (the DigitalPersona SDK returns a
    single view for a single-finger capture). Raises
    :class:`TemplateParseError` for anything that is not a well-formed
    ISO/IEC 19794-2:2005 or ANSI INCITS 378-2004 record.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TemplateParseError("template must be bytes")
    data = bytes(data)
    if len(data) < _HEADER_BYTES:
        raise TemplateParseError("template is too short (%d bytes)" % len(data))
    if data[0:4] != b"FMR\x00":
        raise TemplateParseError("template does not start with the FMR identifier")

    version = data[4:8]
    is_ansi = version.startswith(b" 3")
    declared_length = _u32(data, 8)
    if declared_length and declared_length > len(data):
        raise TemplateParseError(
            "template declares %d bytes but only %d were supplied"
            % (declared_length, len(data))
        )
    limit = declared_length if declared_length else len(data)

    view_count = data[22]
    if view_count < 1 or view_count > _MAX_VIEWS:
        raise TemplateParseError("template has an implausible finger-view count (%d)" % view_count)

    points: List[Dict[str, object]] = []
    offset = _HEADER_BYTES
    for _view in range(view_count):
        if offset + 4 > limit:
            raise TemplateParseError("template ends inside a finger-view header")
        minutiae_count = data[offset + 3]
        if minutiae_count > _MAX_MINUTIAE_PER_VIEW:
            raise TemplateParseError(
                "finger view declares an implausible minutiae count (%d)" % minutiae_count
            )
        offset += 4
        for _index in range(minutiae_count):
            if offset + _MINUTIA_BYTES > limit:
                raise TemplateParseError("template ends inside the minutiae list")
            b0, b1, b2, b3, angle_byte, quality = data[offset:offset + _MINUTIA_BYTES]
            x = ((b0 & 0x3F) << 8) | b1
            y = ((b2 & 0x3F) << 8) | b3
            if is_ansi:
                angle = int(round(angle_byte * 360.0 / 256.0)) % 360
            else:
                angle = (angle_byte * 2) % 360
            points.append({
                "x": x,
                "y": y,
                "angle": angle,
                "type": _TYPE_NAMES[(b0 >> 6) & 0x03],
                "quality": quality,
            })
            offset += _MINUTIA_BYTES
        # Optional per-view extended-data block (ridge counts etc.). Present
        # only when bytes remain inside the declared record; skip it so a
        # following view (or the end of the record) is read correctly.
        if offset + 2 <= limit:
            extended_length = _u16(data, offset)
            offset += 2
            if extended_length:
                if offset + extended_length > limit:
                    raise TemplateParseError("template ends inside the extended-data block")
                offset += extended_length

    if not points:
        raise TemplateParseError("template contains no minutiae")
    return points
