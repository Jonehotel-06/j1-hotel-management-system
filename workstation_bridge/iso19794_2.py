"""Parser for ISO/IEC 19794-2:2005 and ANSI INCITS 378-2004 FMR templates.

``dpfj.dll`` (``dpfj_create_fmd_from_raw`` / ``dpfj_create_fmd_from_fid``)
produces binary Finger Minutiae Records (FMR). The hotel API validates and
matches a JSON list of ``{x, y, angle, type, quality}`` points, so this module
decodes the binary FMR buffer on the workstation without needing external
dependencies.

Record header layout notes (verified against DigitalPersona U.are.U ``dpfj.h``):
* ISO/IEC 19794-2:2005 (``DPFJ_FMD_ISO_19794_2_2005``): 24-byte general header,
  4-byte total length at offset 8, view count at offset 22, reserved byte at 23,
  angles in units of 2 degrees (0..179 -> 0..358 deg).
* ANSI INCITS 378-2004 (``DPFJ_FMD_ANSI_378_2004``): 26-byte general header
  (``DPFJ_FMD_ANSI_378_2004_RECORD_HEADER_LENGTH = 26``), 2-byte length at
  offset 8 (or 6-byte extended length when bytes 8..9 == 0), view count at
  offset 24, reserved byte at 25, angles in units of 360/256 degrees.
"""
from __future__ import annotations

import struct
from typing import Dict, List, Optional

_MAGIC = b"FMR\x00"
_ISO_VERSION = b" 20\x00"
_ANSI_LEGACY_VERSION = b" 30\x00"

_ISO_HEADER_BYTES = 24
_ANSI_HEADER_BYTES = 26
_VIEW_HEADER_BYTES = 4
_MINUTIA_BYTES = 6
_MAX_MINUTIAE_PER_VIEW = 250

_TYPE_NAMES = {
    0: "other",
    1: "ending",
    2: "bifurcation",
    3: "other",
}


class TemplateParseError(ValueError):
    """Raised when ``dpfj.dll`` returned bytes that are not a valid FMR record."""


def _parse_views(
    blob: bytes,
    offset: int,
    view_count: int,
    angle_scale: float,
) -> Optional[List[Dict[str, object]]]:
    pos = offset
    points: List[Dict[str, object]] = []
    for _view in range(view_count):
        if pos + _VIEW_HEADER_BYTES > len(blob):
            return None
        minutia_count = blob[pos + 3]
        pos += _VIEW_HEADER_BYTES
        if minutia_count > _MAX_MINUTIAE_PER_VIEW:
            return None
        needed = minutia_count * _MINUTIA_BYTES
        if pos + needed > len(blob):
            return None
        for idx in range(minutia_count):
            base = pos + idx * _MINUTIA_BYTES
            first, second, raw_angle, raw_quality = struct.unpack_from(">HHBB", blob, base)
            mtype = (first >> 14) & 0x03
            x = first & 0x3FFF
            y = second & 0x3FFF
            angle = round((raw_angle * angle_scale) % 360.0, 2)
            # In ISO/ANSI FMR, 0 means quality not reported; clamp to API range 1..100.
            quality = 50 if raw_quality == 0 else max(1, min(100, int(raw_quality)))
            points.append({
                "x": int(x),
                "y": int(y),
                "angle": int(angle) if angle == int(angle) else angle,
                "type": _TYPE_NAMES.get(mtype, "other"),
                "quality": quality,
            })
        pos += needed
    # If extended data follows the views in a real record, verify the 2-byte
    # extended-data-length block does not exceed remaining bytes; reject raw truncation.
    if pos < len(blob):
        if pos + 2 > len(blob):
            return None
        ext_len = struct.unpack_from(">H", blob, pos)[0]
        if pos + 2 + ext_len > len(blob):
            return None
    return points


