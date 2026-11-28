"""DigitalPersona U.are.U SDK binding for the J-ONE Reader Agent.

WHAT THIS IS
============
A thin ctypes layer over the two vendor libraries HID ships with the U.are.U
SDK for Windows:

``dpfpdd.dll``  DigitalPersona Device (capture) API  -- ``dpfpdd.h``
``dpfj.dll``    FingerJet feature-extraction / MATCHING engine -- ``dpfj.h``

Every prototype below is transcribed from those two headers (the same ABI the
official C/C++ samples and the .NET wrapper use). No method is invented, and
there is no fallback "pretend it matched" path: if the DLLs are missing, the
wrong driver mode is installed, or the sensor does not answer, the caller gets a
typed ``SdkError`` and the UI says so.

WHY MATCHING HAPPENS HERE AND NOT ON THE SERVER
===============================================
``dpfj_identify`` is the vendor's own 1:N matcher, and HID's Developer Guide is
explicit that it is the function to use for identification and verification
(``dpfj_compare`` is documented as a diagnostics tool). The previous J-ONE
implementation extracted minutiae points and re-implemented matching in Python
on the Django server; that scorer cannot tolerate the rotation every optical
sensor produces (measured: a genuine finger re-presented with 8 degrees of
rotation scored 57.5 against a 70 pass mark), so genuine staff were refused.
Matching here, with the vendor engine, is both supported and correct.

DATA HANDLING
=============
Only FMDs (ISO/IEC 19794-2:2005 Fingerprint Minutiae Data, ~1 KB) leave this
module. Raw images are held in memory just long enough to extract features and
are never written to disk, logged, or sent anywhere.
"""
from __future__ import annotations

import ctypes
import os
import platform
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

IS_WINDOWS = platform.system() == "Windows"
DIGITALPERSONA_VID = 0x05BA
_VID_PID_RE = re.compile(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})", re.IGNORECASE)

FINGER_POSITIONS: dict[str, int] = {
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

# --------------------------------------------------------------------------
# Return codes -- dpfpdd.h / dpfj.h (DPERROR(err) == err | (0x05BA << 16))
# --------------------------------------------------------------------------
DPFPDD_OK = 0x00000000
DPFPDD_E_FAILURE = 0x05BA000B
DPFPDD_E_MORE_DATA = 0x05BA000D
DPFPDD_E_INVALID_PARAMETER = 0x05BA0014
DPFPDD_E_INVALID_DEVICE = 0x05BA0015
DPFPDD_E_DEVICE_BUSY = 0x05BA001E
DPFPDD_E_DEVICE_FAILURE = 0x05BA001F
DPFPDD_E_NOT_IMPLEMENTED = 0x05BA000A

DPFJ_SUCCESS = 0x00000000
DPFJ_E_FAILURE = 0x05BA000B
DPFJ_E_NO_DATA = 0x05BA000C
DPFJ_E_MORE_DATA = 0x05BA000D
DPFJ_E_INVALID_PARAMETER = 0x05BA0014
DPFJ_E_INVALID_FID = 0x05BA0065
DPFJ_E_TOO_SMALL_AREA = 0x05BA0066
DPFJ_E_INVALID_FMD = 0x05BA00C9
DPFJ_E_ENROLLMENT_IN_PROGRESS = 0x05BA012D
DPFJ_E_ENROLLMENT_NOT_STARTED = 0x05BA012E
DPFJ_E_ENROLLMENT_NOT_READY = 0x05BA012F

# dpfpdd status values
DPFPDD_STATUS_READY = 0
DPFPDD_STATUS_BUSY = 1
DPFPDD_STATUS_NEED_CALIBRATION = 2
DPFPDD_STATUS_FAILURE = 3

# dpfpdd capture constants
DPFPDD_IMG_FMT_PIXEL_BUFFER = 0
DPFPDD_IMG_PROC_DEFAULT = 0
DPFPDD_IMG_RES_500 = 500
DPFPDD_QUALITY_GOOD = 0

# dpfj constants
DPFJ_FMD_ISO_19794_2_2005 = 0x01010001
DPFJ_PROBABILITY_ONE = 0x7FFFFFFF
DPFJ_POSITION_UNKNOWN = 0
DPFJ_POSITION_RINDEX = 2
MAX_FMD_SIZE = 26 + 4 + 255 * 6 + 2  # MAX_FMD_SIZE from dpfj.h

# Error codes that mean "the reader is gone" rather than "the read failed".
DEVICE_GONE_CODES = {
    DPFPDD_E_INVALID_DEVICE,
    DPFPDD_E_DEVICE_FAILURE,
    DPFPDD_E_NOT_IMPLEMENTED,
}

QUALITY_FLAGS = {
    1 << 1: "no finger on the sensor",
    1 << 2: "a fake/latent print was detected",
    1 << 3: "the print is too small",
    1 << 4: "the print is too light",
    1 << 5: "the print is too dark",
    1 << 6: "the image is too noisy",
    1 << 7: "the print has too little contrast",
    1 << 8: "not enough features in the print",
    1 << 9: "the finger is not centred",
    1 << 10: "this does not look like a finger",
    1 << 11: "the print is too high on the sensor",
    1 << 12: "the print is too low on the sensor",
    1 << 13: "the print is too far left",
    1 << 14: "the print is too far right",
    1 << 15: "the print is too distorted",
    1 << 16: "the finger moved too fast",
    1 << 17: "the finger is skewed",
    1 << 18: "the finger was lifted too soon",
    1 << 19: "the finger moved too slowly",
    1 << 31: "the scan failed",
}


class SdkError(Exception):
    """A capture/feature/match failure with a code the UI can explain."""

    def __init__(self, code: str, message: str, *, status: str = "error", retry: bool = True, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry = retry
        self.detail = detail


# --------------------------------------------------------------------------
# ctypes structures (dpfpdd.h)
# --------------------------------------------------------------------------
class _DPFPDD_VER_INFO(ctypes.Structure):
    _fields_ = [("major", ctypes.c_int), ("minor", ctypes.c_int), ("maintenance", ctypes.c_int)]


class _DPFPDD_VERSION(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint), ("lib_ver", _DPFPDD_VER_INFO), ("api_ver", _DPFPDD_VER_INFO)]


class _DPFPDD_HW_VERSION(ctypes.Structure):
    _fields_ = [("hw_ver", _DPFPDD_VER_INFO), ("fw_ver", _DPFPDD_VER_INFO), ("bcd_rev", ctypes.c_uint16)]


class _DPFPDD_DEV_DESCR(ctypes.Structure):
    _fields_ = [
        ("vendor_name", ctypes.c_char * 128),
        ("product_name", ctypes.c_char * 128),
        ("serial_num", ctypes.c_char * 128),
    ]


class _DPFPDD_DEV_ID(ctypes.Structure):
    _fields_ = [("vendor_id", ctypes.c_uint16), ("product_id", ctypes.c_uint16)]


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


class _DPFJ_CANDIDATE(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint), ("fmd_idx", ctypes.c_uint), ("view_idx", ctypes.c_uint)]


