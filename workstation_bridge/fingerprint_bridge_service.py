"""J-ONE Hotel Management System — Workstation Fingerprint Bridge (Windows 11).

Runs ONLY on the reception/enrolment workstation as a single-instance loopback
HTTP service (``http://127.0.0.1:8787`` by default). The browser page served
from ``https://jonehotels.com`` calls this bridge to detect, initialize, and
capture from the attached DigitalPersona U.are.U 4500 USB fingerprint reader
without installing browser plugins or exposing the reader over the LAN.

Architecture & hardware invariants
----------------------------------
1. Windows-native USB monitoring (``SetupAPI`` + ``CfgMgr32`` + ``WM_DEVICECHANGE``)
   detects DigitalPersona hardware (``VID_05BA``) insertion, removal, PnP driver
   problem codes, and DigitalPersona Windows service (``DpHost``/``DpHostW``)
   state in real time.
2. DigitalPersona U.are.U SDK (``dpfpdd.dll`` + ``dpfj.dll``) ctypes bindings
   match the official SDK C ABI (``dpfpdd.h`` and ``dpfj.h``):
   - ``_DPFPDD_DEV_INFO`` with ``name[1024]``, ``descr`` (3x128), ``id`` (VID/PID),
     ``ver``, ``modality``, ``technology``;
   - ``dpfpdd_query_devices`` handling ``DPFPDD_E_MORE_DATA`` (``0x05BA000D``);
   - ``dpfpdd_open(dev_info.name, &dev)``;
   - ``dpfpdd_capture(dev, param, timeout_ms, result, &size, buf)`` with full
     ``_DPFPDD_CAPTURE_RESULT`` + ``_DPFPDD_IMAGE_INFO`` and ``PIXEL_BUFFER=0`` /
     ``500`` DPI;
   - ``dpfj_create_fmd_from_raw`` with ``DPFJ_FMD_ISO_19794_2_2005=0x01010001``
     (and ``DPFJ_FMD_ANSI_378_2004=0x001B0001`` fallback).
3. Background monitor thread automatically initializes newly connected readers,
   retries delayed Windows startup/driver init, and cleanly recovers/reconnects
   after USB unplug/replug without manual intervention or page refresh.
4. Security: binds ``127.0.0.1`` only, enforces an explicit Origin allowlist
   (never ``*``), sends ``Access-Control-Allow-Private-Network: true`` for
   Chromium Local Network Access, never synthesizes fake fingerprints, and
   returns extracted ISO/ANSI minutiae points only (never raw fingerprint images).
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import queue
import re
import signal
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import iso19794_2

# ---------------------------------------------------------------------------
# Service defaults & status vocabulary
# ---------------------------------------------------------------------------

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
SERVICE_VERSION = "2.1.0"
MIN_CAPTURE_TIMEOUT_MS = 5000
MAX_CAPTURE_TIMEOUT_MS = 60000
DEFAULT_ALLOWED_ORIGINS: Tuple[str, ...] = (
    "https://jonehotels.com",
    "https://www.jonehotels.com",
    "https://api.jonehotels.com",
)

DIGITALPERSONA_VID = 0x05BA
DIGITALPERSONA_PIDS: Dict[int, str] = {
    0x000A: "DigitalPersona U.are.U 4500 Fingerprint Reader",
    0x0007: "DigitalPersona U.are.U 4000B Fingerprint Reader",
    0x0008: "DigitalPersona U.are.U 4000 Fingerprint Reader",
    0x000B: "DigitalPersona U.are.U 5100 Fingerprint Reader",
    0x000C: "DigitalPersona U.are.U 5160 Fingerprint Reader",
}

STATUS_CONNECTED = "connected"
STATUS_NOT_CONNECTED = "not_connected"
STATUS_USB_DETECTED = "usb_detected"
STATUS_INITIALIZING = "initializing"
STATUS_RECONNECTING = "reconnecting"
STATUS_BUSY = "busy"
STATUS_DRIVER_MISSING = "driver_missing"
STATUS_SDK_UNAVAILABLE = "sdk_unavailable"
STATUS_SERVICE_UNAVAILABLE = "service_unavailable"
STATUS_COMMUNICATION_FAILURE = "communication_failure"

# User-facing labels aligned with the frontend UI state contract.
UI_LABELS: Dict[str, str] = {
    STATUS_NOT_CONNECTED: "Fingerprint reader not connected",
    STATUS_USB_DETECTED: "Reader detected",
    STATUS_INITIALIZING: "Initializing fingerprint reader",
    STATUS_RECONNECTING: "Reconnecting to fingerprint reader",
    STATUS_DRIVER_MISSING: "Driver/interface unavailable",
    STATUS_SDK_UNAVAILABLE: "Driver/interface unavailable",
    STATUS_SERVICE_UNAVAILABLE: "Biometric service unavailable",
    STATUS_COMMUNICATION_FAILURE: "Reconnecting to fingerprint reader",
    STATUS_CONNECTED: "Fingerprint reader ready",
    STATUS_BUSY: "Reading fingerprint",
}

STATUS_DETAILS: Dict[str, str] = {
    STATUS_CONNECTED: "Fingerprint reader connected and ready.",
    STATUS_NOT_CONNECTED: (
        "No fingerprint reader detected. Connect the DigitalPersona 4500 USB "
        "reader to this workstation."
    ),
    STATUS_USB_DETECTED: (
        "DigitalPersona USB reader detected on this workstation. Waiting for "
        "driver interface..."
    ),
    STATUS_INITIALIZING: "Initializing DigitalPersona fingerprint reader...",
    STATUS_RECONNECTING: (
        "Reconnecting to the DigitalPersona fingerprint reader after a device "
        "or USB state change..."
    ),
    STATUS_BUSY: "The fingerprint reader is busy.",
    STATUS_DRIVER_MISSING: (
        "DigitalPersona driver/SDK runtime (dpfpdd.dll) is unavailable on this "
        "workstation. Install or repair the DigitalPersona U.are.U runtime once."
    ),
    STATUS_SDK_UNAVAILABLE: (
        "DigitalPersona feature extraction SDK (dpfj.dll) is unavailable on this "
        "workstation."
    ),
    STATUS_SERVICE_UNAVAILABLE: (
        "The DigitalPersona driver is installed, but its Windows biometric "
        "service (DpHost) is not running or failed to initialize."
    ),
    STATUS_COMMUNICATION_FAILURE: (
        "The workstation lost communication with the fingerprint reader. "
        "Reconnecting automatically..."
    ),
}

# ---------------------------------------------------------------------------
# DigitalPersona U.are.U SDK constants (from dpfpdd.h & dpfj.h)
# ---------------------------------------------------------------------------

DPFPDD_OK = 0
DPFPDD_E_FAILURE = 0x05BA000B
DPFPDD_E_MORE_DATA = 0x05BA000D
DPFPDD_E_INVALID_PARAMETER = 0x05BA0014
DPFPDD_E_INVALID_DEVICE = 0x05BA0015
DPFPDD_E_DEVICE_BUSY = 0x05BA001E
DPFPDD_E_DEVICE_FAILURE = 0x05BA001F

DPFPDD_DEVICE_GONE: Tuple[int, ...] = (
    DPFPDD_E_INVALID_DEVICE,
    DPFPDD_E_DEVICE_FAILURE,
    DPFPDD_E_FAILURE,
    # Legacy DigitalPersona error aliases retained for older SDK builds:
    0x05BA0001,
    0x05BA0002,
    0x05BA0003,
    0x05BA0004,
)

DPFPDD_STATUS_READY = 0
DPFPDD_STATUS_BUSY = 1
DPFPDD_STATUS_NEED_CALIBRATION = 2
DPFPDD_STATUS_FAILURE = 3

DPFPDD_IMG_FMT_PIXEL_BUFFER = 0
DPFPDD_IMG_FMT_ANSI381 = 0x001B0401
DPFPDD_IMG_FMT_ISOIEC19794 = 0x01010007

DPFPDD_IMG_PROC_DEFAULT = 0
DPFPDD_IMG_PROC_PIV = 1
DPFPDD_IMG_PROC_ENHANCED = 2
DPFPDD_IMG_RES_500 = 500

DPFPDD_QUALITY_GOOD = 0
DPFPDD_QUALITY_NO_FINGER = 1 << 1
DPFPDD_QUALITY_FAKE_FINGER = 1 << 2
DPFPDD_QUALITY_TOO_SMALL = 1 << 3
DPFPDD_QUALITY_TOO_LIGHT = 1 << 4
DPFPDD_QUALITY_TOO_DARK = 1 << 5
DPFPDD_QUALITY_TOO_NOISY = 1 << 6
DPFPDD_QUALITY_LOW_CONTRAST = 1 << 7
DPFPDD_QUALITY_NOT_ENOUGH_FEATURES = 1 << 8
DPFPDD_QUALITY_NOT_CENTERED = 1 << 9
DPFPDD_QUALITY_NOT_A_FINGER = 1 << 10
DPFPDD_QUALITY_TOO_HIGH = 1 << 11
DPFPDD_QUALITY_TOO_LOW = 1 << 12
DPFPDD_QUALITY_TOO_LEFT = 1 << 13
DPFPDD_QUALITY_TOO_RIGHT = 1 << 14
DPFPDD_QUALITY_TOO_STRANGE = 1 << 15
DPFPDD_QUALITY_TOO_FAST = 1 << 16
DPFPDD_QUALITY_TOO_SKEWED = 1 << 17
DPFPDD_QUALITY_TOO_SHORT = 1 << 18
DPFPDD_QUALITY_TOO_SLOW = 1 << 19
DPFPDD_QUALITY_SCAN_FAILURE = 1 << 31

DPFJ_OK = 0
DPFJ_E_MORE_DATA = 0x05BA000D
DPFJ_FMD_ANSI_378_2004 = 0x001B0001
DPFJ_FMD_ISO_19794_2_2005 = 0x01010001
DPFJ_FID_ANSI_381_2004 = 0x001B0401
DPFJ_FID_ISO_19794_4_2005 = 0x01010007

FINGER_POSITION_CODES: Dict[str, int] = {
    "UNKNOWN": 0,
    "RIGHT_THUMB": 1,
    "RIGHT_INDEX": 2,
    "RIGHT_MIDDLE": 3,
    "RIGHT_RING": 4,
    "RIGHT_LITTLE": 5,
    "LEFT_THUMB": 6,
    "LEFT_INDEX": 7,
    "LEFT_MIDDLE": 8,
    "LEFT_RING": 9,
    "LEFT_LITTLE": 10,
}

# Candidate installation folders for DigitalPersona U.are.U / HID SDK on Windows.
_WINDOWS_DLL_DIRS: Tuple[Path, ...] = (
    Path(r"C:\Program Files\DigitalPersona\Bin"),
    Path(r"C:\Program Files\DigitalPersona\U.are.U SDK\Windows\Lib\x64"),
    Path(r"C:\Program Files\DigitalPersona\U.are.U SDK\Windows\Lib\win32"),
    Path(r"C:\Program Files\HID Global\DigitalPersona\U.are.U SDK\Windows\Lib\x64"),
    Path(r"C:\Program Files\HID Global\DigitalPersona\U.are.U SDK\Windows\Lib\win32"),
    Path(r"C:\Program Files\HID Global\Authentication\Bin"),
    Path(r"C:\Program Files (x86)\DigitalPersona\Bin"),
    Path(r"C:\Program Files (x86)\DigitalPersona\U.are.U SDK\Windows\Lib\win32"),
    Path(r"C:\Windows\System32"),
)

# ---------------------------------------------------------------------------
# DigitalPersona U.are.U ctypes structures (dpfpdd.h)
# ---------------------------------------------------------------------------


class _DPFPDD_VER_INFO(ctypes.Structure):
    _fields_ = [
        ("major", ctypes.c_int),
        ("minor", ctypes.c_int),
        ("maintenance", ctypes.c_int),
    ]


class _DPFPDD_VERSION(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint),
        ("lib_ver", _DPFPDD_VER_INFO),
        ("api_ver", _DPFPDD_VER_INFO),
    ]


class _DPFPDD_HW_VERSION(ctypes.Structure):
    _fields_ = [
        ("hw_ver", _DPFPDD_VER_INFO),
        ("fw_ver", _DPFPDD_VER_INFO),
        ("bcd_rev", ctypes.c_uint16),
    ]


class _DPFPDD_DEV_DESCR(ctypes.Structure):
    _fields_ = [
        ("vendor_name", ctypes.c_char * 128),
        ("product_name", ctypes.c_char * 128),
        ("serial_num", ctypes.c_char * 128),
    ]


class _DPFPDD_DEV_ID(ctypes.Structure):
    _fields_ = [
        ("vendor_id", ctypes.c_uint16),
        ("product_id", ctypes.c_uint16),
    ]


class _DPFPDD_DEV_INFO(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint),
        ("name", ctypes.c_char * 1024),
        ("descr", _DPFPDD_DEV_DESCR),
        ("id", _DPFPDD_DEV_ID),
        ("ver", _DPFPDD_HW_VERSION),
        ("modality", ctypes.c_uint),
        ("technology", ctypes.c_uint),
    ]


class _DPFPDD_DEV_STATUS(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint),
        ("status", ctypes.c_uint),
        ("finger_detected", ctypes.c_int),
        ("data", ctypes.c_ubyte * 1),
    ]


class _DPFPDD_CAPTURE_PARAM(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint),
        ("image_fmt", ctypes.c_uint),
        ("image_proc", ctypes.c_uint),
        ("image_res", ctypes.c_uint),
    ]


class _DPFPDD_IMAGE_INFO(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint),
        ("width", ctypes.c_uint),
        ("height", ctypes.c_uint),
        ("res", ctypes.c_uint),
        ("bpp", ctypes.c_uint),
    ]


class _DPFPDD_CAPTURE_RESULT(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint),
        ("success", ctypes.c_int),
        ("quality", ctypes.c_uint),
        ("score", ctypes.c_uint),
        ("info", _DPFPDD_IMAGE_INFO),
    ]


# ---------------------------------------------------------------------------
# Logging & Windows single-instance / diagnostics helpers
# ---------------------------------------------------------------------------

LOGGER = logging.getLogger("jone.fingerprint_bridge")


def default_log_path() -> Path:
    if platform.system() == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or ".")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "JONE" / "fingerprint_bridge.log"


def configure_logging(log_file: Optional[Path] = None, verbose: bool = False) -> None:
    if LOGGER.handlers:
        return
    LOGGER.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(fmt)
    LOGGER.addHandler(stream_handler)
    target = log_file or default_log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            target, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        LOGGER.addHandler(file_handler)
    except OSError:
        pass


def _decode_cstr(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()


def _format_rc(rc: int) -> str:
    return f"0x{int(rc) & 0xFFFFFFFF:08X}"


def _quality_explanation(quality: int) -> str:
    if quality == DPFPDD_QUALITY_GOOD:
        return "Capture did not succeed."
    reasons = []
    if quality & DPFPDD_QUALITY_NO_FINGER:
        reasons.append("no finger detected on the sensor")
    if quality & DPFPDD_QUALITY_FAKE_FINGER:
        reasons.append("sensor rejected sample as non-live")
    if quality & DPFPDD_QUALITY_TOO_SMALL:
        reasons.append("fingerprint contact area was too small")
    if quality & DPFPDD_QUALITY_TOO_LIGHT:
        reasons.append("print was too faint — press slightly firmer")
    if quality & DPFPDD_QUALITY_TOO_DARK:
        reasons.append("print was too dark or wet — wipe sensor and finger")
    if quality & DPFPDD_QUALITY_TOO_NOISY:
        reasons.append("image was noisy — clean the sensor window")
    if quality & DPFPDD_QUALITY_LOW_CONTRAST:
        reasons.append("ridge contrast was too low")
    if quality & DPFPDD_QUALITY_NOT_ENOUGH_FEATURES:
        reasons.append("not enough ridge minutiae detected — center the finger pad")
    if quality & DPFPDD_QUALITY_NOT_CENTERED:
        reasons.append("finger was not centered on the glass")
    if quality & DPFPDD_QUALITY_NOT_A_FINGER:
        reasons.append("object on sensor was not a fingerprint")
    if quality & (DPFPDD_QUALITY_TOO_HIGH | DPFPDD_QUALITY_TOO_LOW | DPFPDD_QUALITY_TOO_LEFT | DPFPDD_QUALITY_TOO_RIGHT):
        reasons.append("finger was off-center on the sensor")
    if quality & (DPFPDD_QUALITY_TOO_FAST | DPFPDD_QUALITY_TOO_SHORT):
        reasons.append("finger was lifted too quickly")
    if quality & DPFPDD_QUALITY_SCAN_FAILURE:
        reasons.append("sensor scan failed")
    if not reasons:
        return f"Fingerprint capture quality check failed (code {_format_rc(quality)})."
    return "Fingerprint capture failed: " + "; ".join(reasons) + ". Place the finger flat on the sensor and try again."


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ReaderError(Exception):
    """Structured hardware/driver/capture exception mapped cleanly to HTTP JSON."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: str = STATUS_COMMUNICATION_FAILURE,
        reader_status: Optional[int] = None,
        retry: bool = False,
        http_status: int = 502,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.reader_status = reader_status
        self.retry = retry
        self.http_status = http_status