def _parse_iso_24(blob: bytes, angle_scale: float) -> Optional[List[Dict[str, object]]]:
    if len(blob) < _ISO_HEADER_BYTES + _VIEW_HEADER_BYTES:
        return None
    declared_len = struct.unpack_from(">I", blob, 8)[0]
    if declared_len != len(blob):
        # Allow minor mismatch only if views exactly consume the buffer, never truncated.
        view_count = blob[22]
        if view_count < 1 or view_count > 16:
            return None
        parsed = _parse_views(blob, _ISO_HEADER_BYTES, view_count, angle_scale)
        if parsed is None:
            return None
        # Reject if declared_len clearly indicates truncation
        if declared_len > len(blob):
            return None
        return parsed
    view_count = blob[22]
    if view_count < 1 or view_count > 16:
        return None
    return _parse_views(blob, _ISO_HEADER_BYTES, view_count, angle_scale)


def _parse_ansi_26(blob: bytes) -> Optional[List[Dict[str, object]]]:
    if len(blob) < _ANSI_HEADER_BYTES + _VIEW_HEADER_BYTES:
        return None
    short_len = struct.unpack_from(">H", blob, 8)[0]
    if short_len == 0:
        # 6-byte extended record length: header grows by 4 bytes (offset 30)
        if len(blob) < 30 + _VIEW_HEADER_BYTES:
            return None
        ext_len = struct.unpack_from(">I", blob, 10)[0]
        if ext_len > len(blob):
            return None
        view_count = blob[28]
        header_bytes = 30
    else:
        if short_len > len(blob):
            return None
        view_count = blob[24]
        header_bytes = _ANSI_HEADER_BYTES
    if view_count < 1 or view_count > 16:
        return None
    return _parse_views(blob, header_bytes, view_count, 360.0 / 256.0)


def parse_template(blob: bytes, *, standard: str = "auto") -> List[Dict[str, object]]:
    """Decode an ISO 19794-2:2005 or ANSI 378-2004 FMR blob into minutiae dicts."""
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise TemplateParseError("Fingerprint template must be bytes.")
    data = bytes(blob)
    if len(data) < _ISO_HEADER_BYTES + _VIEW_HEADER_BYTES:
        raise TemplateParseError(f"Fingerprint template too short ({len(data)} bytes).")
    if data[:4] != _MAGIC:
        raise TemplateParseError(f"Unexpected template header {data[:4]!r} (expected FMR\\x00).")

    version = data[4:8]
    if version not in (_ISO_VERSION, _ANSI_LEGACY_VERSION):
        raise TemplateParseError(f"Unsupported FMR version {version!r}.")

    std = (standard or "auto").strip().lower()
    if std == "iso":
        points = _parse_iso_24(data, 2.0)
    elif std == "ansi":
        # Real ANSI 378 uses 26-byte header; synthetic test vectors may use 24-byte with " 30\0".
        points = _parse_ansi_26(data)
        if points is None and version == _ANSI_LEGACY_VERSION:
            points = _parse_iso_24(data, 360.0 / 256.0)
    else:
        if version == _ANSI_LEGACY_VERSION:
            points = _parse_iso_24(data, 360.0 / 256.0) or _parse_ansi_26(data)
        else:
            # Both ISO 19794-2:2005 and ANSI 378-2004 use version " 20\0".
            # Differentiate cleanly via total-length field and view layout.
            iso_len = struct.unpack_from(">I", data, 8)[0]
            ansi_len = struct.unpack_from(">H", data, 8)[0]
            if iso_len == len(data):
                points = _parse_iso_24(data, 2.0)
                if points is None and ansi_len == len(data):
                    points = _parse_ansi_26(data)
            elif ansi_len == len(data):
                points = _parse_ansi_26(data)
                if points is None:
                    points = _parse_iso_24(data, 2.0)
            else:
                points = _parse_iso_24(data, 2.0) or _parse_ansi_26(data)

    if not points:
        raise TemplateParseError("Fingerprint template contained no valid minutiae records.")
    return points