@dataclass
class DeviceInfo:
    name: str
    vendor: str = ""
    product: str = ""
    serial: str = ""
    vendor_id: int = 0
    product_id: int = 0
    firmware: str = ""
    modality: int = 0
    technology: int = 0

    @property
    def is_4500(self) -> bool:
        haystack = f"{self.vendor} {self.product}".lower()
        return ("4500" in haystack) or (self.vendor_id == 0x05BA)


@dataclass
class Capture:
    image: bytes
    width: int
    height: int
    dpi: int
    quality: int
    finger_detected: bool = True


@dataclass
class SdkReport:
    """Everything the UI needs to describe the sensor honestly."""
    sdk_available: bool = False
    sdk_version: str = ""
    sdk_dir: str = ""
    missing: list[str] = field(default_factory=list)
    usb_present: bool = False
    usb_instances: list[str] = field(default_factory=list)
    driver_mode: str = "UNKNOWN"
    driver_service: str = ""
    service_running: bool | None = None
    service_name: str = ""
    problem_code: int | None = None
    devices: list[DeviceInfo] = field(default_factory=list)
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "sdk_available": self.sdk_available,
            "sdk_version": self.sdk_version,
            "sdk_dir": self.sdk_dir,
            "missing_libraries": list(self.missing),
            "usb_present": self.usb_present,
            "usb_instances": list(self.usb_instances),
            "driver_mode": self.driver_mode,
            "driver_service": self.driver_service,
            "service_running": self.service_running,
            "service_name": self.service_name,
            "problem_code": self.problem_code,
            "devices": [
                {"name": d.name, "vendor": d.vendor, "product": d.product, "serial": d.serial,
                 "vendor_id": d.vendor_id, "product_id": d.product_id, "firmware": d.firmware}
                for d in self.devices
            ],
            "detail": self.detail,
        }