# ---------------------------------------------------------------------------
# Windows-native USB device & service detection (SetupAPI + CfgMgr32 + SCM)
# ---------------------------------------------------------------------------

_VID_PID_RE = re.compile(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})", re.IGNORECASE)


class UsbSnapshot:
    """Point-in-time snapshot of DigitalPersona USB hardware & Windows service."""

    def __init__(
        self,
        *,
        usb_present: bool,
        devices: Optional[List[Dict[str, Any]]] = None,
        driver_problem_code: Optional[int] = None,
        service_running: Optional[bool] = None,
        service_name: str = "",
        detection_method: str = "none",
    ) -> None:
        self.usb_present = bool(usb_present)
        self.devices = list(devices or [])
        self.driver_problem_code = driver_problem_code
        self.service_running = service_running
        self.service_name = service_name
        self.detection_method = detection_method

    def to_dict(self) -> Dict[str, Any]:
        return {
            "usb_present": self.usb_present,
            "devices": self.devices,
            "driver_problem_code": self.driver_problem_code,
            "service_running": self.service_running,
            "service_name": self.service_name,
            "detection_method": self.detection_method,
        }


class BaseUsbMonitor:
    """Abstract USB & service monitor; subclassed for Windows and test doubles."""

    def inspect(self) -> UsbSnapshot:
        return UsbSnapshot(usb_present=False, detection_method="unsupported")

    def start_event_listener(self, on_change: Callable[[], None]) -> None:
        return None

    def stop_event_listener(self) -> None:
        return None


