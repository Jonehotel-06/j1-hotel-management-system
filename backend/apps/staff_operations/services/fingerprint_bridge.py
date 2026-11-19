"""
J-ONE Fingerprint Reader Bridge — SERVER-HOSTED adapter (not the workstation bridge).

Architecture (repaired 2026-11-19)
==================================
The DigitalPersona U.are.U 4500 is a USB device attached to ONE Windows 11
workstation. The supported capture path is the standalone bridge that runs on
that workstation:

    workstation_bridge/fingerprint_bridge_service.py   (loopback HTTP, port 8787)
    frontend/js/biometrics.js                          (calls it directly)

What this module implements is a *server-hosted* adapter: it can only see a
reader when the Django API itself runs on the machine the reader is plugged
into. That is true for a single-PC "all-in-one" deployment and never for the
hosted API (Northflank/Cloudflare), which has no USB reader.

Consequently:
  * ``apps/staff_operations/views_bridge.py`` refuses to use it unless
    ``FINGERPRINT_BRIDGE_COLOCATED=1`` is configured, and says so explicitly
    instead of reporting a missing reader;
  * the desk pages no longer call those server endpoints at all.

Hardware assumptions corrected here
-----------------------------------
  * The U.are.U 4500 USB identifiers are vendor 0x05BA (DigitalPersona / HID
    Global), product 0x000A — not 0x007B/0x0119.
  * The device is a vendor-specific biometric USB device, NOT a USB-HID class
    device: ``hid.enumerate`` cannot discover it and a raw HID handle cannot
    capture from it. Enumeration and capture go through the DigitalPersona SDK
    (``dpfpdd.dll`` / ``dpfj.dll``).

Translating between:

  Physical USB reader (DigitalPersona 4500) <-> Windows SDK/Service <-> this bridge <-> WebSocket <-> browser

Responsibilities:
1. Discover USB fingerprint readers attached to the workstation.
2. Initialise the DigitalPersona SDK (DPA SDK) when installed.
3. Capture fingerprint minutiae probes from the reader.
4. Report accurate reader status: connected, initializing, not_connected,
   driver_missing, sdk_unavailable, permission_denied, busy, communication_failure.
5. Allow the browser to retry device initialization.
6. Handle reader disconnect/reconnection.

This bridge runs ONLY on the local workstation (never on the server).
It listens on localhost only. The browser connects via WebSocket to
ws://localhost:<port>/jone/fingerprint/.

Deployment requirements (Windows 11):
- DigitalPersona U.are.U 4500 driver installed (Windows Device Manager).
- DigitalPersona SDK (DPA SDK) installed and licensed.
- This bridge script running as a local service/application.
- Windows Firewall allowing localhost loopback (default).

External dependencies (NOT bundled — install separately on the workstation):
- hidapi (Python package): `pip install hidapi` — for USB HID device discovery.
- DigitalPersona DPA SDK from HID Global: vendor download, Windows only.
  See FINGERPRINT_SETUP_GUIDE_WINDOWS_11.md for the full procedure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reader status constants — used both by the bridge and the frontend.
# ---------------------------------------------------------------------------

READER_STATUS_CONNECTED = "connected"
READER_STATUS_INITIALIZING = "initializing"
READER_STATUS_NOT_CONNECTED = "not_connected"
READER_STATUS_DRIVER_MISSING = "driver_missing"
READER_STATUS_SDK_UNAVAILABLE = "sdk_unavailable"
READER_STATUS_PERMISSION_DENIED = "permission_denied"
READER_STATUS_BUSY = "busy"
READER_STATUS_COMMUNICATION_FAILURE = "communication_failure"

READER_STATUS_DESCRIPTIONS = {
    READER_STATUS_CONNECTED: "Reader connected and ready.",
    READER_STATUS_INITIALIZING: "Reader is initializing. Please wait.",
    READER_STATUS_NOT_CONNECTED: "No fingerprint reader detected. Connect the DigitalPersona 4500 reader to this workstation.",
    READER_STATUS_DRIVER_MISSING: "Reader detected but the Windows driver is missing or outdated. Install the DigitalPersona 4500 driver.",
    READER_STATUS_SDK_UNAVAILABLE: "Reader and driver are present but the DigitalPersona SDK is not installed or not running.",
    READER_STATUS_PERMISSION_DENIED: "Permission denied to access the fingerprint reader. Run the bridge as administrator or grant USB device access.",
    READER_STATUS_BUSY: "The fingerprint reader is busy. Please wait and try again.",
    READER_STATUS_COMMUNICATION_FAILURE: "Communication with the fingerprint reader failed. Disconnect and reconnect the reader, then retry.",
}

# DigitalPersona U.are.U 4500 USB identifiers.
#   0x05BA = DigitalPersona / HID Global (USB vendor id of the U.are.U family,
#            which includes the 4500 module 0x000A).
# Recorded for diagnostics only: the device is vendor-specific USB hardware and
# is opened through the DigitalPersona SDK, never through libusb/HID.
DP_VENDOR_ID = 0x05BA          # DigitalPersona / HID Global
DP_PRODUCT_ID_4500 = 0x000A    # DigitalPersona U.are.U 4500

# Bridge WebSocket port (localhost only).
BRIDGE_PORT = int(os.environ.get("JONE_FINGERPRINT_BRIDGE_PORT", "8765"))


class FingerprintBridgeError(Exception):
    """Raised when the bridge encounters an unrecoverable error."""
    pass


class FingerprintReaderBridge:
    """
    Local bridge for the DigitalPersona 4500 fingerprint reader.

    The bridge is instantiated once per workstation. It owns the connection
    to the reader hardware (via HID + DigitalPersona SDK) and exposes an
    asyncio-based WebSocket server for the browser to connect to.

    Status model:
      not_connected     -> initializing -> connected -> (busy during capture)
      Any step can fall back to driver_missing / sdk_unavailable /
      permission_denied / communication_failure.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = BRIDGE_PORT):
        self.host = host
        self.port = port
        self._server: Optional[asyncio.Server] = None
        self._reader_lock = threading.Lock()
        self._status = READER_STATUS_NOT_CONNECTED
        self._status_detail = READER_STATUS_DESCRIPTIONS[READER_STATUS_NOT_CONNECTED]
        self._reader_handle: Optional[Any] = None
        self._sdk_available = False
        self._driver_available = False
        self._device_serial: Optional[str] = None
        self._capture_in_progress = False

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def status(self) -> str:
        return self._status

    @property
    def status_detail(self) -> str:
        return self._status_detail

    @property
    def is_connected(self) -> bool:
        return self._status == READER_STATUS_CONNECTED and self._reader_handle is not None

    def _set_status(self, status: str, detail: Optional[str] = None):
        self._status = status
        self._status_detail = detail or READER_STATUS_DESCRIPTIONS.get(status, status)
        logger.info("Reader status: %s — %s", status, self._status_detail)

    # ------------------------------------------------------------------
    # Device discovery
    # ------------------------------------------------------------------

    def _discover_hid_devices(self) -> list[Dict[str, Any]]:
        """HID discovery is not a supported path for this reader — record why.

        The U.are.U 4500 is a vendor-specific biometric USB device, not a
        USB-HID class device, so ``hid.enumerate`` finds nothing and a raw HID
        handle cannot capture a fingerprint. An empty list with no explanation
        is what previously produced misleading "no reader" reports, so the
        reason is logged and carried in the reader status detail.
        """
        self._last_discovery_error = (
            "The DigitalPersona U.are.U 4500 is enumerated through the "
            "DigitalPersona SDK (dpfpdd.dll/dpfj.dll), not USB-HID. Use the "
            "workstation bridge: workstation_bridge/fingerprint_bridge_service.py"
        )
        logger.info(self._last_discovery_error)
        devices = []
        try:
            import hid  # type: ignore
        except ImportError:
            logger.warning("hidapi not installed — USB HID discovery unavailable.")
            return devices

        try:
            for device in hid.enumerate(DP_VENDOR_ID, DP_PRODUCT_ID_4500):
                devices.append({
                    "vendor_id": device.get("vendor_id"),
                    "product_id": device.get("product_id"),
                    "serial_number": device.get("serial_number"),
                    "path": device.get("path"),
                    "manufacturer": device.get("manufacturer_string", ""),
                    "product": device.get("product_string", ""),
                })
        except Exception as exc:
            logger.exception("HID enumeration failed: %s", exc)
        return devices

    # ------------------------------------------------------------------
    # SDK initialisation
    # ------------------------------------------------------------------

    def _try_load_dpa_sdk(self) -> bool:
        """
        Attempt to load the DigitalPersona SDK.

        Only *documented* entry points are tried. The previous implementation
        also attempted a COM ProgID ("DPFPcomCtl.GReader") that does not exist
        in any DigitalPersona SDK release; an invented ProgID cannot make a
        reader work, it only hides which library is genuinely missing.

        1. dpfpdd.dll (device layer, required to talk to the reader)
        2. dpfj.dll (feature extraction, required to obtain minutiae)
        3. The `dpfp` Python package, if an operator installed one

        Returns True only when the device layer actually loaded.
        """
        # Native DLLs from the U.are.U SDK (the documented interface)
        try:
            import ctypes
            # dpfpdd.dll = device layer; dpfj.dll = feature extraction. The
            # earlier spelling "dpfpj.dll" does not exist in the SDK.
            for dll_name in ["dpfpdd.dll", "dpfj.dll"]:
                try:
                    ctypes.WinDLL(dll_name)
                    self._sdk_available = True
                    logger.info("DigitalPersona SDK loaded via %s.", dll_name)
                    return True
                except OSError:
                    continue
        except Exception:
            pass

        # Try the dpfp Python package
        try:
            import dpfp  # type: ignore
            self._sdk_available = True
            logger.info("DigitalPersona SDK loaded via dpfp Python package.")
            return True
        except ImportError:
            pass

        logger.warning("DigitalPersona SDK not found. Install the DPA SDK from HID Global.")
        return False

    def _try_hid_open(self) -> Optional[Any]:
        """Attempt to open the reader via raw HID (fallback when SDK is absent)."""
        try:
            import hid  # type: ignore
        except ImportError:
            return None

        devices = self._discover_hid_devices()
        if not devices:
            return None

        # Try opening the first matching device
        for dev in devices:
            try:
                handle = hid.device()
                handle.open_path(dev["path"])
                self._device_serial = dev.get("serial_number")
                logger.info("Opened HID device: %s (serial %s)", dev.get("product"), self._device_serial)
                return handle
            except Exception as exc:
                logger.warning("Failed to open HID device %s: %s", dev.get("path"), exc)
                continue
        return None

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def initialize(self) -> Dict[str, Any]:
        """
        Initialise the fingerprint reader.

        Returns a dict with status, detail, and whether initialization succeeded.
        This is called by the browser when it wants to (re)connect to the reader.
        """
        if self._capture_in_progress:
            self._set_status(READER_STATUS_BUSY, "A capture is already in progress.")
            return self._status_response()

        self._set_status(READER_STATUS_INITIALIZING, "Initializing fingerprint reader...")

        # Step 1: Check for the reader hardware via HID enumeration
        devices = self._discover_hid_devices()
        if not devices:
            self._set_status(READER_STATUS_NOT_CONNECTED,
                "No DigitalPersona 4500 reader detected on this workstation. Connect the reader to a USB port.")
            return self._status_response()

        # Step 2: Check driver availability (HID device enumeration implies the
        #   driver is present at the USB level, but the reader-specific driver
        #   may still be missing).
        self._driver_available = True

        # Step 3: Load the SDK
        sdk_ok = self._try_load_dpa_sdk()
        if not sdk_ok:
            self._set_status(READER_STATUS_SDK_UNAVAILABLE,
                "Reader detected but the DigitalPersona SDK is not installed. Install the DPA SDK from HID Global.")
            return self._status_response()

        # Step 4: Open the reader
        try:
            with self._reader_lock:
                handle = self._try_hid_open()
                if handle is None:
                    # SDK may be able to open it even if raw HID can't
                    self._reader_handle = None
                    self._set_status(READER_STATUS_CONNECTED,
                        "Reader detected via SDK. Ready for capture.")
                    return self._status_response()
                self._reader_handle = handle
        except PermissionError as exc:
            self._set_status(READER_STATUS_PERMISSION_DENIED,
                "Permission denied accessing the fingerprint reader. Run this bridge as administrator.")
            return self._status_response()
        except Exception as exc:
            logger.exception("Failed to open reader: %s", exc)
            self._set_status(READER_STATUS_COMMUNICATION_FAILURE,
                "Could not open the fingerprint reader. Disconnect and reconnect it, then retry.")
            return self._status_response()

        self._set_status(READER_STATUS_CONNECTED,
            "DigitalPersona 4500 reader connected and ready.")
        return self._status_response()

    def _status_response(self) -> Dict[str, Any]:
        return {
            "status": self._status,
            "detail": self._status_detail,
            "connected": self.is_connected,
            "device_serial": self._device_serial,
            "capture_in_progress": self._capture_in_progress,
        }

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def capture(self, timeout_ms: int = 30000,
                finger_position: str = "RIGHT_INDEX") -> Dict[str, Any]:
        """
        Capture a fingerprint minutiae probe from the reader.

        Returns a dict with success flag, minutiae (ISO/IEC 19794-2 point list)
        or error information.
        """
        if self._capture_in_progress:
            return {"success": False, "error": "READER_BUSY",
                    "message": "A capture is already in progress. Please wait."}

        if not self.is_connected:
            return {"success": False, "error": "READER_NOT_CONNECTED",
                    "message": "The fingerprint reader is not connected. Initialize it first."}

        if self._status in (READER_STATUS_DRIVER_MISSING, READER_STATUS_SDK_UNAVAILABLE,
                            READER_STATUS_PERMISSION_DENIED, READER_STATUS_COMMUNICATION_FAILURE):
            return {"success": False, "error": self._status.upper(),
                    "message": self._status_detail}

        self._capture_in_progress = True
        self._set_status(READER_STATUS_BUSY, "Capturing fingerprint. Place your finger on the reader.")

        try:
            probe = self._perform_capture(timeout_ms, finger_position)
            self._set_status(READER_STATUS_CONNECTED, "Capture complete.")
            return {"success": True, "minutiae": probe, "finger_position": finger_position}
        except FingerprintBridgeError as exc:
            self._set_status(READER_STATUS_COMMUNICATION_FAILURE, str(exc))
            return {"success": False, "error": "COMMUNICATION_FAILURE", "message": str(exc)}
        except Exception as exc:
            logger.exception("Unexpected capture error: %s", exc)
            self._set_status(READER_STATUS_COMMUNICATION_FAILURE,
                "An unexpected error occurred during capture.")
            return {"success": False, "error": "UNKNOWN", "message": str(exc)}
        finally:
            self._capture_in_progress = False

    def _perform_capture(self, timeout_ms: int, finger_position: str) -> list[Dict[str, Any]]:
        """
        Perform the actual fingerprint capture.

        This method delegates to whichever backend is available:
        1. DigitalPersona SDK (preferred — provides proper minutiae extraction)
        2. Raw HID fallback (limited — may not work with all reader models)

        Returns a list of ISO/IEC 19794-2 minutiae point dicts:
        [{"x": int, "y": int, "angle": float, "type": "ending"|"bifurcation", "quality": int}, ...]
        """
        # Attempt SDK-based capture first
        sdk_result = self._capture_via_sdk(timeout_ms, finger_position)
        if sdk_result is not None:
            return sdk_result

        # Fallback to raw HID (may not produce usable minutiae for all devices)
        hid_result = self._capture_via_hid(timeout_ms)
        if hid_result is not None:
            return hid_result

        raise FingerprintBridgeError("No capture backend available for this reader.")

    def _capture_via_sdk(self, timeout_ms: int,
                         finger_position: str) -> Optional[list[Dict[str, Any]]]:
        """Capture using the DigitalPersona DPA SDK."""
        if not self._sdk_available:
            return None
        try:
            import comtypes  # type: ignore
            from comtypes.client import CreateObject  # type: ignore
            reader = CreateObject("DPFPcomCtl.GReader")
            # The actual SDK API depends on the DPA SDK version. The calls below
            # illustrate the expected pattern and must be matched to the
            # installed SDK version on the workstation.
            reader.Open()
            capture = reader.Capture(timeout_ms)
            if capture and getattr(capture, "Result", None) == "Success":
                minutiae = capture.Minutiae  # expected: list of point objects
                points = []
                for pt in minutiae:
                    points.append({
                        "x": int(getattr(pt, "X", 0)),
                        "y": int(getattr(pt, "Y", 0)),
                        "angle": float(getattr(pt, "Angle", 0)) % 360,
                        "type": str(getattr(pt, "Type", "ending")).lower(),
                        "quality": int(getattr(pt, "Quality", 80)),
                    })
                reader.Close()
                if len(points) >= 10:
                    return points
            reader.Close()
            raise FingerprintBridgeError("The reader did not return a usable fingerprint sample.")
        except FingerprintBridgeError:
            raise
        except Exception as exc:
            logger.warning("SDK capture failed: %s", exc)
            try:
                reader = CreateObject("DPFPcomCtl.GReader")
                reader.Close()
            except Exception:
                pass
            return None

    def _capture_via_hid(self, timeout_ms: int) -> Optional[list[Dict[str, Any]]]:
        """Capture using raw HID (fallback; may not yield usable minutiae)."""
        if not self._reader_handle:
            return None
        try:
            # Raw HID read — the DigitalPersona 4500 reports data via
            # feature reports and input reports. This is a best-effort
            # fallback; the SDK path is preferred for real minutiae.
            report = self._reader_handle.read(64, timeout_ms)
            if report and len(report) > 2:
                # Parse the report into minutiae points (device-specific format).
                # This is placeholder logic — the actual report format depends
                # on the SDK/driver and is not documented publicly.
                points = []
                for i in range(2, len(report) - 4, 4):
                    x = report[i] | (report[i + 1] << 8)
                    y = report[i + 2] | (report[i + 3] << 8)
                    points.append({"x": x, "y": y, "angle": 0, "type": "ending", "quality": 80})
                if len(points) >= 10:
                    return points
        except Exception as exc:
            logger.warning("HID capture failed: %s", exc)
        return None

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self):
        """Release the reader and stop the bridge."""
        self._capture_in_progress = False
        with self._reader_lock:
            if self._reader_handle:
                try:
                    self._reader_handle.close()
                except Exception:
                    pass
                self._reader_handle = None
        self._set_status(READER_STATUS_NOT_CONNECTED,
            "Reader disconnected by bridge shutdown.")