def _decode(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()


def _candidate_dirs(explicit: str | None) -> list[Path]:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("JONE_DP_SDK_DIR")
    if env:
        candidates.append(Path(env))
    here = Path(__file__).resolve().parent
    is_64 = running_python_bits() == 64
    arch_dirs = (
        [
            Path(r"C:\Program Files\DigitalPersona\U.are.U SDK\Windows\Lib\x64"),
            Path(r"C:\Program Files\HID Global\DigitalPersona\U.are.U SDK\Windows\Lib\x64"),
            Path(r"C:\Program Files\DigitalPersona\Bin"),
            Path(r"C:\Program Files\HID Global\Authentication\Bin"),
            Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32",
            Path(r"C:\Program Files\DigitalPersona\U.are.U SDK\Windows\Lib\win32"),
            Path(r"C:\Program Files (x86)\DigitalPersona\U.are.U SDK\Windows\Lib\win32"),
            Path(r"C:\Program Files (x86)\DigitalPersona\Bin"),
        ]
        if is_64
        else [
            Path(r"C:\Program Files (x86)\DigitalPersona\U.are.U SDK\Windows\Lib\win32"),
            Path(r"C:\Program Files\DigitalPersona\U.are.U SDK\Windows\Lib\win32"),
            Path(r"C:\Program Files\HID Global\DigitalPersona\U.are.U SDK\Windows\Lib\win32"),
            Path(r"C:\Program Files (x86)\DigitalPersona\Bin"),
            Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32",
        ]
    )
    candidates.extend([here, here / "lib", *arch_dirs])
    seen, unique = set(), []
    for path in candidates:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


class DpSdk:
    """Loaded vendor SDK. One instance per process; the sensor is exclusive."""

    def __init__(self, dll_dir: str | None = None, *, log=None):
        self.dll_dir = dll_dir
        self._dd = None
        self._fj = None
        self._initialized = False
        self._dev = ctypes.c_void_p(0)
        self._dev_name = ""
        self._log = log or (lambda *_a, **_k: None)
        self.report = SdkReport()

    # -- loading -----------------------------------------------------------
    def load(self) -> SdkReport:
        report = SdkReport(sdk_dir=str(self.dll_dir or ""))
        if not IS_WINDOWS:
            report.detail = "The DigitalPersona SDK is a Windows component; this computer is not Windows."
            self.report = report
            raise SdkError("SDK_UNAVAILABLE", report.detail, status="sdk_unavailable", retry=False)

        dirs = _candidate_dirs(self.dll_dir)
        dd, fj, sdk_dir, missing, load_error = _load_sdk_libraries(dirs)
        report.missing = missing
        if dd is None or fj is None:
            if load_error:
                report.detail = (
                    f"The DigitalPersona SDK libraries could not be loaded ({load_error}). "
                    f"Python is running in {running_python_bits()}-bit mode — ensure matching "
                    "DigitalPersona U.are.U SDK runtime DLLs are installed."
                )
            else:
                report.detail = (
                    "The DigitalPersona U.are.U SDK runtime was not found on this computer ("
                    + ", ".join(missing)
                    + "). The device driver alone is not enough: the SDK's libraries must be installed "
                    "(see reports/04_DRIVER_SDK_REQUIREMENTS_AND_INSTALL.md)."
                )
            self.report = report
            raise SdkError("SDK_UNAVAILABLE", report.detail, status="sdk_unavailable", retry=False, detail=load_error)

        report.sdk_dir = sdk_dir
        self._dd = dd
        self._fj = fj
        self._declare_prototypes()
        report.sdk_version = self._version()
        report.sdk_available = True
        self.report = report
        return self.refresh_status()

    def refresh_status(self) -> SdkReport:
        """Re-probe live USB presence, Windows driver/service status, and SDK devices."""
        report = SdkReport(
            sdk_available=bool(self._dd is not None and self._fj is not None),
            sdk_version=self.report.sdk_version or (self._version() if self._dd is not None else ""),
            sdk_dir=self.report.sdk_dir,
            missing=list(self.report.missing),
        )
        usb_info = inspect_usb_and_services()
        report.usb_present = bool(usb_info.get("usb_present"))
        report.usb_instances = list(usb_info.get("instances") or [])
        report.driver_service = str(usb_info.get("driver_service") or "")
        report.service_running = usb_info.get("service_running")
        report.service_name = str(usb_info.get("service_name") or "")
        report.problem_code = usb_info.get("problem_code")

        if self._dd is not None:
            if not self._initialized:
                try:
                    rc = int(self._dd.dpfpdd_init()) & 0xFFFFFFFF
                    self._initialized = (rc == DPFPDD_OK)
                except Exception:
                    self._initialized = False
            report.devices = self.query_devices()
            # If USB is present and dpfpdd_init succeeded earlier or just now,
            # one more initialize() call covers a freshly-started DpHost service.
            if not report.devices and report.usb_present:
                try:
                    rc = int(self._dd.dpfpdd_init()) & 0xFFFFFFFF
                    if rc == DPFPDD_OK:
                        self._initialized = True
                        report.devices = self.query_devices()
                except Exception:
                    pass

        report.driver_mode = self._driver_mode(report)
        report.detail = self._report_detail(report)
        self.report = report
        return report

    def _version(self) -> str:
        try:
            ver = _DPFPDD_VERSION()
            ver.size = ctypes.sizeof(_DPFPDD_VERSION)
            if int(self._dd.dpfpdd_version(ctypes.byref(ver))) & 0xFFFFFFFF != DPFPDD_OK:
                return ""
            lib, api = ver.lib_ver, ver.api_ver
            return f"{lib.major}.{lib.minor}.{lib.maintenance} (api {api.major}.{api.minor}.{api.maintenance})"
        except Exception:  # pragma: no cover - version is informational
            return ""

    def _declare_prototypes(self) -> None:
        dd, fj = self._dd, self._fj
        void_p, uint, int_ = ctypes.c_void_p, ctypes.c_uint, ctypes.c_int

        dd.dpfpdd_version.argtypes = [ctypes.POINTER(_DPFPDD_VERSION)]
        dd.dpfpdd_version.restype = int_
        dd.dpfpdd_init.argtypes = []
        dd.dpfpdd_init.restype = int_
        dd.dpfpdd_exit.argtypes = []
        dd.dpfpdd_exit.restype = int_
        # dpfpdd_query_devices(unsigned int *dev_cnt, DPFPDD_DEV_INFO *dev_infos[])
        dd.dpfpdd_query_devices.argtypes = [ctypes.POINTER(uint), ctypes.POINTER(ctypes.POINTER(_DPFPDD_DEV_INFO))]
        dd.dpfpdd_query_devices.restype = int_
        dd.dpfpdd_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(void_p)]
        dd.dpfpdd_open.restype = int_
        dd.dpfpdd_close.argtypes = [void_p]
        dd.dpfpdd_close.restype = int_
        dd.dpfpdd_capture.argtypes = [
            void_p, ctypes.POINTER(_DPFPDD_CAPTURE_PARAM), uint,
            ctypes.POINTER(_DPFPDD_CAPTURE_RESULT), ctypes.POINTER(uint), void_p,
        ]
        dd.dpfpdd_capture.restype = int_
        dd.dpfpdd_get_device_status.argtypes = [void_p, ctypes.POINTER(_DPFPDD_DEV_STATUS)]
        dd.dpfpdd_get_device_status.restype = int_

        # int dpfj_create_fmd_from_raw(const unsigned char *image_data, unsigned int image_size,
        #     unsigned int image_width, unsigned int image_height, unsigned int image_dpi,
        #     DPFJ_FINGER_POSITION finger_pos, unsigned int cbeff_id, DPFJ_FMD_FORMAT fmd_type,
        #     unsigned char *fmd, unsigned int *fmd_size);
        fj.dpfj_create_fmd_from_raw.argtypes = [
            ctypes.c_char_p, uint, uint, uint, uint, uint, uint, uint, void_p, ctypes.POINTER(uint),
        ]
        fj.dpfj_create_fmd_from_raw.restype = int_
        # int dpfj_identify(DPFJ_FMD_FORMAT fmd1_type, unsigned char *fmd1, unsigned int fmd1_size,
        #     unsigned int fmd1_view_idx, DPFJ_FMD_FORMAT fmds_type, unsigned int fmds_cnt,
        #     unsigned char **fmds, unsigned int *fmds_size, unsigned int threshold_score,
        #     unsigned int *candidate_cnt, DPFJ_CANDIDATE *candidates);
        fj.dpfj_identify.argtypes = [
            uint, ctypes.c_char_p, uint, uint, uint, uint,
            ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(uint), uint,
            ctypes.POINTER(uint), ctypes.POINTER(_DPFJ_CANDIDATE),
        ]
        fj.dpfj_identify.restype = int_
        # int dpfj_compare(...) -- diagnostics only, wired because HID documents it.
        fj.dpfj_compare.argtypes = [
            uint, ctypes.c_char_p, uint, uint, uint, ctypes.c_char_p, uint, uint, ctypes.POINTER(uint),
        ]
        fj.dpfj_compare.restype = int_
        fj.dpfj_start_enrollment.argtypes = [uint]
        fj.dpfj_start_enrollment.restype = int_
        fj.dpfj_add_to_enrollment.argtypes = [uint, ctypes.c_char_p, uint, uint]
        fj.dpfj_add_to_enrollment.restype = int_
        fj.dpfj_create_enrollment_fmd.argtypes = [void_p, ctypes.POINTER(uint)]
        fj.dpfj_create_enrollment_fmd.restype = int_
        fj.dpfj_finish_enrollment.argtypes = []
        fj.dpfj_finish_enrollment.restype = int_

    def _driver_mode(self, report: SdkReport) -> str:
        if report.devices:
            return "LEGACY_SDK"
        if report.usb_present:
            svc = (report.driver_service or "").lower()
            if svc in {"wudfrd", "winusb", "wbiosrvc", "wudf"}:
                return "WBF"
            if report.service_running is False and report.service_name.lower().startswith("dphost"):
                return "LEGACY_SDK"
            # The reader is on the USB bus but dpfpdd cannot see it: on the 4500
            # that normally means the Windows Biometric Framework (Windows Hello)
            # driver is bound, which HID documents as incompatible with the SDK.
            return "WBF"
        return "UNKNOWN"

    def _report_detail(self, report: SdkReport) -> str:
        if report.devices:
            device = report.devices[0]
            return f"{device.product or 'DigitalPersona reader'} ready (serial {device.serial or 'unknown'})."
        if report.usb_present and report.problem_code:
            return (
                f"The reader is plugged in but Windows Device Manager reported problem code "
                f"{report.problem_code}. Reinstall the DigitalPersona 4500 legacy driver (4.1.1.221) "
                "and replug the USB cable."
            )
        if report.usb_present and report.service_running is False and report.service_name.lower().startswith("dphost"):
            return (
                f"The reader is plugged in and uses the legacy driver, but the Windows service "
                f"'{report.service_name}' is stopped. Start that service in Services (services.msc) or reboot."
            )
        if report.driver_mode == "WBF":
            return (
                "The reader is plugged in but Windows has bound the Windows Biometric Framework "
                "(Windows Hello) driver, which HID documents as incompatible with the DigitalPersona SDK. "
                "Install the legacy DigitalPersona 4500 driver (4.1.1.221) for this computer; Windows Hello "
                "fingerprint sign-in and SDK capture cannot both use the same reader at the same time."
            )
        if report.usb_present:
            return "The reader is on the USB bus but the DigitalPersona driver did not expose it."
        return "No DigitalPersona reader is connected to this computer."

    # -- devices -----------------------------------------------------------
    def query_devices(self) -> list[DeviceInfo]:
        if self._dd is None:
            return []
        count = ctypes.c_uint(0)
        rc = int(self._dd.dpfpdd_query_devices(ctypes.byref(count), None)) & 0xFFFFFFFF
        if rc not in (DPFPDD_OK, DPFPDD_E_MORE_DATA):
            return []
        if count.value == 0:
            return []
        array = (ctypes.POINTER(_DPFPDD_DEV_INFO) * count.value)()
        infos = [_DPFPDD_DEV_INFO() for _ in range(count.value)]
        for idx, info in enumerate(infos):
            info.size = ctypes.sizeof(_DPFPDD_DEV_INFO)
            array[idx] = ctypes.pointer(info)
        rc = int(self._dd.dpfpdd_query_devices(ctypes.byref(count), array)) & 0xFFFFFFFF
        if rc != DPFPDD_OK:
            return []
        devices = []
        for info in infos[: count.value]:
            fw = f"{info.ver.fw_ver.major}.{info.ver.fw_ver.minor}.{info.ver.fw_ver.maintenance}"
            devices.append(DeviceInfo(
                name=_decode(info.name),
                vendor=_decode(info.descr.vendor_name),
                product=_decode(info.descr.product_name),
                serial=_decode(info.descr.serial_num),
                vendor_id=int(info.id.vendor_id),
                product_id=int(info.id.product_id),
                firmware=fw,
                modality=int(info.modality),
                technology=int(info.technology),
            ))
        return devices

    def initialize(self) -> list[DeviceInfo]:
        """dpfpdd_init + enumerate. Idempotent; safe to call on reconnect."""
        rc = int(self._dd.dpfpdd_init()) & 0xFFFFFFFF
        if rc != DPFPDD_OK:
            self._initialized = False
            raise SdkError(
                "SERVICE_UNAVAILABLE",
                "The DigitalPersona device interface did not initialise (dpfpdd_init). "
                "The reader driver service is not answering; replug the reader, then retry.",
                status="service_unavailable",
            )
        self._initialized = True
        return self.query_devices()

    def open(self, device: DeviceInfo) -> None:
        if self._dev and self._dev.value:
            self.close()
        dev = ctypes.c_void_p(0)
        rc = int(self._dd.dpfpdd_open(device.name.encode(), ctypes.byref(dev))) & 0xFFFFFFFF
        if rc != DPFPDD_OK:
            raise SdkError(
                "DEVICE_OPEN_FAILED",
                f"The reader could not be opened (code 0x{rc:08X}). Another program may be holding it.",
                status="busy" if rc == DPFPDD_E_DEVICE_BUSY else "error",
            )
        self._dev = dev
        self._dev_name = device.name

    def close(self) -> None:
        try:
            if self._dev and self._dev.value:
                self._dd.dpfpdd_close(self._dev)
        except Exception:  # pragma: no cover - close must never raise
            pass
        self._dev = ctypes.c_void_p(0)
        self._dev_name = ""

    def status(self) -> dict:
        if not (self._dev and self._dev.value):
            return {"status": DPFPDD_STATUS_FAILURE, "finger_detected": False}
        info = _DPFPDD_DEV_STATUS()
        info.size = ctypes.sizeof(_DPFPDD_DEV_STATUS)
        rc = int(self._dd.dpfpdd_get_device_status(self._dev, ctypes.byref(info))) & 0xFFFFFFFF
        if rc != DPFPDD_OK:
            return {"status": DPFPDD_STATUS_FAILURE, "finger_detected": False, "rc": rc}
        return {"status": int(info.status), "finger_detected": bool(info.finger_detected)}

    # -- capture -----------------------------------------------------------
    def capture(self, timeout_ms: int = 15000) -> Capture:
        if not (self._dev and self._dev.value):
            raise SdkError("READER_NOT_OPEN", "The reader is not open.", status="not_connected")
        param = _DPFPDD_CAPTURE_PARAM()
        param.size = ctypes.sizeof(_DPFPDD_CAPTURE_PARAM)
        param.image_fmt = DPFPDD_IMG_FMT_PIXEL_BUFFER
        param.image_proc = DPFPDD_IMG_PROC_DEFAULT
        param.image_res = DPFPDD_IMG_RES_500
        result = _DPFPDD_CAPTURE_RESULT()
        result.size = ctypes.sizeof(_DPFPDD_CAPTURE_RESULT)
        capacity = ctypes.c_uint(0)

        rc = int(self._dd.dpfpdd_capture(
            self._dev, ctypes.byref(param), ctypes.c_uint(timeout_ms),
            ctypes.byref(result), ctypes.byref(capacity), None,
        )) & 0xFFFFFFFF
        if rc not in (DPFPDD_OK, DPFPDD_E_MORE_DATA):
            raise self._capture_error(rc)
        if capacity.value == 0:
            raise SdkError("CAPTURE_EMPTY", "The reader returned an empty image.", status="error")

        buffer = ctypes.create_string_buffer(capacity.value)
        rc = int(self._dd.dpfpdd_capture(
            self._dev, ctypes.byref(param), ctypes.c_uint(timeout_ms),
            ctypes.byref(result), ctypes.byref(capacity), buffer,
        )) & 0xFFFFFFFF
        if rc != DPFPDD_OK:
            raise self._capture_error(rc)

        quality = int(result.quality)
        if quality != DPFPDD_QUALITY_GOOD:
            raise SdkError(
                "CAPTURE_QUALITY",
                _quality_message(quality),
                status="capture_quality",
            )
        if not int(result.success):
            raise SdkError("CAPTURE_FAILED", "The reader did not report a successful capture.", status="error")

        return Capture(
            image=bytes(buffer.raw[: capacity.value]),
            width=int(result.info.width),
            height=int(result.info.height),
            dpi=int(result.info.res) or DPFPDD_IMG_RES_500,
            quality=quality,
        )

    def _capture_error(self, rc: int) -> SdkError:
        if rc == DPFPDD_E_DEVICE_BUSY:
            return SdkError("READER_BUSY", "The reader is busy with another capture.", status="busy")
        if rc in DEVICE_GONE_CODES:
            return SdkError("READER_GONE", "The reader stopped responding; it may have been unplugged.",
                            status="not_connected")
        return SdkError("CAPTURE_FAILED", f"The reader could not capture a fingerprint (code 0x{rc:08X}).")

    # -- feature extraction / matching -------------------------------------
    def fmd_from_capture(self, capture: Capture, finger_position: int = DPFJ_POSITION_RINDEX) -> bytes:
        size = ctypes.c_uint(MAX_FMD_SIZE)
        buffer = ctypes.create_string_buffer(MAX_FMD_SIZE)
        rc = int(self._fj.dpfj_create_fmd_from_raw(
            capture.image, ctypes.c_uint(len(capture.image)),
            ctypes.c_uint(capture.width), ctypes.c_uint(capture.height), ctypes.c_uint(capture.dpi),
            ctypes.c_uint(finger_position), ctypes.c_uint(0),
            ctypes.c_uint(DPFJ_FMD_ISO_19794_2_2005),
            buffer, ctypes.byref(size),
        )) & 0xFFFFFFFF
        if rc == DPFJ_E_MORE_DATA:
            needed = max(int(size.value), MAX_FMD_SIZE)
            buffer = ctypes.create_string_buffer(needed)
            size = ctypes.c_uint(needed)
            rc = int(self._fj.dpfj_create_fmd_from_raw(
                capture.image, ctypes.c_uint(len(capture.image)),
                ctypes.c_uint(capture.width), ctypes.c_uint(capture.height), ctypes.c_uint(capture.dpi),
                ctypes.c_uint(finger_position), ctypes.c_uint(0),
                ctypes.c_uint(DPFJ_FMD_ISO_19794_2_2005),
                buffer, ctypes.byref(size),
            )) & 0xFFFFFFFF
        if rc == DPFJ_E_TOO_SMALL_AREA:
            raise SdkError("CAPTURE_TOO_SMALL", "Place the whole fingertip flat on the sensor.", status="capture_quality")
        if rc != DPFJ_SUCCESS or int(size.value) == 0:
            raise SdkError("FMD_FAILED", f"Fingerprint features could not be extracted (code 0x{rc:08X}).")
        return bytes(buffer.raw[: int(size.value)])

    def identify(self, probe_fmd: bytes, gallery: Sequence[tuple[int, bytes]], *,
                 threshold: int, max_candidates: int = 5) -> list[tuple[int, int]]:
        """1:N match. Returns [(gallery_index, dissimilarity_score)] best first.

        ``gallery`` is a sequence of (index, fmd_bytes); the index is whatever
        the caller wants back (the agent passes the row number, then maps it to
        staff/template).
        """
        if not gallery:
            return []
        buffers = [ctypes.c_char_p(fmd) for _idx, fmd in gallery]
        sizes = (ctypes.c_uint * len(gallery))(*[ctypes.c_uint(len(fmd)) for _idx, fmd in gallery])
        array = (ctypes.c_char_p * len(gallery))(*buffers)
        candidates = (_DPFJ_CANDIDATE * max_candidates)()
        count = ctypes.c_uint(max_candidates)
        for candidate in candidates:
            candidate.size = ctypes.sizeof(_DPFJ_CANDIDATE)

        rc = int(self._fj.dpfj_identify(
            ctypes.c_uint(DPFJ_FMD_ISO_19794_2_2005), probe_fmd, ctypes.c_uint(len(probe_fmd)),
            ctypes.c_uint(0),
            ctypes.c_uint(DPFJ_FMD_ISO_19794_2_2005), ctypes.c_uint(len(gallery)),
            array, sizes,
            ctypes.c_uint(threshold), ctypes.byref(count), candidates,
        )) & 0xFFFFFFFF
        if rc != DPFJ_SUCCESS:
            raise SdkError("IDENTIFY_FAILED", f"The fingerprint could not be compared (code 0x{rc:08X}).")

        # dpfj_identify returns candidates ordered by dissimilarity already; the
        # score is not part of DPFJ_CANDIDATE, so ask dpfj_compare for the exact
        # value of each returned candidate (HID documents this combination).
        results: list[tuple[int, int]] = []
        for candidate in candidates[: count.value]:
            idx = int(candidate.fmd_idx)
            if idx >= len(gallery):
                continue
            score = self.compare(probe_fmd, gallery[idx][1], view_index=int(candidate.view_idx))
            results.append((gallery[idx][0], score))
        results.sort(key=lambda item: item[1])
        return results

    def compare(self, fmd1: bytes, fmd2: bytes, *, view_index: int = 0) -> int:
        score = ctypes.c_uint(0)
        rc = int(self._fj.dpfj_compare(
            ctypes.c_uint(DPFJ_FMD_ISO_19794_2_2005), fmd1, ctypes.c_uint(len(fmd1)), ctypes.c_uint(0),
            ctypes.c_uint(DPFJ_FMD_ISO_19794_2_2005), fmd2, ctypes.c_uint(len(fmd2)), ctypes.c_uint(view_index),
            ctypes.byref(score),
        )) & 0xFFFFFFFF
        if rc != DPFJ_SUCCESS:
            raise SdkError("COMPARE_FAILED", f"The fingerprints could not be compared (code 0x{rc:08X}).")
        return int(score.value)

    def start_enrollment(self) -> None:
        rc = int(self._fj.dpfj_start_enrollment(ctypes.c_uint(DPFJ_FMD_ISO_19794_2_2005))) & 0xFFFFFFFF
        if rc != DPFJ_SUCCESS:
            raise SdkError("ENROLL_FAILED", f"Enrolment could not start (code 0x{rc:08X}).")

    def add_to_enrollment(self, fmd: bytes) -> str:
        """Feed one scan FMD. Returns 'ready' when enough views exist, or 'more_data'."""
        rc = int(self._fj.dpfj_add_to_enrollment(
            ctypes.c_uint(DPFJ_FMD_ISO_19794_2_2005), fmd, ctypes.c_uint(len(fmd)), ctypes.c_uint(0),
        )) & 0xFFFFFFFF
        if rc == DPFJ_SUCCESS:
            return "ready"
        if rc == DPFJ_E_MORE_DATA:
            return "more_data"
        raise SdkError("ENROLL_FAILED", f"An enrolment impression was rejected (code 0x{rc:08X}).")

    def create_enrollment_fmd(self) -> bytes:
        size = ctypes.c_uint(MAX_FMD_SIZE)
        buffer = ctypes.create_string_buffer(MAX_FMD_SIZE)
        rc = int(self._fj.dpfj_create_enrollment_fmd(buffer, ctypes.byref(size))) & 0xFFFFFFFF
        if rc == DPFJ_E_ENROLLMENT_NOT_READY:
            raise SdkError("ENROLL_NOT_READY", "More fingerprint impressions are needed for enrolment.")
        if rc != DPFJ_SUCCESS or int(size.value) == 0:
            raise SdkError("ENROLL_FAILED", f"The enrolment template could not be created (code 0x{rc:08X}).")
        return bytes(buffer.raw[: int(size.value)])

    def finish_enrollment(self) -> None:
        try:
            if self._fj is not None:
                self._fj.dpfj_finish_enrollment()
        except Exception:  # pragma: no cover
            pass

    def enrollment_fmd(self, fmds: Iterable[bytes]) -> bytes:
        """Combine several impressions into one enrolment FMD."""
        self.start_enrollment()
        added = 0
        try:
            for fmd in fmds:
                state = self.add_to_enrollment(fmd)
                added += 1
                if state == "ready":
                    break
            if not added:
                raise SdkError("ENROLL_FAILED", "No enrolment impression was accepted.")
            return self.create_enrollment_fmd()
        finally:
            self.finish_enrollment()

    def shutdown(self) -> None:
        self.close()
        try:
            if self._dd is not None:
                self._dd.dpfpdd_exit()
            self._initialized = False
        except Exception:  # pragma: no cover
            pass