class WindowsUsbMonitor(BaseUsbMonitor):
    """Pure-ctypes Windows PnP SetupAPI + CfgMgr32 + WM_DEVICECHANGE + SCM monitor."""

    DIGCF_PRESENT = 0x00000002
    DIGCF_ALLCLASSES = 0x00000004
    SPDRP_DEVICEDESC = 0x00000000
    SPDRP_HARDWAREID = 0x00000001
    SPDRP_SERVICE = 0x00000004
    SPDRP_MFG = 0x0000000B
    DN_HAS_PROBLEM = 0x00000400

    SC_MANAGER_CONNECT = 0x0001
    SERVICE_QUERY_STATUS = 0x0004
    SERVICE_RUNNING = 0x00000004

    WM_DEVICECHANGE = 0x0219
    WM_QUIT = 0x0012
    DBT_DEVICEARRIVAL = 0x8000
    DBT_DEVICEREMOVECOMPLETE = 0x8004
    DBT_DEVNODES_CHANGED = 0x0007
    DBT_DEVTYP_DEVICEINTERFACE = 0x00000005
    DEVICE_NOTIFY_WINDOW_HANDLE = 0x00000000
    HWND_MESSAGE = ctypes.c_void_p(-3)

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._hwnd = None
        self._tid: Optional[int] = None

    def inspect(self) -> UsbSnapshot:
        if platform.system() != "Windows":
            return UsbSnapshot(usb_present=False, detection_method="non_windows")
        devices, problem_code = self._enumerate_pnp_usb()
        svc_running, svc_name = self._query_dp_service()
        return UsbSnapshot(
            usb_present=bool(devices),
            devices=devices,
            driver_problem_code=problem_code,
            service_running=svc_running,
            service_name=svc_name,
            detection_method="windows_setupapi",
        )

    def _enumerate_pnp_usb(self) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        found: List[Dict[str, Any]] = []
        first_problem: Optional[int] = None
        try:
            setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
            cfgmgr32 = ctypes.WinDLL("cfgmgr32", use_last_error=True)
        except (OSError, AttributeError):
            return found, None

        class SP_DEVINFO_DATA(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("ClassGuid", ctypes.c_ubyte * 16),
                ("DevInst", wintypes.DWORD),
                ("Reserved", ctypes.c_size_t),
            ]

        setupapi.SetupDiGetClassDevsW.argtypes = [
            ctypes.c_void_p, wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD
        ]
        setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
        setupapi.SetupDiEnumDeviceInfo.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(SP_DEVINFO_DATA)
        ]
        setupapi.SetupDiEnumDeviceInfo.restype = wintypes.BOOL
        setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(SP_DEVINFO_DATA),
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        setupapi.SetupDiGetDeviceRegistryPropertyW.restype = wintypes.BOOL
        setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
        setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

        cfgmgr32.CM_Get_DevNode_Status.argtypes = [
            ctypes.POINTER(wintypes.ULONG),
            ctypes.POINTER(wintypes.ULONG),
            wintypes.DWORD,
            wintypes.ULONG,
        ]
        cfgmgr32.CM_Get_DevNode_Status.restype = wintypes.DWORD

        invalid_handle = ctypes.c_void_p(-1).value
        hdev = setupapi.SetupDiGetClassDevsW(
            None, "USB", None, self.DIGCF_PRESENT | self.DIGCF_ALLCLASSES
        )
        if not hdev or hdev == invalid_handle:
            return found, None

        def _get_prop(dev_info, prop_id: int) -> str:
            buf = ctypes.create_unicode_buffer(1024)
            req = wintypes.DWORD(0)
            ok = setupapi.SetupDiGetDeviceRegistryPropertyW(
                hdev, ctypes.byref(dev_info), prop_id, None,
                buf, ctypes.sizeof(buf), ctypes.byref(req),
            )
            if not ok:
                return ""
            # MULTI_SZ hardware IDs contain embedded NULs; replace with spaces.
            raw = ctypes.wstring_at(buf, max(0, (req.value // 2) - 1))
            return raw.replace("\x00", " ").strip()

        try:
            idx = 0
            dev_info = SP_DEVINFO_DATA()
            dev_info.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
            while setupapi.SetupDiEnumDeviceInfo(hdev, idx, ctypes.byref(dev_info)):
                idx += 1
                hw_id = _get_prop(dev_info, self.SPDRP_HARDWAREID)
                if not hw_id:
                    continue
                match = _VID_PID_RE.search(hw_id)
                if not match:
                    continue
                vid = int(match.group(1), 16)
                pid = int(match.group(2), 16)
                if vid != DIGITALPERSONA_VID:
                    continue
                desc = _get_prop(dev_info, self.SPDRP_DEVICEDESC)
                mfg = _get_prop(dev_info, self.SPDRP_MFG)
                svc = _get_prop(dev_info, self.SPDRP_SERVICE)
                status_flags = wintypes.ULONG(0)
                problem_num = wintypes.ULONG(0)
                cm_rc = cfgmgr32.CM_Get_DevNode_Status(
                    ctypes.byref(status_flags),
                    ctypes.byref(problem_num),
                    dev_info.DevInst,
                    0,
                )
                has_problem = (cm_rc == 0) and bool(status_flags.value & self.DN_HAS_PROBLEM)
                pcode = int(problem_num.value) if has_problem else None
                if pcode is not None and first_problem is None:
                    first_problem = pcode
                found.append({
                    "vid": f"0x{vid:04X}",
                    "pid": f"0x{pid:04X}",
                    "product": DIGITALPERSONA_PIDS.get(pid, desc or "DigitalPersona USB Reader"),
                    "manufacturer": mfg,
                    "service": svc,
                    "hardware_id": hw_id.split(" ")[0],
                    "problem_code": pcode,
                })
        finally:
            setupapi.SetupDiDestroyDeviceInfoList(hdev)
        return found, first_problem

    def _query_dp_service(self) -> Tuple[Optional[bool], str]:
        try:
            advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        except (OSError, AttributeError):
            return None, ""

        class SERVICE_STATUS(ctypes.Structure):
            _fields_ = [
                ("dwServiceType", wintypes.DWORD),
                ("dwCurrentState", wintypes.DWORD),
                ("dwControlsAccepted", wintypes.DWORD),
                ("dwWin32ExitCode", wintypes.DWORD),
                ("dwServiceSpecificExitCode", wintypes.DWORD),
                ("dwCheckPoint", wintypes.DWORD),
                ("dwWaitHint", wintypes.DWORD),
            ]

        advapi32.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        advapi32.OpenSCManagerW.restype = ctypes.c_void_p
        advapi32.OpenServiceW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.DWORD]
        advapi32.OpenServiceW.restype = ctypes.c_void_p
        advapi32.QueryServiceStatus.argtypes = [ctypes.c_void_p, ctypes.POINTER(SERVICE_STATUS)]
        advapi32.QueryServiceStatus.restype = wintypes.BOOL
        advapi32.CloseServiceHandle.argtypes = [ctypes.c_void_p]
        advapi32.CloseServiceHandle.restype = wintypes.BOOL

        scm = advapi32.OpenSCManagerW(None, None, self.SC_MANAGER_CONNECT)
        if not scm:
            return None, ""
        try:
            candidates = (
                "DpHost",
                "DpHostW",
                "DigitalPersona Authentication Service",
                "WbioSrvc",
            )
            found_any = False
            for name in candidates:
                svc = advapi32.OpenServiceW(scm, name, self.SERVICE_QUERY_STATUS)
                if not svc:
                    continue
                found_any = True
                try:
                    st = SERVICE_STATUS()
                    if advapi32.QueryServiceStatus(svc, ctypes.byref(st)):
                        if st.dwCurrentState == self.SERVICE_RUNNING:
                            return True, name
                        return False, name
                finally:
                    advapi32.CloseServiceHandle(svc)
            return (False, "") if not found_any else (None, "")
        finally:
            advapi32.CloseServiceHandle(scm)

    def start_event_listener(self, on_change: Callable[[], None]) -> None:
        if platform.system() != "Windows" or self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_message_loop,
            args=(on_change,),
            name="jone-usb-wm-devicechange",
            daemon=True,
        )
        self._thread.start()

    def stop_event_listener(self) -> None:
        self._stop_event.set()
        if platform.system() == "Windows" and self._tid is not None:
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=False)
                user32.PostThreadMessageW(self._tid, self.WM_QUIT, 0, 0)
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run_message_loop(self, on_change: Callable[[], None]) -> None:
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._tid = int(kernel32.GetCurrentThreadId())

            WNDPROC = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
            )

            class WNDCLASSEXW(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.UINT),
                    ("style", wintypes.UINT),
                    ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HCURSOR),
                    ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR),
                    ("hIconSm", wintypes.HICON),
                ]

            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8),
                ]

            class DEV_BROADCAST_DEVICEINTERFACE_W(ctypes.Structure):
                _fields_ = [
                    ("dbcc_size", wintypes.DWORD),
                    ("dbcc_devicetype", wintypes.DWORD),
                    ("dbcc_reserved", wintypes.DWORD),
                    ("dbcc_classguid", GUID),
                    ("dbcc_name", ctypes.c_wchar * 1),
                ]

            def wndproc(hwnd, msg, wparam, lparam):
                if msg == self.WM_DEVICECHANGE and wparam in (
                    self.DBT_DEVICEARRIVAL,
                    self.DBT_DEVICEREMOVECOMPLETE,
                    self.DBT_DEVNODES_CHANGED,
                ):
                    try:
                        on_change()
                    except Exception:
                        pass
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            cb = WNDPROC(wndproc)
            hinst = kernel32.GetModuleHandleW(None)
            cls_name = f"JoneUsbWatch_{os.getpid()}"
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = cb
            wc.hInstance = hinst
            wc.lpszClassName = cls_name
            if not user32.RegisterClassExW(ctypes.byref(wc)):
                return

            hwnd = user32.CreateWindowExW(
                0, cls_name, "JONE USB Watch", 0, 0, 0, 0, 0,
                self.HWND_MESSAGE, None, hinst, None,
            )
            if not hwnd:
                return
            self._hwnd = hwnd

            # GUID_DEVINTERFACE_USB_DEVICE = {A5DCBF10-6530-11D2-901F-00C04FB951ED}
            usb_guid = GUID(
                0xA5DCBF10, 0x6530, 0x11D2,
                (ctypes.c_ubyte * 8)(0x90, 0x1F, 0x00, 0xC0, 0x4F, 0xB9, 0x51, 0xED),
            )
            flt = DEV_BROADCAST_DEVICEINTERFACE_W()
            flt.dbcc_size = ctypes.sizeof(DEV_BROADCAST_DEVICEINTERFACE_W)
            flt.dbcc_devicetype = self.DBT_DEVTYP_DEVICEINTERFACE
            flt.dbcc_classguid = usb_guid
            hnotify = user32.RegisterDeviceNotificationW(
                hwnd, ctypes.byref(flt), self.DEVICE_NOTIFY_WINDOW_HANDLE
            )
            msg = wintypes.MSG()
            while not self._stop_event.is_set():
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            if hnotify:
                user32.UnregisterDeviceNotification(hnotify)
            user32.DestroyWindow(hwnd)
        except Exception as exc:
            LOGGER.debug("WM_DEVICECHANGE monitor exited: %s", exc)


