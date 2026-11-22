#!/usr/bin/env python3
"""J-ONE Fingerprint Bridge — Windows 11 workstation service (DigitalPersona 4500).

WHAT THIS IS
============
The fingerprint reader is a USB device plugged into ONE Windows workstation.
No browser and no data-centre server can reach it. This small service runs on
that workstation, owns the reader through the vendor SDK (``dpfpdd.dll`` +
``dpfj.dll`` from the HID Global / DigitalPersona U.are.U SDK), and exposes a
loopback-only HTTP API that the J-ONE desk pages call directly:

    GET  /health      liveness of this service (no device access)
    GET  /status      honest reader status (device, driver, SDK, busy)
    POST /initialize  (re)enumerate + open the reader after a restart/replug
    POST /capture     capture one finger and return ISO/IEC 19794-2 minutiae

Security posture
----------------
* Binds 127.0.0.1 only by default — not reachable from the hotel LAN.
* Holds NO credential: it cannot verify, enrol, clock anyone in, or call the
  API. Verification stays server-side, on the JWT-authenticated endpoints.
* Only *minutiae* leave the process. The captured grayscale image is discarded
  immediately and is never written to disk.
* Origin allowlist (CORS) is explicit: https://jonehotels.com,
  https://www.jonehotels.com and local development origins. Any other page that
  tries to call the bridge gets no CORS grant, so a random site the operator
  visits cannot drive the reader.
* No simulation mode exists here. If the reader is absent or the SDK cannot be
  loaded, the bridge says so; it never fabricates a capture.

Installation, driver/SDK layout, the Windows service registration and the
troubleshooting matrix are in ``docs/WINDOWS_11_FINGERPRINT_SETUP.md``.

Run:
    python fingerprint_bridge_service.py                # start on 127.0.0.1:8787
    python fingerprint_bridge_service.py --diagnose     # print device/SDK report
    python fingerprint_bridge_service.py --port 8787 --allow-origin https://jonehotels.com

Requires: Windows 10/11, 64-bit Python 3.9+, DigitalPersona U.are.U 4500 driver
and the U.are.U SDK (which provides dpfpdd.dll and dpfj.dll). No third-party
Python packages.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import platform
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:  # Optional import: this module must stay importable on Linux/CI too.
    from . import iso19794_2
except ImportError:  # pragma: no cover - direct script execution
    import iso19794_2  # type: ignore

SERVICE_NAME = "jone-fingerprint-bridge"
SERVICE_VERSION = "2.0.0"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("JONE_FINGERPRINT_BRIDGE_PORT", "8787"))

DEFAULT_ALLOWED_ORIGINS = (
    "https://jonehotels.com",
    "https://www.jonehotels.com",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
)

# ---------------------------------------------------------------------------
# Reader status vocabulary — MUST match frontend/js/biometrics.js.
# ---------------------------------------------------------------------------
STATUS_CONNECTED = "connected"
STATUS_INITIALIZING = "initializing"
STATUS_NOT_CONNECTED = "not_connected"
STATUS_DRIVER_MISSING = "driver_missing"
STATUS_SDK_UNAVAILABLE = "sdk_unavailable"
STATUS_PERMISSION_DENIED = "permission_denied"
STATUS_BUSY = "busy"
STATUS_COMMUNICATION_FAILURE = "communication_failure"
# The vendor DLLs are present (the driver IS installed) but dpfpdd_init() did
# not succeed — in practice the DigitalPersona runtime service is stopped or
# still starting. Distinct from driver_missing because the repair is different:
# start a service, not reinstall a driver.
STATUS_SERVICE_UNAVAILABLE = "service_unavailable"

STATUS_DETAILS = {
    STATUS_CONNECTED: "Fingerprint reader connected and ready.",
    STATUS_INITIALIZING: "Fingerprint reader is initializing. Please wait.",
    STATUS_NOT_CONNECTED: "No fingerprint reader detected. Connect the DigitalPersona 4500 reader to this workstation.",
    STATUS_DRIVER_MISSING: "The DigitalPersona 4500 driver (dpfpdd.dll) was not found. Install the U.are.U driver/SDK on this workstation.",
    STATUS_SDK_UNAVAILABLE: "The reader driver is present but the DigitalPersona feature-extraction library (dpfj.dll) is not available.",
    STATUS_PERMISSION_DENIED: "Windows denied access to the fingerprint reader. Close other fingerprint software or run the bridge as an administrator.",
    STATUS_BUSY: "The fingerprint reader is busy with another capture.",
    STATUS_COMMUNICATION_FAILURE: "Communication with the fingerprint reader failed. Unplug and reconnect the reader, then press Retry.",
    STATUS_SERVICE_UNAVAILABLE: (
        "The DigitalPersona driver is installed but its Windows service is not running or did not "
        "answer, so the reader cannot be opened. Start the \"DigitalPersona\" service (services.msc) "
        "and replug the reader, then press Retry."
    ),
}

# dpfpdd.dll status codes. Only 0 (success) is treated as success; every other
# value is reported verbatim (hex) rather than being mapped to a guessed name,
# because the vendor's code list differs between SDK releases.
DPFPDD_OK = 0
# DPFPDD_E_DEVICE_FAILURE / DEVICE_MISSING / DEVICE_BUSY are the vendor's
# "device went away" family. They are grouped only to decide ONE thing: whether
# the handle must be re-opened before the next read (see _capture_raw).
DPFPDD_DEVICE_GONE = (0x05BA0001, 0x05BA0002, 0x05BA0003, 0x05BA0004)
DPFPDD_IMG_FMT_PIXEL_BUFFER = 1
DPFPDD_IMG_PROC_DEFAULT = 0
DPFPDD_IMG_RES_500 = 1
DPFPDD_MAX_IMAGE_BYTES = 500 * 1024  # generous: 500 dpi frames are ~250 KB

# dpfj.dll format identifiers. The candidate lists are tried in order and the
# winning combination is reported by --diagnose, so an SDK whose constants
# differ is detected on the workstation instead of being assumed.
FMD_FORMAT_CANDIDATES = (
    ("ISO_19794_2_2005", 0x001B0100),
    ("ANSI_378_2004", 0x001B0001),
)
FID_FORMAT_CANDIDATES = (
    ("ANSI_381_2004", 0x001B1011),
    ("ISO_19794_4_2005", 0x001B1110),
)

logger = logging.getLogger(SERVICE_NAME)


class ReaderError(Exception):
    """A reader operation failed. Carries a machine code and a user message.

    ``retry`` marks a transient device fault (the USB handle went stale after a
    replug or a driver restart). Only those may re-open the reader and capture
    again — a genuine "no finger on the sensor" timeout is reported as-is.
    """

    def __init__(self, code: str, message: str, status: str = STATUS_COMMUNICATION_FAILURE,
                 retry: bool = False, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry = retry
        self.extra = extra


# ---------------------------------------------------------------------------
# DLL discovery
# ---------------------------------------------------------------------------
def _candidate_dll_dirs() -> List[Path]:
    """Directories the U.are.U SDK is installed into, most specific first."""
    dirs: List[Path] = []
    override = (os.environ.get("JONE_DPFP_DLL_DIR") or "").strip()
    if override:
        dirs.append(Path(override))
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = (os.environ.get(env) or "").strip()
        if not base:
            continue
        dirs.extend([
            Path(base) / "DigitalPersona" / "U.are.U SDK" / "Windows" / "Lib" / "x64",
            Path(base) / "DigitalPersona" / "U.are.U SDK" / "Windows" / "Lib" / "x86",
            Path(base) / "HID Global" / "U.are.U SDK" / "Windows" / "Lib" / "x64",
            Path(base) / "HID Global" / "DigitalPersona" / "U.are.U SDK" / "Windows" / "Lib" / "x64",
            Path(base) / "Crossmatch" / "U.are.U SDK" / "Windows" / "Lib" / "x64",
            Path(base) / "DigitalPersona" / "U.are.U 4500 Fingerprint Reader",
            # The runtime (RTE) installer — the package the setup guide tells
            # the operator to verify with Test-Path — drops both DLLs in Bin.
            # The first revision never looked there, so a workstation that
            # passed the guide's own check still reported driver_missing.
            Path(base) / "DigitalPersona" / "Bin",
            Path(base) / "HID Global" / "DigitalPersona" / "Bin",
            Path(base) / "DigitalPersona" / "U.are.U SDK" / "Bin",
        ])
    system_root = (os.environ.get("SystemRoot") or r"C:\Windows")
    dirs.append(Path(system_root) / "System32")
    return [d for d in dirs if d and d.is_dir()]


def find_dll(name: str) -> Optional[Path]:
    """Locate a vendor DLL, honouring JONE_DPFP_DLL_DIR then the SDK layout."""
    override = (os.environ.get("JONE_DPFP_DLL_DIR") or "").strip()
    if override and (Path(override) / name).is_file():
        return Path(override) / name
    for directory in _candidate_dll_dirs():
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# ctypes structures (dpfpdd.h)
# ---------------------------------------------------------------------------
class _DPFPDD_CAPTURE_PARAM(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint32),
        ("image_fmt", ctypes.c_uint32),
        ("image_proc", ctypes.c_uint32),
        ("image_res", ctypes.c_uint32),
    ]


class _DPFPDD_CAPTURE_RESULT(ctypes.Structure):
    _fields_ = [
        ("success", ctypes.c_uint8),
        ("quality", ctypes.c_uint32),
        ("score", ctypes.c_uint32),
        ("info", ctypes.c_uint32),
    ]


class _DPFPDD_DEV_INFO(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint32),
        ("vendor_name", ctypes.c_char * 128),
        ("product_name", ctypes.c_char * 128),
        ("serial_num", ctypes.c_char * 16),
    ]


def _decode(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------
class DigitalPersonaReader:
    """Owns the attached DigitalPersona reader through the vendor SDK.

    All device access is serialised by ``self._lock``: two captures at once is
    the fastest way to make a reader look "broken", and the frontend reports
    ``busy`` honestly instead.
    """

    def __init__(self) -> None:
        # RLock, not Lock: capture() holds the lock while _capture_raw() may
        # call initialize() (which re-enters it) when the device was never
        # opened. With a plain Lock that first capture deadlocked forever
        # and every capture ended as a frontend BRIDGE_TIMEOUT.
        self._lock = threading.RLock()
        self._dd = None            # dpfpdd.dll handle
        self._fj = None            # dpfj.dll handle
        self._device = ctypes.c_void_p(0)
        self._device_open = False
        self._serial = ""
        self._product = ""
        self._last_error = ""
        self._fmd_format: Optional[Tuple[str, int]] = None
        self._fid_format: Optional[Tuple[str, int]] = None
        self._capturing = False
        # dpfpdd_init() outcome, kept apart from "the DLL is missing" — see
        # _load_dd(). Both are reported honestly by status().
        self._init_failed = False
        self._init_rc = 0
        self._dd_path: Optional[Path] = None

    # -- introspection -----------------------------------------------------
    def _load_dd(self) -> bool:
        if self._dd is not None:
            return True
        if platform.system() != "Windows":
            self._last_error = "The fingerprint bridge must run on Windows (this host is %s)." % platform.system()
            return False
        path = find_dll("dpfpdd.dll")
        if path is None:
            self._last_error = "dpfpdd.dll was not found. Install the DigitalPersona U.are.U driver/SDK."
            return False
        try:
            dd = ctypes.WinDLL(str(path))
        except OSError as exc:  # wrong architecture / blocked DLL
            self._last_error = "dpfpdd.dll could not be loaded (%s). Install the 64-bit U.are.U SDK." % exc
            return False
        dd.dpfpdd_init.restype = ctypes.c_int
        dd.dpfpdd_init.argtypes = []
        dd.dpfpdd_exit.restype = ctypes.c_int
        dd.dpfpdd_exit.argtypes = []
        dd.dpfpdd_query_devices.restype = ctypes.c_int
        dd.dpfpdd_query_devices.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(_DPFPDD_DEV_INFO)]
        dd.dpfpdd_open.restype = ctypes.c_int
        dd.dpfpdd_open.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p]
        dd.dpfpdd_close.restype = ctypes.c_int
        dd.dpfpdd_close.argtypes = [ctypes.c_void_p]
        dd.dpfpdd_capture.restype = ctypes.c_int
        dd.dpfpdd_capture.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_DPFPDD_CAPTURE_PARAM),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
            ctypes.POINTER(_DPFPDD_CAPTURE_RESULT),
        ]
        try:
            init_rc = dd.dpfpdd_init()
        except OSError as exc:  # pragma: no cover - vendor DLL init failure
            self._last_error = "dpfpdd_init failed (%s)." % exc
            return False
        # dpfpdd_init() returning an error is NOT the same fault as a missing
        # DLL, and the two need completely different repairs on the workstation.
        # This code is the one recorded when the DigitalPersona runtime service
        # is installed but not running/answered:
        #
        #   dpfpdd_init() -> DPFPDD_E_DEVICE_FAILURE (0x05BA0005)
        #
        # The DLL loaded, so the driver IS installed; nothing can enumerate the
        # reader until the vendor service is running. Reporting that as
        # "driver_missing" sent technicians to reinstall a driver that was
        # already present, so it gets its own status and its own sentence.
        if init_rc != DPFPDD_OK:
            self._last_error = "dpfpdd_init returned 0x%04X." % init_rc
            self._init_failed = True
            self._init_rc = init_rc
            # Keep the handle: dpfj (template extraction) is still usable and
            # /diagnose must be able to report the exact code.
            self._dd = dd
            self._dd_path = path
            return False
        self._init_failed = False
        self._init_rc = 0
        self._dd = dd
        self._dd_path = path
        return True

    def _load_fj(self) -> bool:
        if self._fj is not None:
            return True
        path = find_dll("dpfj.dll")
        if path is None:
            self._last_error = "dpfj.dll was not found. Install the complete U.are.U SDK (not only the driver)."
            return False
        try:
            fj = ctypes.WinDLL(str(path))
        except OSError as exc:
            self._last_error = "dpfj.dll could not be loaded (%s)." % exc
            return False
        for name in ("dpfj_create_fmd_from_raw", "dpfj_create_fid", "dpfj_create_fmd_from_fid"):
            fn = getattr(fj, name, None)
            if fn is not None:
                fn.restype = ctypes.c_int
        if getattr(fj, "dpfj_create_fmd_from_raw", None) is not None:
            fj.dpfj_create_fmd_from_raw.argtypes = [
                ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint32,
                ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.c_int, ctypes.c_int, ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_uint32),
            ]
        if getattr(fj, "dpfj_create_fid", None) is not None:
            fj.dpfj_create_fid.argtypes = [
                ctypes.c_uint32, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint32,
                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_uint32),
            ]
        if getattr(fj, "dpfj_create_fmd_from_fid", None) is not None:
            fj.dpfj_create_fmd_from_fid.argtypes = [
                ctypes.c_uint32, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint32,
                ctypes.c_uint32, ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_uint32),
            ]
        self._fj = fj
        return True

    def _enumerate(self) -> List[Dict[str, str]]:
        dd = self._dd
        if dd is None:
            return []
        count = ctypes.c_uint32(0)
        rc = dd.dpfpdd_query_devices(ctypes.byref(count), None)
        if rc != DPFPDD_OK or count.value == 0:
            if rc != DPFPDD_OK:
                self._last_error = "dpfpdd_query_devices returned 0x%04X." % rc
            return []
        infos = (_DPFPDD_DEV_INFO * count.value)()
        for info in infos:
            info.size = ctypes.sizeof(_DPFPDD_DEV_INFO)
        rc = dd.dpfpdd_query_devices(ctypes.byref(count), infos)
        if rc != DPFPDD_OK:
            self._last_error = "dpfpdd_query_devices returned 0x%04X." % rc
            return []
        devices = []
        for info in list(infos)[: count.value]:
            devices.append({
                "vendor": _decode(info.vendor_name),
                "product": _decode(info.product_name),
                "serial": _decode(info.serial_num),
            })
        return devices

    def _close_device(self) -> None:
        if self._device_open and self._dd is not None:
            try:
                self._dd.dpfpdd_close(self._device)
            except OSError:  # pragma: no cover - best effort on a dead handle
                pass
        self._device_open = False
        self._device = ctypes.c_void_p(0)
        self._serial = ""
        self._product = ""

    def is_capturing(self) -> bool:
        """True while a capture holds the reader (used by /status)."""
        return bool(self._capturing)

    def status(self) -> Dict[str, Any]:
        """Report device state without capturing. Cheap enough to poll.

        Never blocks on a capture: while a capture is in flight the busy flag
        is what the desk needs, and waiting for the sensor would make the page
        look frozen for the whole capture timeout.
        """
        base = {
            "status": STATUS_NOT_CONNECTED,
            "connected": False,
            "detail": STATUS_DETAILS[STATUS_NOT_CONNECTED],
            "device_serial": self._serial,
            "device_product": self._product,
            "capture_in_progress": self._capturing,
            "driver_installed": False,
            "sdk_loaded": False,
            "device_count": 0,
            "last_error": self._last_error,
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
        }
        if platform.system() != "Windows":
            base.update(status=STATUS_SDK_UNAVAILABLE,
                        detail="This bridge must run on the Windows workstation that has the reader attached.")
            return base
        if not self._load_dd():
            if self._init_failed:
                # DLLs found and loaded; dpfpdd_init() itself failed.
                base.update(status=STATUS_SERVICE_UNAVAILABLE,
                            detail=STATUS_DETAILS[STATUS_SERVICE_UNAVAILABLE],
                            driver_installed=True,
                            driver_dll=str(self._dd_path or ""),
                            init_status="0x%04X" % self._init_rc)
                base["last_error"] = self._last_error
                return base
            base.update(status=STATUS_DRIVER_MISSING, detail=STATUS_DETAILS[STATUS_DRIVER_MISSING])
            base["last_error"] = self._last_error
            return base
        base["driver_installed"] = True
        sdk = self._load_fj()
        base["sdk_loaded"] = sdk
        devices = self._enumerate()
        base["device_count"] = len(devices)
        if devices:
            base["device_serial"] = devices[0]["serial"]
            base["device_product"] = devices[0]["product"]
        if not devices:
            base.update(status=STATUS_NOT_CONNECTED, detail=STATUS_DETAILS[STATUS_NOT_CONNECTED])
            return base
        if not sdk:
            base.update(status=STATUS_SDK_UNAVAILABLE, detail=STATUS_DETAILS[STATUS_SDK_UNAVAILABLE])
            return base
        base.update(status=STATUS_CONNECTED, connected=True, detail=STATUS_DETAILS[STATUS_CONNECTED])
        if self._capturing:
            # A second capture attempt would be refused; say so now instead of
            # letting the page start (and time out) on a reader that is busy.
            base.update(status=STATUS_BUSY, captured_state=STATUS_CONNECTED,
                        detail=STATUS_DETAILS[STATUS_BUSY], capture_in_progress=True)
        return base

    def initialize(self) -> Dict[str, Any]:
        """(Re)open the reader — used after a replug or a bridge restart.

        Called from an HTTP thread while a capture may be running on another.
        A blocking acquire here made /initialize hang for the whole capture
        timeout (and the desk page treat the bridge as dead); instead it answers
        \"busy\" immediately and the caller retries after the capture.
        """
        if not self._lock.acquire(blocking=False):
            state = self.status()
            state.update(status=STATUS_BUSY, detail=STATUS_DETAILS[STATUS_BUSY],
                         busy=True, initialized=False)
            return state
        try:
            return self._initialize_locked()
        finally:
            self._lock.release()

    def _initialize_locked(self) -> Dict[str, Any]:
        if True:  # structure preserved: the body below runs under self._lock
            self._close_device()
            self._last_error = ""
            state = self.status()
            if not state.get("driver_installed"):
                return state
            devices = self._enumerate()
            if not devices:
                return state
            name = devices[0].get("serial") or devices[0].get("product") or ""
            rc = self._dd.dpfpdd_open(ctypes.byref(self._device), name.encode("utf-8"))
            if rc != DPFPDD_OK or not self._device.value:
                self._last_error = "dpfpdd_open returned 0x%04X." % rc
                mapped = STATUS_PERMISSION_DENIED if rc in (0x0A01, 0x0A02) else STATUS_COMMUNICATION_FAILURE
                state.update(status=mapped, connected=False,
                             detail=STATUS_DETAILS[mapped], last_error=self._last_error)
                return state
            self._device_open = True
            self._serial = devices[0].get("serial", "")
            self._product = devices[0].get("product", "")
            state.update(connected=True, status=STATUS_CONNECTED, detail=STATUS_DETAILS[STATUS_CONNECTED],
                         device_serial=self._serial, device_product=self._product, last_error="")
            return state

    # -- capture -----------------------------------------------------------
    def _capture_raw(self, timeout_ms: int) -> Tuple[bytes, int, int]:
        """Capture one frame; returns (gray pixels, width, height)."""
        if not self._device_open:
            state = self.initialize()
            if not state.get("connected"):
                raise ReaderError("READER_NOT_CONNECTED", state.get("detail") or STATUS_DETAILS[STATUS_NOT_CONNECTED],
                                  status=STATUS_NOT_CONNECTED)
        param = _DPFPDD_CAPTURE_PARAM(
            size=ctypes.sizeof(_DPFPDD_CAPTURE_PARAM),
            image_fmt=DPFPDD_IMG_FMT_PIXEL_BUFFER,
            image_proc=DPFPDD_IMG_PROC_DEFAULT,
            image_res=DPFPDD_IMG_RES_500,
        )
        buffer = (ctypes.c_ubyte * DPFPDD_MAX_IMAGE_BYTES)()
        result = _DPFPDD_CAPTURE_RESULT()
        deadline = time.monotonic() + max(1.0, timeout_ms / 1000.0)
        last_rc = 0
        while time.monotonic() < deadline:
            size = ctypes.c_uint32(DPFPDD_MAX_IMAGE_BYTES)
            rc = self._dd.dpfpdd_capture(self._device, ctypes.byref(param), ctypes.byref(size),
                                         buffer, ctypes.byref(result))
            last_rc = rc
            if rc in DPFPDD_DEVICE_GONE:
                # The USB handle is stale (reader replugged, driver restarted or
                # the device was reset). The handle must be closed and re-opened
                # or every later read fails the same way — which is exactly how
                # a replugged reader stayed "broken" until the bridge restarted.
                try:
                    self._close_device()
                except Exception:  # pragma: no cover - closing a dead handle
                    self._device_open = False
                raise ReaderError(
                    "READER_RECONNECT_NEEDED",
                    "The fingerprint reader was disconnected or re-plugged. Re-open the reader and try again.",
                    status=STATUS_COMMUNICATION_FAILURE,
                    retry=True,
                    reader_status=rc,
                )
            if rc == DPFPDD_OK and result.success:
                width, height = self._frame_dimensions(size.value)
                if width and height and size.value == width * height:
                    return bytes(buffer[: size.value]), width, height
                # Non-square / padded frame: ask the device for its geometry.
                width, height = 0, 0
                self._last_error = "Captured frame geometry could not be determined (%d bytes)." % size.value
            # A zero-quality or absent finger is a normal retry within the
            # timeout; keep waiting until the deadline.
            time.sleep(0.05)
        raise ReaderError(
            "READER_CAPTURE_TIMEOUT",
            "No fingerprint was captured within %d seconds (last reader status 0x%04X). "
            "Place the finger flat on the sensor and try again." % (int(timeout_ms / 1000), last_rc),
            status=STATUS_COMMUNICATION_FAILURE,
            reader_status=last_rc,
        )

    @staticmethod
    def _frame_dimensions(size_bytes: int) -> Tuple[int, int]:
        """Infer 500 dpi frame geometry from the pixel count.

        The U.are.U 4500 sensor is 356 x 344 px (``dpfpdd`` returns a raw 8-bit
        grayscale buffer). A few SDK builds expose other geometries, so the
        candidates are tried in order and an unknown size is reported instead of
        guessed.
        """
        known = [(356, 344), (352, 344), (356, 328), (300, 400), (256, 400), (384, 384)]
        for width, height in known:
            if width * height == size_bytes:
                return width, height
        for side in range(200, 513, 8):
            if side * side == size_bytes:
                return side, side
        return 0, 0

    def _extract_minutiae(self, pixels: bytes, width: int, height: int) -> List[Dict[str, object]]:
        fj = self._fj
        if fj is None:
            raise ReaderError("SDK_UNAVAILABLE", STATUS_DETAILS[STATUS_SDK_UNAVAILABLE], status=STATUS_SDK_UNAVAILABLE)
        image = (ctypes.c_ubyte * len(pixels)).from_buffer_copy(pixels)
        last_error = ""

        # Preferred path: raw image -> FMD (template) in one SDK call.
        creator = getattr(fj, "dpfj_create_fmd_from_raw", None)
        if creator is not None:
            for label, fmd_format in FMD_FORMAT_CANDIDATES:
                size = ctypes.c_uint32(0)
                rc = creator(image, len(pixels), width, height, 500, 0, 0, fmd_format, None, ctypes.byref(size))
                if rc != 0 or size.value == 0:
                    last_error = "dpfj_create_fmd_from_raw(%s) returned 0x%04X." % (label, rc)
                    continue
                buf = (ctypes.c_ubyte * size.value)()
                rc = creator(image, len(pixels), width, height, 500, 0, 0, fmd_format, buf, ctypes.byref(size))
                if rc != 0 or size.value == 0:
                    last_error = "dpfj_create_fmd_from_raw(%s) returned 0x%04X." % (label, rc)
                    continue
                try:
                    points = iso19794_2.parse_template(bytes(buf[: size.value]))
                except iso19794_2.TemplateParseError as exc:
                    last_error = "%s template could not be parsed (%s)." % (label, exc)
                    continue
                self._fmd_format = (label, fmd_format)
                return points

        # Fallback: raw image -> FID (image record) -> FMD.
        create_fid = getattr(fj, "dpfj_create_fid", None)
        create_fmd = getattr(fj, "dpfj_create_fmd_from_fid", None)
        if create_fid is not None and create_fmd is not None:
            for fid_label, fid_format in FID_FORMAT_CANDIDATES:
                fid_size = ctypes.c_uint32(0)
                rc = create_fid(fid_format, image, len(pixels), width, height, 500, None, ctypes.byref(fid_size))
                if rc != 0 or fid_size.value == 0:
                    last_error = "dpfj_create_fid(%s) returned 0x%04X." % (fid_label, rc)
                    continue
                fid = (ctypes.c_ubyte * fid_size.value)()
                rc = create_fid(fid_format, image, len(pixels), width, height, 500, fid, ctypes.byref(fid_size))
                if rc != 0 or fid_size.value == 0:
                    continue
                for label, fmd_format in FMD_FORMAT_CANDIDATES:
                    size = ctypes.c_uint32(0)
                    rc = create_fmd(fid_format, fid, ctypes.byref(fid_size), fmd_format, None, ctypes.byref(size))
                    if rc != 0 or size.value == 0:
                        last_error = "dpfj_create_fmd_from_fid(%s) returned 0x%04X." % (label, rc)
                        continue
                    buf = (ctypes.c_ubyte * size.value)()
                    rc = create_fmd(fid_format, fid, ctypes.byref(fid_size), fmd_format, buf, ctypes.byref(size))
                    if rc != 0 or size.value == 0:
                        continue
                    try:
                        points = iso19794_2.parse_template(bytes(buf[: size.value]))
                    except iso19794_2.TemplateParseError as exc:
                        last_error = "%s template could not be parsed (%s)." % (label, exc)
                        continue
                    self._fmd_format = (label, fmd_format)
                    self._fid_format = (fid_label, fid_format)
                    return points

        raise ReaderError(
            "TEMPLATE_EXTRACTION_FAILED",
            "The reader captured a finger but the SDK could not produce usable minutiae. %s "
            "Run the bridge with --diagnose and check the U.are.U SDK installation." % (last_error or ""),
            status=STATUS_COMMUNICATION_FAILURE,
        )

    def capture(self, timeout_ms: int = 30000, finger_position: str = "RIGHT_INDEX") -> Dict[str, Any]:
        """Capture one finger and return vitals + minutiae (never an image)."""
        if not self._lock.acquire(blocking=False):
            raise ReaderError("READER_BUSY", STATUS_DETAILS[STATUS_BUSY], status=STATUS_BUSY)
        self._capturing = True
        started = time.monotonic()
        try:
            if not self._load_dd():
                if self._init_failed:
                    raise ReaderError(
                        "DRIVER_SERVICE_UNAVAILABLE",
                        STATUS_DETAILS[STATUS_SERVICE_UNAVAILABLE] + " (%s)" % self._last_error,
                        status=STATUS_SERVICE_UNAVAILABLE,
                        retry=True,
                    )
                raise ReaderError("DRIVER_MISSING", STATUS_DETAILS[STATUS_DRIVER_MISSING], status=STATUS_DRIVER_MISSING)
            if not self._load_fj():
                raise ReaderError("SDK_UNAVAILABLE", STATUS_DETAILS[STATUS_SDK_UNAVAILABLE], status=STATUS_SDK_UNAVAILABLE)
            try:
                pixels, width, height = self._capture_raw(timeout_ms)
            except ReaderError as first:
                # ONLY a stale device handle is retried automatically, and only
                # once. The lock is already held here, so a second device open
                # cannot race another capture. A no-finger timeout is NOT
                # retried: it would double the caller's own timeout and tell
                # the operator nothing new.
                if not getattr(first, "retry", False):
                    raise
                logger.info("Re-opening the fingerprint reader after %s", first.code)
                state = self.initialize()
                if not state.get("connected"):
                    raise ReaderError(
                        "READER_RECONNECT_FAILED",
                        "The fingerprint reader was re-plugged but could not be re-opened: %s"
                        % (state.get("detail") or STATUS_DETAILS[STATUS_NOT_CONNECTED]),
                        status=state.get("status") or STATUS_COMMUNICATION_FAILURE,
                    ) from first
                pixels, width, height = self._capture_raw(timeout_ms)
            points = self._extract_minutiae(pixels, width, height)
            # The raw frame never leaves this function and is never written to
            # disk: only the minutiae below are returned.
            del pixels
            return {
                "success": True,
                "minutiae": points,
                "finger_position": finger_position,
                "count": len(points),
                "device_serial": self._serial,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "format": self._fmd_format[0] if self._fmd_format else "",
            }
        except ReaderError:
            raise
        except Exception as exc:  # noqa: BLE001 - report, never mask
            logger.exception("Unexpected capture failure")
            raise ReaderError("CAPTURE_FAILED", "The fingerprint capture failed unexpectedly (%s)." % exc) from exc
        finally:
            self._capturing = False
            self._lock.release()

    def diagnose(self) -> Dict[str, Any]:
        """Print-ready report used by ``--diagnose`` and the setup guide."""
        report: Dict[str, Any] = {
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "architecture": platform.machine(),
            "dpfpdd_dll": str(find_dll("dpfpdd.dll") or ""),
            "dpfj_dll": str(find_dll("dpfj.dll") or ""),
            "sdk_directories": [str(d) for d in _candidate_dll_dirs()],
            "last_error": self._last_error,
        }
        report["status"] = self.status()
        report["fmd_format"] = self._fmd_format[0] if self._fmd_format else ""
        report["fid_format"] = self._fid_format[0] if self._fid_format else ""
        return report

    def shutdown(self) -> None:
        with self._lock:
            self._close_device()
            if self._dd is not None:
                try:
                    self._dd.dpfpdd_exit()
                except OSError:  # pragma: no cover
                    pass


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------
def make_handler(reader: Any, allowed_origins: Iterable[str]) -> type:
    """Build a request handler bound to *reader* (injected for testability)."""
    origins = tuple(o.rstrip("/") for o in allowed_origins if o)

    class BridgeHandler(BaseHTTPRequestHandler):
        server_version = "%s/%s" % (SERVICE_NAME, SERVICE_VERSION)
        sys_version = ""
        protocol_version = "HTTP/1.1"
        max_body_bytes = 8192

        # -- helpers -------------------------------------------------------
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _origin(self) -> str:
            return (self.headers.get("Origin") or "").rstrip("/")

        def _cors_headers(self) -> Dict[str, str]:
            origin = self._origin()
            if origin and origin in origins:
                return {
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type",
                    "Access-Control-Max-Age": "600",
                    "Vary": "Origin",
                }
            return {"Vary": "Origin"}

        def _send(self, payload: Dict[str, Any], code: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in self._cors_headers().items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _origin_allowed(self) -> bool:
            origin = self._origin()
            return not origin or origin in origins

        def _read_json(self) -> Dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > self.max_body_bytes:
                raise ValueError("Request body is too large.")
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Request body must be JSON.") from exc
            return parsed if isinstance(parsed, dict) else {}

        # -- verbs ---------------------------------------------------------
        def do_OPTIONS(self) -> None:  # noqa: N802
            if not self._origin_allowed():
                self._send({"success": False, "error": "ORIGIN_NOT_ALLOWED",
                            "message": "This origin is not allowed to use the fingerprint bridge."}, 403)
                return
            self.send_response(204)
            for key, value in self._cors_headers().items():
                self.send_header(key, value)
            # Chrome/Edge Private Network Access: a public page (the hosted
            # desk) calling loopback sends a preflight carrying
            # Access-Control-Request-Private-Network, and the grant below is
            # what lets that preflight succeed. Loopback-only, and only ever
            # sent in answer to that exact request header.
            if (self.headers.get("Access-Control-Request-Private-Network") or "").strip().lower() == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            self._route("GET")

        def do_HEAD(self) -> None:  # noqa: N802
            self._route("HEAD")

        def do_POST(self) -> None:  # noqa: N802
            self._route("POST")

        def _route(self, method: str) -> None:
            if not self._origin_allowed():
                self._send({"success": False, "error": "ORIGIN_NOT_ALLOWED",
                            "message": "This origin is not allowed to use the fingerprint bridge."}, 403)
                return
            path = (self.path or "/").split("?", 1)[0].rstrip("/") or "/"
            try:
                if path in ("/", "/health"):
                    self._send({"success": True, "service": SERVICE_NAME, "version": SERVICE_VERSION,
                                "platform": platform.platform(), "loopback_only": True})
                elif path == "/status":
                    self._send({"success": True, **reader.status()})
                elif path == "/initialize":
                    if method != "POST":
                        self._send({"success": False, "error": "METHOD_NOT_ALLOWED",
                                    "message": "Use POST for /initialize."}, 405)
                        return
                    self._read_json_safe()
                    self._send({"success": True, **reader.initialize()})
                elif path == "/capture":
                    if method != "POST":
                        self._send({"success": False, "error": "METHOD_NOT_ALLOWED",
                                    "message": "Use POST for /capture."}, 405)
                        return
                    body = self._read_json_safe()
                    timeout_ms = int(body.get("timeout_ms") or 30000)
                    timeout_ms = max(5000, min(timeout_ms, 60000))
                    finger = str(body.get("finger_position") or "RIGHT_INDEX")
                    try:
                        result = reader.capture(timeout_ms=timeout_ms, finger_position=finger)
                    except ReaderError as exc:
                        self._send({"success": False, "error": exc.code, "message": exc.message,
                                    "status": exc.status, **exc.extra})
                        return
                    self._send({"success": True, **result})
                elif path == "/diagnose":
                    self._send({"success": True, **reader.diagnose()})
                else:
                    self._send({"success": False, "error": "NOT_FOUND",
                                "message": "Unknown bridge endpoint."}, 404)
            except ValueError as exc:
                self._send({"success": False, "error": "BAD_REQUEST", "message": str(exc)}, 400)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Bridge request failed: %s", path)
                self._send({"success": False, "error": "INTERNAL_ERROR",
                            "message": "The fingerprint bridge hit an internal error (%s)." % exc}, 500)

        def _read_json_safe(self) -> Dict[str, Any]:
            # Malformed JSON is a client error (HTTP 400 via _route), not an
            # empty body: silently treating it as {} made a broken caller
            # indistinguishable from a valid capture with default options.
            return self._read_json()

    return BridgeHandler


def serve(reader: Any, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
          allowed_origins: Iterable[str] = DEFAULT_ALLOWED_ORIGINS) -> ThreadingHTTPServer:
    handler = make_handler(reader, allowed_origins)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="J-ONE Fingerprint Bridge (DigitalPersona 4500)")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind address (default 127.0.0.1 — loopback only)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="TCP port (default 8787)")
    parser.add_argument("--allow-origin", action="append", default=None,
                        help="Browser origin allowed to call the bridge (repeatable)")
    parser.add_argument("--diagnose", action="store_true", help="Print a device/SDK report and exit")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    origins = tuple(args.allow_origin) if args.allow_origin else DEFAULT_ALLOWED_ORIGINS
    reader = DigitalPersonaReader()

    if args.diagnose:
        print(json.dumps(reader.diagnose(), indent=2))
        reader.shutdown()
        return 0

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning("Binding the fingerprint bridge to %s exposes the reader to the network. "
                       "Use loopback unless you understand the risk.", args.host)

    try:
        server = serve(reader, host=args.host, port=args.port, allowed_origins=origins)
    except OSError as exc:
        # Almost always "port already in use": a second copy of the bridge,
        # or the previous one never stopped. Name it instead of dumping a
        # traceback the reception operator cannot act on.
        logger.error(
            "The fingerprint bridge cannot listen on http://%s:%d (%s). "
            "If the bridge is already running, use that copy; otherwise stop "
            "the process holding the port or start with --port and set the "
            "same port in the desk page configuration.",
            args.host, args.port, exc,
        )
        reader.shutdown()
        return 2
    logger.info("%s %s listening on http://%s:%d (origins: %s)",
                SERVICE_NAME, SERVICE_VERSION, args.host, args.port, ", ".join(origins))
    state = reader.status()
    logger.info("Reader status at start: %s — %s", state.get("status"), state.get("detail"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping the fingerprint bridge.")
    finally:
        server.server_close()
        reader.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