def _quality_message(quality: int) -> str:
    reasons = [text for flag, text in QUALITY_FLAGS.items() if quality & flag]
    if reasons:
        return "Fingerprint not accepted: " + "; ".join(reasons) + ". Place the fingertip flat and still, then try again."
    return f"Fingerprint capture quality was rejected (0x{quality:08X}). Place the finger flat and try again."


def _find_dll(name: str, dirs: Sequence[Path]) -> Path | None:
    for directory in dirs:
        candidate = Path(directory) / name
        try:
            if candidate.is_file():
                return candidate
        except OSError:  # pragma: no cover - unreachable path
            continue
    return None


def _find_all_dlls(name: str, dirs: Sequence[Path]) -> list[Path]:
    found: list[Path] = []
    for directory in dirs:
        candidate = Path(directory) / name
        try:
            if candidate.is_file():
                found.append(candidate)
        except OSError:
            continue
    return found


def _load_sdk_libraries(dirs: Sequence[Path]) -> tuple[object | None, object | None, str, list[str], str]:
    """Locate and load matching-bitness dpfpdd.dll + dpfj.dll across candidate folders."""
    dd_candidates = _find_all_dlls("dpfpdd.dll", dirs)
    fj_candidates = _find_all_dlls("dpfj.dll", dirs)
    missing = [
        name
        for name, items in (("dpfpdd.dll", dd_candidates), ("dpfj.dll", fj_candidates))
        if not items
    ]
    if missing:
        return None, None, "", missing, ""

    # Prefer a directory that provides both DLLs together, falling back to first working pair.
    fj_by_dir = {str(p.parent).lower(): p for p in fj_candidates}
    ordered_pairs: list[tuple[Path, Path]] = []
    for dd_path in dd_candidates:
        paired_fj = fj_by_dir.get(str(dd_path.parent).lower())
        if paired_fj is not None:
            ordered_pairs.append((dd_path, paired_fj))
    for dd_path in dd_candidates:
        for fj_path in fj_candidates:
            pair = (dd_path, fj_path)
            if pair not in ordered_pairs:
                ordered_pairs.append(pair)

    last_error = ""
    for dd_path, fj_path in ordered_pairs:
        try:
            if IS_WINDOWS and hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(str(dd_path.parent))
                    if fj_path.parent != dd_path.parent:
                        os.add_dll_directory(str(fj_path.parent))
                except OSError:
                    pass
            dd = _load_library(dd_path)
            fj = _load_library(fj_path)
            return dd, fj, str(dd_path.parent), [], ""
        except OSError as exc:
            last_error = str(exc)
            continue
    return None, None, "", [], last_error