# ---------------------------------------------------------------------------
# DigitalPersona U.are.U Reader Controller + Auto-Init / Reconnect Monitor
# ---------------------------------------------------------------------------


def _find_dll(name: str, extra_dirs: Iterable[Path] = ()) -> Optional[Path]:
    candidates = []
    for env_key in ("JONE_DPFP_DLL_DIR", "JONE_DIGITALPERSONA_DLL_DIR"):
        env_override = os.environ.get(env_key)
        if env_override:
            candidates.append(Path(env_override))
    candidates.extend(extra_dirs)
    candidates.extend(_WINDOWS_DLL_DIRS)
    for folder in candidates:
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None


class DigitalPersonaReader:
    """Thread-safe controller for the DigitalPersona U.are.U 4500 reader."""

    def __init__(
        self,
        *,
        usb_monitor: Optional[BaseUsbMonitor] = None,
        poll_interval_s: float = 1.5,
        auto_monitor: bool = False,
    ) -> None:
        self._lock = threading.RLock()
        self._usb_monitor: BaseUsbMonitor = usb_monitor or WindowsUsbMonitor()
        self._poll_interval_s = max(0.2, float(poll_interval_s))

        self._dd: Any = None
        self._fj: Any = None
        self._dd_path: Optional[Path] = None
        self._fj_path: Optional[Path] = None
        self._init_succeeded = False
        self._init_failed = False
        self._init_rc: Optional[int] = None

        self._device: Optional[ctypes.c_void_p] = None
        self._device_open = False
        self._device_name: bytes = b""
        self._device_serial = ""
        self._device_product = ""
        self._device_vendor = ""
        self._device_count = 0

        self._capture_in_progress = False
        self._initializing = False
        self._reconnecting = False
        self._last_error = ""
        self._last_usb: UsbSnapshot = UsbSnapshot(usb_present=False)
        self._reconnect_attempts = 0
        self._next_retry_monotonic = 0.0

        self._stop_monitor = threading.Event()
        self._wake_monitor = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        self._listeners_lock = threading.Lock()
        self._listeners: List[queue.Queue] = []
        self._last_emitted_state: Tuple[Any, ...] = ()

        if auto_monitor:
            self.start_monitor()

    # ---- Status event subscription (used by GET /events SSE) --------------

    def subscribe_events(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=32)
        with self._listeners_lock:
            self._listeners.append(q)
        return q

    def unsubscribe_events(self, q: queue.Queue) -> None:
        with self._listeners_lock:
            try:
                self._listeners.remove(q)
            except ValueError:
                pass

    def _emit_state_if_changed(self) -> None:
        snap = self.status()
        sig = (
            snap.get("status"),
            snap.get("connected"),
            snap.get("usb_detected"),
            snap.get("device_serial"),
            snap.get("capture_in_progress"),
            snap.get("last_error"),
        )
        if sig == self._last_emitted_state:
            return
        self._last_emitted_state = sig
        with self._listeners_lock:
            targets = list(self._listeners)
        for q in targets:
            try:
                q.put_nowait(snap)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(snap)
                except queue.Empty:
                    pass

    # ---- Background USB + Driver Auto-Init & Reconnection Monitor ---------

    def start_monitor(self) -> None:
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return
        self._stop_monitor.clear()
        self._usb_monitor.start_event_listener(self._wake_monitor.set)
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="jone-reader-monitor",
            daemon=True,
        )
        self._monitor_thread.start()

    def stop_monitor(self) -> None:
        self._stop_monitor.set()
        self._wake_monitor.set()
        self._usb_monitor.stop_event_listener()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=2.0)
            self._monitor_thread = None

    def _monitor_loop(self) -> None:
        while not self._stop_monitor.is_set():
            try:
                self.reconcile_once()
            except Exception as exc:
                LOGGER.debug("Reader monitor reconcile error: %s", exc)
            self._emit_state_if_changed()
            self._wake_monitor.wait(timeout=self._poll_interval_s)
            self._wake_monitor.clear()

    def reconcile_once(self) -> Dict[str, Any]:
        """Single non-blocking-to-captures reconciliation step for USB/SDK/device."""
        if self._capture_in_progress:
            return self.status()
        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            return self.status()
        try:
            usb = self._usb_monitor.inspect()
            prev_usb_present = self._last_usb.usb_present
            self._last_usb = usb

            # USB was unplugged -> release stale handle immediately so replug
            # never hits a dead device handle.
            if not usb.usb_present and self._device_open and usb.detection_method in ("windows_setupapi", "injected"):
                LOGGER.info("DigitalPersona USB reader disconnected; closing handle cleanly.")
                self._close_device_unlocked()
                self._device_count = 0
                self._reconnecting = False
                self._initializing = False
                self._reconnect_attempts = 0
                return self.status()

            # USB arrived or device not yet open -> try initialization when
            # backoff allows and no missing-driver problem code is flagged.
            if usb.usb_present and usb.driver_problem_code not in (28,):
                if not prev_usb_present and not self._device_open:
                    LOGGER.info("DigitalPersona USB reader detected (%s).", usb.devices)
                if not self._device_open:
                    now = time.monotonic()
                    if now >= self._next_retry_monotonic:
                        self._reconnecting = self._reconnect_attempts > 0
                        self._initializing = not self._reconnecting
                        try:
                            self._open_first_device_unlocked()
                            self._reconnect_attempts = 0
                            self._next_retry_monotonic = 0.0
                            self._reconnecting = False
                            self._initializing = False
                        except ReaderError as exc:
                            self._reconnect_attempts += 1
                            backoff = min(5.0, 0.5 * (2 ** min(self._reconnect_attempts - 1, 3)))
                            self._next_retry_monotonic = time.monotonic() + backoff
                            self._last_error = exc.message
                            self._initializing = False
                            if exc.status in (STATUS_DRIVER_MISSING, STATUS_SDK_UNAVAILABLE, STATUS_SERVICE_UNAVAILABLE):
                                self._reconnecting = False
                            else:
                                self._reconnecting = True
                        return self.status()

            # Already open -> verify device health with dpfpdd_get_device_status
            if self._device_open:
                ok, reason = self._probe_open_device_unlocked()
                if not ok:
                    LOGGER.warning("Reader health probe failed (%s); scheduling reconnect.", reason)
                    self._close_device_unlocked()
                    self._reconnecting = True
                    self._reconnect_attempts += 1
                    self._next_retry_monotonic = time.monotonic() + 0.5
                    self._last_error = reason
            return self.status()
        finally:
            self._lock.release()

    # ---- DLL loading & C ABI setup ----------------------------------------

    def _load_dd(self) -> bool:
        if self._dd is not None and self._init_succeeded:
            return True
        if platform.system() != "Windows":
            return False

        if self._dd is None:
            path = _find_dll("dpfpdd.dll")
            if path is None:
                self._dd_path = None
                self._init_failed = False
                self._last_error = "dpfpdd.dll was not found in the DigitalPersona installation folders."
                return False
            self._dd_path = path
            try:
                # Add DLL folder to loader search path on Windows Python 3.8+
                if hasattr(os, "add_dll_directory"):
                    try:
                        os.add_dll_directory(str(path.parent))
                    except OSError:
                        pass
                dd = ctypes.WinDLL(str(path)) if hasattr(ctypes, "WinDLL") else ctypes.CDLL(str(path))
            except OSError as exc:
                self._last_error = f"Failed to load {path}: {exc}"
                return False

            # Configure exact DigitalPersona dpfpdd.h C ABI signatures (when loaded as ctypes DLL)
            if isinstance(dd, ctypes.CDLL):
                dd.dpfpdd_init.argtypes = []
                dd.dpfpdd_init.restype = ctypes.c_uint

                dd.dpfpdd_exit.argtypes = []
                dd.dpfpdd_exit.restype = ctypes.c_uint

                dd.dpfpdd_query_devices.argtypes = [
                    ctypes.POINTER(ctypes.c_uint),
                    ctypes.POINTER(_DPFPDD_DEV_INFO),
                ]
                dd.dpfpdd_query_devices.restype = ctypes.c_uint

                dd.dpfpdd_open.argtypes = [
                    ctypes.c_char_p,
                    ctypes.POINTER(ctypes.c_void_p),
                ]
                dd.dpfpdd_open.restype = ctypes.c_uint

                dd.dpfpdd_close.argtypes = [ctypes.c_void_p]
                dd.dpfpdd_close.restype = ctypes.c_uint

                if hasattr(dd, "dpfpdd_get_device_status"):
                    dd.dpfpdd_get_device_status.argtypes = [
                        ctypes.c_void_p,
                        ctypes.POINTER(_DPFPDD_DEV_STATUS),
                    ]
                    dd.dpfpdd_get_device_status.restype = ctypes.c_uint

                if hasattr(dd, "dpfpdd_reset"):
                    dd.dpfpdd_reset.argtypes = [ctypes.c_void_p]
                    dd.dpfpdd_reset.restype = ctypes.c_uint

                if hasattr(dd, "dpfpdd_cancel"):
                    dd.dpfpdd_cancel.argtypes = [ctypes.c_void_p]
                    dd.dpfpdd_cancel.restype = ctypes.c_uint

                dd.dpfpdd_capture.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(_DPFPDD_CAPTURE_PARAM),
                    ctypes.c_uint,
                    ctypes.POINTER(_DPFPDD_CAPTURE_RESULT),
                    ctypes.POINTER(ctypes.c_uint),
                    ctypes.c_void_p,
                ]
                dd.dpfpdd_capture.restype = ctypes.c_uint

            self._dd = dd

        rc = int(self._dd.dpfpdd_init()) & 0xFFFFFFFF
        if rc != DPFPDD_OK:
            self._init_succeeded = False
            self._init_failed = True
            self._init_rc = rc
            self._last_error = (
                f"dpfpdd_init returned {_format_rc(rc)} (DigitalPersona biometric service may still be starting or stopped)."
            )
            try:
                self._dd.dpfpdd_exit()
            except Exception:
                pass
            return False

        self._init_succeeded = True
        self._init_failed = False
        self._init_rc = DPFPDD_OK
        return True

    def _load_fj(self) -> bool:
        if self._fj is not None:
            return True
        if platform.system() != "Windows":
            return False
        extra = [self._dd_path.parent] if self._dd_path else []
        path = _find_dll("dpfj.dll", extra_dirs=extra)
        if path is None:
            self._fj_path = None
            self._last_error = "dpfj.dll was not found in the DigitalPersona installation folders."
            return False
        self._fj_path = path
        try:
            fj = ctypes.WinDLL(str(path)) if hasattr(ctypes, "WinDLL") else ctypes.CDLL(str(path))
        except OSError as exc:
            self._last_error = f"Failed to load {path}: {exc}"
            return False

        if isinstance(fj, ctypes.CDLL):
            fj.dpfj_create_fmd_from_raw.argtypes = [
                ctypes.c_void_p,          # image_data
                ctypes.c_uint,            # image_size
                ctypes.c_uint,            # image_width
                ctypes.c_uint,            # image_height
                ctypes.c_uint,            # image_dpi
                ctypes.c_uint,            # finger_pos
                ctypes.c_uint,            # cbeff_id
                ctypes.c_uint,            # fmd_type
                ctypes.c_void_p,          # fmd
                ctypes.POINTER(ctypes.c_uint),  # fmd_size
            ]
            fj.dpfj_create_fmd_from_raw.restype = ctypes.c_uint

            if hasattr(fj, "dpfj_create_fmd_from_fid"):
                fj.dpfj_create_fmd_from_fid.argtypes = [
                    ctypes.c_uint,                  # fid_type
                    ctypes.c_void_p,                # fid
                    ctypes.c_uint,                  # fid_size
                    ctypes.c_uint,                  # fmd_type
                    ctypes.c_void_p,                # fmd
                    ctypes.POINTER(ctypes.c_uint),  # fmd_size
                ]
                fj.dpfj_create_fmd_from_fid.restype = ctypes.c_uint

        self._fj = fj
        return True

    # ---- Device enumeration / open / close / health probe -----------------

    def _enumerate_unlocked(self) -> List[_DPFPDD_DEV_INFO]:
        if not self._load_dd():
            return []
        count = ctypes.c_uint(0)
        rc = int(self._dd.dpfpdd_query_devices(ctypes.byref(count), None)) & 0xFFFFFFFF
        if rc == DPFPDD_OK and count.value == 0:
            return []
        if rc not in (DPFPDD_OK, DPFPDD_E_MORE_DATA):
            self._last_error = f"dpfpdd_query_devices sizing failed ({_format_rc(rc)})."
            return []
        if count.value == 0:
            return []

        # Allocate with headroom in case a device arrives concurrently
        allocated = int(count.value)
        buf = (_DPFPDD_DEV_INFO * allocated)()
        for idx in range(allocated):
            buf[idx].size = ctypes.sizeof(_DPFPDD_DEV_INFO)
        req_count = ctypes.c_uint(allocated)
        rc = int(self._dd.dpfpdd_query_devices(ctypes.byref(req_count), buf)) & 0xFFFFFFFF
        if rc == DPFPDD_E_MORE_DATA and req_count.value > allocated:
            allocated = int(req_count.value)
            buf = (_DPFPDD_DEV_INFO * allocated)()
            for idx in range(allocated):
                buf[idx].size = ctypes.sizeof(_DPFPDD_DEV_INFO)
            req_count = ctypes.c_uint(allocated)
            rc = int(self._dd.dpfpdd_query_devices(ctypes.byref(req_count), buf)) & 0xFFFFFFFF
        if rc != DPFPDD_OK:
            self._last_error = f"dpfpdd_query_devices failed ({_format_rc(rc)})."
            return []
        actual = min(int(req_count.value), allocated)
        return [buf[i] for i in range(actual)]

    def _close_device_unlocked(self) -> None:
        handle = self._device
        self._device = None
        self._device_open = False
        if handle is not None and self._dd is not None:
            try:
                self._dd.dpfpdd_close(handle)
            except Exception:
                pass

    def _probe_open_device_unlocked(self) -> Tuple[bool, str]:
        if not self._device_open or self._device is None or self._dd is None:
            return False, "Device handle is closed."
        if not hasattr(self._dd, "dpfpdd_get_device_status"):
            return True, ""
        st = _DPFPDD_DEV_STATUS()
        st.size = ctypes.sizeof(_DPFPDD_DEV_STATUS)
        rc = int(self._dd.dpfpdd_get_device_status(self._device, ctypes.byref(st))) & 0xFFFFFFFF
        if rc in DPFPDD_DEVICE_GONE or rc != DPFPDD_OK:
            return False, f"dpfpdd_get_device_status returned {_format_rc(rc)}."
        if st.status == DPFPDD_STATUS_FAILURE:
            return False, "DigitalPersona device reported DPFPDD_STATUS_FAILURE."
        return True, ""

    def _open_first_device_unlocked(self) -> Dict[str, Any]:
        usb = self._usb_monitor.inspect()
        self._last_usb = usb
        if not self._load_dd():
            if self._dd is not None and self._init_failed:
                raise ReaderError(
                    "DRIVER_SERVICE_UNAVAILABLE",
                    self._last_error or STATUS_DETAILS[STATUS_SERVICE_UNAVAILABLE],
                    status=STATUS_SERVICE_UNAVAILABLE,
                    retry=True,
                )
            raise ReaderError(
                "DRIVER_MISSING",
                self._last_error or STATUS_DETAILS[STATUS_DRIVER_MISSING],
                status=STATUS_DRIVER_MISSING,
                retry=False,
            )
        if not self._load_fj():
            raise ReaderError(
                "SDK_UNAVAILABLE",
                self._last_error or STATUS_DETAILS[STATUS_SDK_UNAVAILABLE],
                status=STATUS_SDK_UNAVAILABLE,
                retry=False,
            )

        devices = self._enumerate_unlocked()
        self._device_count = len(devices)
        if not devices:
            self._close_device_unlocked()
            if usb.usb_present:
                if usb.driver_problem_code == 28:
                    raise ReaderError(
                        "DRIVER_MISSING",
                        "DigitalPersona USB hardware was detected, but Windows reports no driver is installed (Code 28).",
                        status=STATUS_DRIVER_MISSING,
                        retry=False,
                    )
                raise ReaderError(
                    "READER_INITIALIZING",
                    STATUS_DETAILS[STATUS_USB_DETECTED],
                    status=STATUS_USB_DETECTED,
                    retry=True,
                )
            raise ReaderError(
                "READER_NOT_CONNECTED",
                STATUS_DETAILS[STATUS_NOT_CONNECTED],
                status=STATUS_NOT_CONNECTED,
                retry=True,
            )

        # Prefer DigitalPersona U.are.U 4500 / VID 0x05BA if multiple readers exist
        chosen = devices[0]
        for info in devices:
            if info.id.vendor_id == DIGITALPERSONA_VID:
                chosen = info
                break

        dev_name = bytes(chosen.name).split(b"\x00", 1)[0]
        handle = ctypes.c_void_p()
        rc = int(self._dd.dpfpdd_open(dev_name, ctypes.byref(handle))) & 0xFFFFFFFF
        if rc != DPFPDD_OK or not handle:
            self._close_device_unlocked()
            if rc == DPFPDD_E_DEVICE_BUSY:
                raise ReaderError(
                    "READER_BUSY",
                    "The DigitalPersona reader is currently held by another process.",
                    status=STATUS_BUSY,
                    reader_status=rc,
                    retry=True,
                )
            raise ReaderError(
                "READER_OPEN_FAILED",
                f"dpfpdd_open failed ({_format_rc(rc)}).",
                status=STATUS_COMMUNICATION_FAILURE,
                reader_status=rc,
                retry=True,
            )

        self._device = handle
        self._device_open = True
        self._device_name = dev_name
        self._device_vendor = _decode_cstr(bytes(chosen.descr.vendor_name))
        self._device_product = _decode_cstr(bytes(chosen.descr.product_name)) or "U.are.U 4500"
        self._device_serial = _decode_cstr(bytes(chosen.descr.serial_num)) or _decode_cstr(dev_name)[:32]
        self._last_error = ""
        return self._build_status_unlocked()

    # ---- Status construction (non-blocking during active capture) ---------

    def _build_status_unlocked(self) -> Dict[str, Any]:
        usb = self._last_usb
        if usb.detection_method == "none":
            usb = self._usb_monitor.inspect()
            self._last_usb = usb

        driver_installed = self._dd is not None or _find_dll("dpfpdd.dll") is not None
        sdk_loaded = self._fj is not None or _find_dll("dpfj.dll") is not None

        if self._capture_in_progress:
            status_code = STATUS_BUSY
        elif self._dd is not None and self._init_failed:
            status_code = STATUS_SERVICE_UNAVAILABLE
        elif not driver_installed:
            status_code = STATUS_DRIVER_MISSING
        elif not sdk_loaded:
            status_code = STATUS_SDK_UNAVAILABLE
        elif self._device_open:
            status_code = STATUS_CONNECTED
        elif usb.usb_present and usb.driver_problem_code == 28:
            status_code = STATUS_DRIVER_MISSING
        elif usb.service_running is False:
            status_code = STATUS_SERVICE_UNAVAILABLE
        elif self._reconnecting:
            status_code = STATUS_RECONNECTING
        elif self._initializing:
            status_code = STATUS_INITIALIZING
        elif usb.usb_present:
            status_code = STATUS_USB_DETECTED
        else:
            status_code = STATUS_NOT_CONNECTED

        connected = status_code in (STATUS_CONNECTED, STATUS_BUSY)
        ready = status_code == STATUS_CONNECTED
        detail = STATUS_DETAILS.get(status_code, "")
        if status_code == STATUS_SERVICE_UNAVAILABLE and self._last_error:
            detail = f"{STATUS_DETAILS[STATUS_SERVICE_UNAVAILABLE]} ({self._last_error})"

        payload: Dict[str, Any] = {
            "status": status_code,
            "ui_state": status_code,
            "ui_label": UI_LABELS.get(status_code, "Fingerprint reader"),
            "connected": connected,
            "ready": ready,
            "busy": status_code == STATUS_BUSY,
            "detail": detail,
            "device_serial": self._device_serial if connected else "",
            "device_product": self._device_product if connected else (
                usb.devices[0]["product"] if usb.devices else ""
            ),
            "device_vendor": self._device_vendor if connected else "",
            "capture_in_progress": self._capture_in_progress,
            "driver_installed": bool(driver_installed),
            "driver_ready": bool(driver_installed and not self._init_failed and usb.driver_problem_code is None),
            "sdk_loaded": bool(sdk_loaded),
            "usb_detected": bool(usb.usb_present or self._device_open),
            "usb_devices": usb.devices,
            "service_running": usb.service_running,
            "initializing": status_code in (STATUS_INITIALIZING, STATUS_USB_DETECTED),
            "reconnecting": status_code == STATUS_RECONNECTING,
            "device_count": self._device_count if connected else (1 if usb.usb_present else 0),
            "last_error": self._last_error,
            "bridge_pid": os.getpid(),
        }
        if self._init_rc is not None and self._init_failed:
            payload["init_status"] = _format_rc(self._init_rc)
        return payload

    def status(self) -> Dict[str, Any]:
        # Never block GET /status behind a live capture wait!
        if self._capture_in_progress:
            return {
                "status": STATUS_BUSY,
                "ui_state": STATUS_BUSY,
                "ui_label": UI_LABELS[STATUS_BUSY],
                "connected": True,
                "ready": False,
                "busy": True,
                "detail": STATUS_DETAILS[STATUS_BUSY],
                "device_serial": self._device_serial,
                "device_product": self._device_product,
                "capture_in_progress": True,
                "driver_installed": True,
                "driver_ready": True,
                "sdk_loaded": self._fj is not None,
                "usb_detected": True,
                "usb_devices": self._last_usb.devices,
                "service_running": self._last_usb.service_running,
                "initializing": False,
                "reconnecting": False,
                "device_count": max(1, self._device_count),
                "last_error": self._last_error,
                "bridge_pid": os.getpid(),
            }

        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            return {
                "status": STATUS_BUSY,
                "ui_state": STATUS_BUSY,
                "ui_label": UI_LABELS[STATUS_BUSY],
                "connected": self._device_open,
                "ready": False,
                "busy": True,
                "detail": STATUS_DETAILS[STATUS_BUSY],
                "device_serial": self._device_serial,
                "device_product": self._device_product,
                "capture_in_progress": self._capture_in_progress,
                "driver_installed": bool(self._dd is not None or _find_dll("dpfpdd.dll") is not None),
                "sdk_loaded": bool(self._fj is not None or _find_dll("dpfj.dll") is not None),
                "usb_detected": bool(self._last_usb.usb_present or self._device_open),
                "device_count": self._device_count,
                "last_error": self._last_error,
                "bridge_pid": os.getpid(),
            }
        try:
            usb = self._usb_monitor.inspect()
            self._last_usb = usb
            # Attempt a lightweight sync if DLLs haven't been checked yet
            if not self._device_open and not self._init_failed and platform.system() == "Windows":
                if self._load_dd() and self._load_fj():
                    devs = self._enumerate_unlocked()
                    self._device_count = len(devs)
                    if devs and not self._device_open:
                        try:
                            self._open_first_device_unlocked()
                        except ReaderError as exc:
                            self._last_error = exc.message
            return self._build_status_unlocked()
        finally:
            self._lock.release()

    def initialize(self, *, reopen: bool = True) -> Dict[str, Any]:
        # Must never hang behind an active capture or a held lock!
        if self._capture_in_progress:
            snap = self.status()
            snap["busy"] = True
            return snap
        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            return {
                "status": STATUS_BUSY,
                "ui_state": STATUS_BUSY,
                "ui_label": UI_LABELS[STATUS_BUSY],
                "connected": self._device_open,
                "ready": False,
                "busy": True,
                "detail": STATUS_DETAILS[STATUS_BUSY],
                "capture_in_progress": self._capture_in_progress,
                "driver_installed": bool(self._dd is not None or _find_dll("dpfpdd.dll") is not None),
                "sdk_loaded": bool(self._fj is not None or _find_dll("dpfj.dll") is not None),
                "device_count": self._device_count,
                "last_error": self._last_error,
            }
        try:
            self._initializing = True
            if reopen and self._device_open:
                self._close_device_unlocked()
            try:
                return self._open_first_device_unlocked()
            except ReaderError as exc:
                self._last_error = exc.message
                return self._build_status_unlocked()
            finally:
                self._initializing = False
                self._reconnecting = False
        finally:
            self._lock.release()

    def diagnose(self) -> Dict[str, Any]:
        return {
            "service": "jone-fingerprint-bridge",
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "dd_path": str(self._dd_path) if self._dd_path else "",
            "fj_path": str(self._fj_path) if self._fj_path else "",
            "usb": self._usb_monitor.inspect().to_dict(),
            "status": self.status(),
        }

    # ---- Capture & ISO/ANSI minutiae extraction ---------------------------

    def capture(self, *, timeout_ms: int = 30000, finger_position: str = "RIGHT_INDEX") -> Dict[str, Any]:
        try:
            timeout_int = int(timeout_ms)
        except (TypeError, ValueError):
            raise ReaderError(
                "BAD_REQUEST", "timeout_ms must be an integer.",
                status=STATUS_COMMUNICATION_FAILURE, http_status=400,
            )
        if timeout_int < 5000 or timeout_int > 120000:
            raise ReaderError(
                "BAD_REQUEST",
                "timeout_ms must be between 5000 and 120000 milliseconds.",
                status=STATUS_COMMUNICATION_FAILURE,
                http_status=400,
            )
        pos_key = str(finger_position or "RIGHT_INDEX").strip().upper()
        if pos_key not in FINGER_POSITION_CODES:
            raise ReaderError(
                "BAD_REQUEST",
                f"Unsupported finger_position {finger_position!r}.",
                status=STATUS_COMMUNICATION_FAILURE,
                http_status=400,
            )

        if not self._lock.acquire(blocking=False):
            raise ReaderError(
                "READER_BUSY", STATUS_DETAILS[STATUS_BUSY],
                status=STATUS_BUSY, retry=True, http_status=409,
            )
        try:
            self._capture_in_progress = True
            started = time.monotonic()
            # Re-open if not currently open; retry once if handle went stale.
            for attempt in range(2):
                if not self._device_open:
                    self._open_first_device_unlocked()
                try:
                    raw_bytes, img_info, quality = self._capture_raw_unlocked(timeout_int)
                    minutiae, fmd_format = self._extract_minutiae_unlocked(
                        raw_bytes,
                        width=img_info.width,
                        height=img_info.height,
                        res=img_info.res or DPFPDD_IMG_RES_500,
                        finger_pos=FINGER_POSITION_CODES[pos_key],
                    )
                    duration_ms = int((time.monotonic() - started) * 1000)
                    return {
                        "success": True,
                        "minutiae": minutiae,
                        "finger_position": pos_key,
                        "count": len(minutiae),
                        "device_serial": self._device_serial,
                        "device_product": self._device_product,
                        "duration_ms": duration_ms,
                        "format": fmd_format,
                        "quality": quality,
                    }
                except ReaderError as exc:
                    if exc.retry and attempt == 0:
                        LOGGER.info("Retrying capture after recoverable device error (%s).", exc.code)
                        self._close_device_unlocked()
                        continue
                    raise
        finally:
            self._capture_in_progress = False
            self._lock.release()

    def _capture_raw_unlocked(self, timeout_ms: int) -> Tuple[bytes, _DPFPDD_IMAGE_INFO, int]:
        if not self._device_open or self._device is None or self._dd is None:
            raise ReaderError(
                "READER_NOT_CONNECTED",
                STATUS_DETAILS[STATUS_NOT_CONNECTED],
                status=STATUS_NOT_CONNECTED,
                retry=True,
            )

        param = _DPFPDD_CAPTURE_PARAM()
        param.size = ctypes.sizeof(_DPFPDD_CAPTURE_PARAM)
        param.image_fmt = DPFPDD_IMG_FMT_PIXEL_BUFFER
        param.image_proc = DPFPDD_IMG_PROC_DEFAULT
        param.image_res = DPFPDD_IMG_RES_500

        result = _DPFPDD_CAPTURE_RESULT()
        result.size = ctypes.sizeof(_DPFPDD_CAPTURE_RESULT)
        result.info.size = ctypes.sizeof(_DPFPDD_IMAGE_INFO)

        buf_size = 262144  # 256 KiB comfortably fits 500 DPI 8-bit U.are.U 4500 frames
        image_size = ctypes.c_uint(buf_size)
        buf = (ctypes.c_ubyte * buf_size)()

        rc = int(self._dd.dpfpdd_capture(
            self._device,
            ctypes.byref(param),
            ctypes.c_uint(timeout_ms),
            ctypes.byref(result),
            ctypes.byref(image_size),
            buf,
        )) & 0xFFFFFFFF

        if rc == DPFPDD_E_MORE_DATA and image_size.value > buf_size:
            buf_size = int(image_size.value)
            buf = (ctypes.c_ubyte * buf_size)()
            result.size = ctypes.sizeof(_DPFPDD_CAPTURE_RESULT)
            result.info.size = ctypes.sizeof(_DPFPDD_IMAGE_INFO)
            rc = int(self._dd.dpfpdd_capture(
                self._device,
                ctypes.byref(param),
                ctypes.c_uint(timeout_ms),
                ctypes.byref(result),
                ctypes.byref(image_size),
                buf,
            )) & 0xFFFFFFFF

        if rc in DPFPDD_DEVICE_GONE:
            raise ReaderError(
                "READER_RECONNECT_NEEDED",
                "The fingerprint reader was disconnected or reset during capture.",
                status=STATUS_COMMUNICATION_FAILURE,
                reader_status=rc,
                retry=True,
            )
        if rc == DPFPDD_E_DEVICE_BUSY:
            raise ReaderError(
                "READER_BUSY",
                STATUS_DETAILS[STATUS_BUSY],
                status=STATUS_BUSY,
                reader_status=rc,
                retry=False,
                http_status=409,
            )
        if rc != DPFPDD_OK:
            raise ReaderError(
                "READER_CAPTURE_FAILED",
                f"dpfpdd_capture failed ({_format_rc(rc)}).",
                status=STATUS_COMMUNICATION_FAILURE,
                reader_status=rc,
                retry=False,
            )

        if result.success == 0:
            if result.quality & DPFPDD_QUALITY_NO_FINGER:
                raise ReaderError(
                    "READER_CAPTURE_TIMEOUT",
                    f"No fingerprint was captured within {timeout_ms // 1000} seconds. Place the finger flat on the sensor and try again.",
                    status=STATUS_COMMUNICATION_FAILURE,
                    reader_status=int(result.quality),
                    retry=False,
                )
            raise ReaderError(
                "READER_POOR_QUALITY",
                _quality_explanation(int(result.quality)),
                status=STATUS_COMMUNICATION_FAILURE,
                reader_status=int(result.quality),
                retry=False,
                http_status=422,
            )

        actual_len = int(image_size.value)
        if actual_len <= 0 or result.info.width == 0 or result.info.height == 0:
            raise ReaderError(
                "READER_EMPTY_IMAGE",
                "The reader returned an empty image frame. Clean the sensor and try again.",
                status=STATUS_COMMUNICATION_FAILURE,
                retry=False,
            )
        return bytes(buf[:actual_len]), result.info, int(result.quality)

    def _extract_minutiae_unlocked(
        self,
        raw_bytes: bytes,
        *,
        width: int,
        height: int,
        res: int,
        finger_pos: int,
    ) -> Tuple[List[Dict[str, Any]], str]:
        if not self._load_fj():
            raise ReaderError(
                "SDK_UNAVAILABLE",
                STATUS_DETAILS[STATUS_SDK_UNAVAILABLE],
                status=STATUS_SDK_UNAVAILABLE,
                retry=False,
            )
        raw_buf = (ctypes.c_ubyte * len(raw_bytes)).from_buffer_copy(raw_bytes)
        last_rc = 0
        for fmd_const, fmd_name, std_hint in (
            (DPFJ_FMD_ISO_19794_2_2005, "ISO_19794_2_2005", "iso"),
            (DPFJ_FMD_ANSI_378_2004, "ANSI_378_2004", "ansi"),
        ):
            fmd_cap = 4096
            fmd_size = ctypes.c_uint(fmd_cap)
            fmd_buf = (ctypes.c_ubyte * fmd_cap)()
            rc = int(self._fj.dpfj_create_fmd_from_raw(
                raw_buf,
                ctypes.c_uint(len(raw_bytes)),
                ctypes.c_uint(width),
                ctypes.c_uint(height),
                ctypes.c_uint(res),
                ctypes.c_uint(finger_pos),
                ctypes.c_uint(0),
                ctypes.c_uint(fmd_const),
                fmd_buf,
                ctypes.byref(fmd_size),
            )) & 0xFFFFFFFF
            if rc == DPFJ_E_MORE_DATA and fmd_size.value > fmd_cap:
                fmd_cap = int(fmd_size.value)
                fmd_buf = (ctypes.c_ubyte * fmd_cap)()
                rc = int(self._fj.dpfj_create_fmd_from_raw(
                    raw_buf,
                    ctypes.c_uint(len(raw_bytes)),
                    ctypes.c_uint(width),
                    ctypes.c_uint(height),
                    ctypes.c_uint(res),
                    ctypes.c_uint(finger_pos),
                    ctypes.c_uint(0),
                    ctypes.c_uint(fmd_const),
                    fmd_buf,
                    ctypes.byref(fmd_size),
                )) & 0xFFFFFFFF
            if rc != DPFJ_OK or fmd_size.value == 0:
                last_rc = rc
                continue
            blob = bytes(fmd_buf[: int(fmd_size.value)])
            try:
                points = iso19794_2.parse_template(blob, standard=std_hint)
            except iso19794_2.TemplateParseError as exc:
                self._last_error = str(exc)
                continue
            return points, fmd_name

        raise ReaderError(
            "TEMPLATE_EXTRACTION_FAILED",
            f"The reader captured a finger image, but minutiae extraction failed ({_format_rc(last_rc)}). Place the finger flat on the sensor and try again.",
            status=STATUS_COMMUNICATION_FAILURE,
            reader_status=last_rc,
            retry=False,
            http_status=422,
        )

    def shutdown(self) -> None:
        self.stop_monitor()
        with self._lock:
            self._close_device_unlocked()
            if self._dd is not None and self._init_succeeded:
                try:
                    self._dd.dpfpdd_exit()
                except Exception:
                    pass
                self._init_succeeded = False