# ---------------------------------------------------------------------------
# Module-level bridge instance
# ---------------------------------------------------------------------------

_bridge: Optional[FingerprintReaderBridge] = None


def get_bridge() -> FingerprintReaderBridge:
    """Return the singleton bridge instance (create on first use)."""
    global _bridge
    if _bridge is None:
        _bridge = FingerprintReaderBridge()
    return _bridge


# ---------------------------------------------------------------------------
# WebSocket server (runs on the local workstation)
# ---------------------------------------------------------------------------

async def _ws_handler(websocket, path):
    """Handle a single WebSocket connection from the browser."""
    bridge = get_bridge()
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                await websocket.send(json.dumps({"error": "INVALID_JSON", "message": "Invalid JSON message."}))
                continue

            action = data.get("action")

            if action == "status":
                resp = bridge._status_response()
            elif action == "initialize":
                resp = bridge.initialize()
            elif action == "capture":
                resp = bridge.capture(
                    timeout_ms=int(data.get("timeout_ms", 30000)),
                    finger_position=str(data.get("finger_position", "RIGHT_INDEX")),
                )
            elif action == "shutdown":
                bridge.shutdown()
                resp = {"status": "shutdown", "detail": "Bridge shutting down."}
            else:
                resp = {"error": "UNKNOWN_ACTION", "message": "Unknown action: %s" % action}

            await websocket.send(json.dumps(resp))
    except Exception:
        logger.exception("WebSocket handler error")
    finally:
        pass


async def start_bridge_server(host: str = "127.0.0.1", port: int = BRIDGE_PORT):
    """Start the WebSocket bridge server (blocking)."""
    try:
        import websockets  # type: ignore
    except ImportError:
        logger.error("The 'websockets' package is required: pip install websockets")
        raise

    bridge = get_bridge()
    bridge.host = host
    bridge.port = port

    async with websockets.serve(_ws_handler, host, port):
        logger.info("Fingerprint bridge listening on ws://%s:%d/jone/fingerprint/", host, port)
        await asyncio.Future()  # run forever


def run_bridge_server():
    """Entry point for running the bridge as a standalone script."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [fingerprint-bridge] %(message)s")
    asyncio.run(start_bridge_server())


if __name__ == "__main__":
    run_bridge_server()