def _load_library(path: Path):
    if IS_WINDOWS and hasattr(ctypes, "WinDLL"):
        return ctypes.WinDLL(str(path))
    return ctypes.CDLL(str(path))  # pragma: no cover - Linux dev machines


def _query_windows_dp_service() -> tuple[bool | None, str]:
    """Inspect Windows SCM for DigitalPersona / WBF host services."""
    if not IS_WINDOWS:  # pragma: no cover
        return None, ""
    try:
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    except (OSError, AttributeError):
        return None, ""

    class _SERVICE_STATUS(ctypes.Structure):
        _fields_ = [
            ("dwServiceType", wintypes.DWORD),
            ("dwCurrentState", wintypes.DWORD),
            ("dwControlsAccepted", wintypes.DWORD),
            ("dwWin32ExitCode", wintypes.DWORD),
            ("dwServiceSpecificExitCode", wintypes.DWORD),
            ("dwCheckPoint", wintypes.DWORD),
            ("dwWaitHint", wintypes.DWORD),
        ]

    SC_MANAGER_CONNECT = 0x0001
    SERVICE_QUERY_STATUS = 0x0004
    SERVICE_RUNNING = 0x00000004

    advapi32.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    advapi32.OpenSCManagerW.restype = ctypes.c_void_p
    advapi32.OpenServiceW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.DWORD]
    advapi32.OpenServiceW.restype = ctypes.c_void_p
    advapi32.QueryServiceStatus.argtypes = [ctypes.c_void_p, ctypes.POINTER(_SERVICE_STATUS)]
    advapi32.QueryServiceStatus.restype = wintypes.BOOL
    advapi32.CloseServiceHandle.argtypes = [ctypes.c_void_p]
    advapi32.CloseServiceHandle.restype = wintypes.BOOL

    scm = advapi32.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
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
        first_stopped = ""
        for name in candidates:
            svc = advapi32.OpenServiceW(scm, name, SERVICE_QUERY_STATUS)
            if not svc:
                continue
            found_any = True
            try:
                st = _SERVICE_STATUS()
                if advapi32.QueryServiceStatus(svc, ctypes.byref(st)):
                    if st.dwCurrentState == SERVICE_RUNNING:
                        return True, name
                    if not first_stopped:
                        first_stopped = name
            finally:
                advapi32.CloseServiceHandle(svc)
        if found_any:
            return False, first_stopped
        return None, ""
    finally:
        advapi32.CloseServiceHandle(scm)