# ---------------------------------------------------------------------------
# HTTP Loopback Server (CORS + Private Network Access + SSE)
# ---------------------------------------------------------------------------


def _normalize_origin(origin: str) -> str:
    parsed = urllib.parse.urlparse((origin or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _origin_is_allowed(origin: str, allowlist: Sequence[str]) -> bool:
    norm = _normalize_origin(origin)
    if not norm:
        return False
    allowed = {_normalize_origin(item) for item in allowlist if item}
    return norm in allowed


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows, SO_REUSEADDR allows multiple processes to bind the same port!
    # Keep SO_REUSEADDR disabled on Windows and enforce SO_EXCLUSIVEADDRUSE.
    allow_reuse_address = platform.system() != "Windows"

    def server_bind(self) -> None:
        if platform.system() == "Windows" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        super().server_bind()


def make_handler(reader: Any, allowed_origins: Sequence[str] = DEFAULT_ALLOWED_ORIGINS):
    origins = tuple(allowed_origins or DEFAULT_ALLOWED_ORIGINS)

    class BridgeHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            LOGGER.debug("HTTP %s - " + format, self.client_address[0], *args)

        def _origin(self) -> str:
            return str(self.headers.get("Origin") or "").strip()

        def _check_origin(self) -> Tuple[bool, str]:
            origin = self._origin()
            if not origin:
                # Non-browser loopback CLI diagnostics without Origin header
                return True, ""
            if _origin_is_allowed(origin, origins):
                return True, origin
            return False, origin

        def _send_cors_headers(self, origin: str) -> None:
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Vary", "Origin")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")

        def _send_json(self, status_code: int, payload: Dict[str, Any], origin: str = "") -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self._send_cors_headers(origin)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _read_json(self) -> Dict[str, Any]:
            raw_len = self.headers.get("Content-Length")
            if not raw_len:
                return {}
            try:
                length = int(raw_len)
            except ValueError:
                raise ValueError("Invalid Content-Length")
            if length <= 0:
                return {}
            if length > 65536:
                raise ValueError("Payload too large")
            body = self.rfile.read(length)
            parsed = json.loads(body.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("JSON body must be an object")
            return parsed

        def do_OPTIONS(self) -> None:  # noqa: N802
            ok, origin = self._check_origin()
            if not ok:
                self._send_json(403, {
                    "success": False,
                    "error": "ORIGIN_NOT_ALLOWED",
                    "message": f"Origin {origin!r} is not allowlisted on this workstation bridge.",
                })
                return
            self.send_response(204)
            self._send_cors_headers(origin)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            ok, origin = self._check_origin()
            if not ok:
                self._send_json(403, {
                    "success": False,
                    "error": "ORIGIN_NOT_ALLOWED",
                    "message": f"Origin {origin!r} is not allowlisted on this workstation bridge.",
                })
                return
            path = urllib.parse.urlparse(self.path).path
            if path == "/health":
                self._send_json(200, {
                    "success": True,
                    "service": "jone-fingerprint-bridge",
                    "version": SERVICE_VERSION,
                    "loopback_only": True,
                    "pid": os.getpid(),
                }, origin=origin)
                return
            if path == "/status":
                self._send_json(200, reader.status(), origin=origin)
                return
            if path == "/diagnose":
                self._send_json(200, reader.diagnose(), origin=origin)
                return
            if path == "/events":
                self._handle_sse(origin)
                return
            if path in ("/initialize", "/capture"):
                self._send_json(405, {
                    "success": False,
                    "error": "METHOD_NOT_ALLOWED",
                    "message": f"Use POST for {path}.",
                }, origin=origin)
                return
            self._send_json(404, {
                "success": False,
                "error": "NOT_FOUND",
                "message": f"Unknown bridge route {path}.",
            }, origin=origin)

        def _handle_sse(self, origin: str) -> None:
            if not hasattr(reader, "subscribe_events"):
                self._send_json(404, {"success": False, "error": "SSE_UNSUPPORTED"}, origin=origin)
                return
            self.send_response(200)
            self._send_cors_headers(origin)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = reader.subscribe_events()
            try:
                initial = json.dumps(reader.status(), ensure_ascii=False)
                self.wfile.write(f"event: status\ndata: {initial}\n\n".encode("utf-8"))
                self.wfile.flush()
                while True:
                    try:
                        snap = q.get(timeout=15.0)
                        line = json.dumps(snap, ensure_ascii=False)
                        self.wfile.write(f"event: status\ndata: {line}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                reader.unsubscribe_events(q)

        def do_POST(self) -> None:  # noqa: N802
            ok, origin = self._check_origin()
            if not ok:
                self._send_json(403, {
                    "success": False,
                    "error": "ORIGIN_NOT_ALLOWED",
                    "message": f"Origin {origin!r} is not allowlisted on this workstation bridge.",
                })
                return
            path = urllib.parse.urlparse(self.path).path
            try:
                body = self._read_json()
            except (ValueError, UnicodeDecodeError) as exc:
                self._send_json(400, {
                    "success": False,
                    "error": "BAD_REQUEST",
                    "message": f"Invalid JSON request body: {exc}",
                }, origin=origin)
                return

            if path == "/initialize":
                try:
                    if "reopen" in body and hasattr(reader, "initialize"):
                        try:
                            state = reader.initialize(reopen=bool(body.get("reopen", True)))
                        except TypeError:
                            state = reader.initialize()
                    else:
                        state = reader.initialize()
                except ReaderError as exc:
                    self._send_json(exc.http_status, {
                        "success": False,
                        "error": exc.code,
                        "message": exc.message,
                        "status": exc.status,
                    }, origin=origin)
                    return
                self._send_json(200, state, origin=origin)
                return

            if path == "/capture":
                if "timeout_ms" in body and body["timeout_ms"] is not None:
                    raw_timeout = body["timeout_ms"]
                    if isinstance(raw_timeout, bool):
                        timeout_int = -1
                    else:
                        try:
                            timeout_int = int(raw_timeout)
                        except (TypeError, ValueError):
                            timeout_int = -1
                    if timeout_int < MIN_CAPTURE_TIMEOUT_MS or timeout_int > MAX_CAPTURE_TIMEOUT_MS:
                        self._send_json(400, {
                            "success": False,
                            "error": "INVALID_CAPTURE_TIMEOUT",
                            "message": (
                                f"timeout_ms must be between {MIN_CAPTURE_TIMEOUT_MS} and "
                                f"{MAX_CAPTURE_TIMEOUT_MS} milliseconds."
                            ),
                            "min_timeout_ms": MIN_CAPTURE_TIMEOUT_MS,
                            "max_timeout_ms": MAX_CAPTURE_TIMEOUT_MS,
                        }, origin=origin)
                        return
                    timeout_ms = timeout_int
                else:
                    timeout_ms = 30000
                finger_position = body.get("finger_position", "RIGHT_INDEX")
                try:
                    result = reader.capture(timeout_ms=timeout_ms, finger_position=finger_position)
                except ReaderError as exc:
                    self._send_json(exc.http_status, {
                        "success": False,
                        "error": exc.code,
                        "message": exc.message,
                        "status": exc.status,
                        "retry": exc.retry,
                    }, origin=origin)
                    return
                self._send_json(200, result, origin=origin)
                return

            self._send_json(404, {
                "success": False,
                "error": "NOT_FOUND",
                "message": f"Unknown bridge route {path}.",
            }, origin=origin)

    return BridgeHandler


def serve(
    reader: Any,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    allowed_origins: Sequence[str] = DEFAULT_ALLOWED_ORIGINS,
) -> BridgeHTTPServer:
    if host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("JONE_BRIDGE_ALLOW_NON_LOOPBACK"):
        raise ValueError(
            f"Refusing non-loopback bind host {host!r}; loopback 127.0.0.1 is required."
        )
    handler = make_handler(reader, allowed_origins=allowed_origins)
    return BridgeHTTPServer((host, int(port)), handler)


def _acquire_single_instance_mutex(port: int) -> Optional[Any]:
    """Acquire a Windows kernel mutex so only one bridge runs per workstation."""
    if platform.system() != "Windows":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        name = f"Local\\JONE_FingerprintBridge_{port}"
        handle = kernel32.CreateMutexW(None, False, name)
        err = ctypes.get_last_error()
        ERROR_ALREADY_EXISTS = 183
        if err == ERROR_ALREADY_EXISTS:
            if handle:
                kernel32.CloseHandle(handle)
            return False
        return handle
    except OSError:
        return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="J-ONE Workstation Fingerprint Bridge")
    parser.add_argument("--host", default=os.environ.get("JONE_BRIDGE_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("JONE_BRIDGE_PORT", DEFAULT_PORT)))
    parser.add_argument(
        "--allowed-origins",
        default=os.environ.get("JONE_BRIDGE_ALLOWED_ORIGINS", ",".join(DEFAULT_ALLOWED_ORIGINS)),
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    configure_logging(verbose=args.verbose)

    mutex = _acquire_single_instance_mutex(args.port)
    if mutex is False:
        LOGGER.info("J-ONE Fingerprint Bridge is already running on port %d; exiting duplicate instance.", args.port)
        return 0

    origins = tuple(o.strip() for o in str(args.allowed_origins).split(",") if o.strip())
    reader = DigitalPersonaReader(auto_monitor=True)
    try:
        server = serve(reader, host=args.host, port=args.port, allowed_origins=origins)
    except OSError as exc:
        LOGGER.error("Cannot bind %s:%d (%s). Another service may already own this port.", args.host, args.port, exc)
        reader.shutdown()
        return 1

    LOGGER.info(
        "J-ONE Fingerprint Bridge listening on http://%s:%d (PID %d, origins=%s)",
        args.host, args.port, os.getpid(), ",".join(origins),
    )

    def _shutdown(*_args: Any) -> None:
        LOGGER.info("Shutting down J-ONE Fingerprint Bridge...")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    try:
        server.serve_forever()
    finally:
        reader.shutdown()
        server.server_close()
        if mutex and platform.system() == "Windows":
            try:
                ctypes.WinDLL("kernel32").CloseHandle(mutex)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