def inspect_usb_and_services() -> dict:
    """Detect *currently connected* DigitalPersona USB hardware via SetupAPI + CfgMgr32.

    Unlike enumerating HKLM\\SYSTEM\\CurrentControlSet\\Enum\\USB (which retains
    every USB device ever plugged into Windows across unplugs), SetupDiGetClassDevsW
    with DIGCF_PRESENT reports only readers physically attached right now, along
    with the bound kernel service (SPDRP_SERVICE) and Device Manager problem code.
    """
    if not IS_WINDOWS:  # pragma: no cover - Linux dev machines
        return {
            "usb_present": False,
            "instances": [],
            "driver_service": "",
            "service_running": None,
            "service_name": "",
            "problem_code": None,
        }

    instances: list[str] = []
    driver_service = ""
    first_problem: int | None = None
    svc_running, svc_name = _query_windows_dp_service()

    try:
        from ctypes import wintypes

        setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
        cfgmgr32 = ctypes.WinDLL("cfgmgr32", use_last_error=True)
    except (OSError, AttributeError):  # pragma: no cover
        return {
            "usb_present": False,
            "instances": [],
            "driver_service": "",
            "service_running": svc_running,
            "service_name": svc_name,
            "problem_code": None,
        }

    DIGCF_PRESENT = 0x00000002
    DIGCF_ALLCLASSES = 0x00000004
    SPDRP_HARDWAREID = 0x00000001
    SPDRP_SERVICE = 0x00000004
    DN_HAS_PROBLEM = 0x00000400

    class _SP_DEVINFO_DATA(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("ClassGuid", ctypes.c_ubyte * 16),
            ("DevInst", wintypes.DWORD),
            ("Reserved", ctypes.c_size_t),
        ]

    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.c_void_p, wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD,
    ]
    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiEnumDeviceInfo.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_SP_DEVINFO_DATA),
    ]
    setupapi.SetupDiEnumDeviceInfo.restype = wintypes.BOOL
    setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_SP_DEVINFO_DATA),
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
        None, "USB", None, DIGCF_PRESENT | DIGCF_ALLCLASSES,
    )
    if not hdev or hdev == invalid_handle:
        return {
            "usb_present": False,
            "instances": [],
            "driver_service": "",
            "service_running": svc_running,
            "service_name": svc_name,
            "problem_code": None,
        }

    def _get_prop(dev_info, prop_id: int) -> str:
        buf = ctypes.create_unicode_buffer(1024)
        req = wintypes.DWORD(0)
        ok = setupapi.SetupDiGetDeviceRegistryPropertyW(
            hdev, ctypes.byref(dev_info), prop_id, None,
            buf, ctypes.sizeof(buf), ctypes.byref(req),
        )
        if not ok:
            return ""
        raw = ctypes.wstring_at(buf, max(0, (req.value // 2) - 1))
        return raw.replace("\x00", " ").strip()

    try:
        idx = 0
        dev_info = _SP_DEVINFO_DATA()
        dev_info.cbSize = ctypes.sizeof(_SP_DEVINFO_DATA)
        while setupapi.SetupDiEnumDeviceInfo(hdev, idx, ctypes.byref(dev_info)):
            idx += 1
            hw_id = _get_prop(dev_info, SPDRP_HARDWAREID)
            if not hw_id:
                continue
            match = _VID_PID_RE.search(hw_id)
            if not match or int(match.group(1), 16) != DIGITALPERSONA_VID:
                continue
            svc = _get_prop(dev_info, SPDRP_SERVICE)
            if svc and not driver_service:
                driver_service = svc
            status_flags = wintypes.ULONG(0)
            problem_num = wintypes.ULONG(0)
            cm_rc = cfgmgr32.CM_Get_DevNode_Status(
                ctypes.byref(status_flags),
                ctypes.byref(problem_num),
                dev_info.DevInst,
                0,
            )
            if cm_rc == 0 and bool(status_flags.value & DN_HAS_PROBLEM) and first_problem is None:
                first_problem = int(problem_num.value)
            first_hwid = hw_id.split(" ")[0]
            instances.append(f"{first_hwid} ({svc or 'no-driver'})")
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)

    return {
        "usb_present": bool(instances),
        "instances": instances,
        "driver_service": driver_service,
        "service_running": svc_running,
        "service_name": svc_name,
        "problem_code": first_problem,
    }


def usb_reader_present() -> tuple[bool, list[str]]:
    """Is a DigitalPersona reader (USB VID 05BA) currently plugged in?"""
    info = inspect_usb_and_services()
    return bool(info.get("usb_present")), list(info.get("instances") or [])


def running_python_bits() -> int:
    return 64 if sys.maxsize > 2**32 else 32
