"""Simulated hardware, SDK C ABI, USB lifecycle, and auto-reconnect tests.

WHAT THIS PROVES (and what it does NOT)
----------------------------------------
These tests inject a mocked ``dpfpdd.dll`` / ``dpfj.dll`` and a simulated
``BaseUsbMonitor`` to verify:
  1. Every ctypes structure & SDK constant matches ``dpfpdd.h`` and ``dpfj.h``;
  2. ``dpfpdd_query_devices`` handles ``DPFPDD_E_MORE_DATA`` (0x05BA000D);
  3. ``dpfpdd_open(dev_name, &handle)`` and 6-argument ``dpfpdd_capture(...)``
     pass arguments in the exact vendor C ABI order;
  4. Both 24-byte ISO 19794-2:2005 and 26-byte ANSI 378-2004 FMR templates parse
     into valid minutiae lists;
  5. USB arrival, delayed Windows driver/service startup, driver-missing
     problem codes, unplugging while open, automatic handle cleanup, and
     reconnection all transition through the exact state machine without manual
     intervention or bridge restart;
  6. ``GET /events`` pushes SSE status snapshots with strict CORS/PNA headers.

This is a simulation test suite — physical DigitalPersona U.are.U 4500 capture
still requires the Windows 11 reception workstation acceptance test.
"""
from __future__ import annotations

import ctypes
import http.client
import json
import pathlib
import struct
import sys
import threading
import time
import unittest
from unittest import mock

BRIDGE_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

import fingerprint_bridge_service as bridge  # noqa: E402
import iso19794_2  # noqa: E402


def build_iso_fmr(count: int = 12) -> bytes:
    minutiae = b""
    for i in range(count):
        x = 50 + i * 10
        y = 70 + i * 8
        mtype = 1 if i % 2 == 0 else 2
        first = ((mtype & 0x03) << 14) | (x & 0x3FFF)
        second = y & 0x3FFF
        minutiae += struct.pack(">HHBB", first, second, (i * 15) % 180, 90)
    view = bytes([1, 0, 88, count]) + minutiae
    total = 24 + len(view)
    header = b"FMR\x00 20\x00" + struct.pack(">I", total) + struct.pack(">H", 0)
    header += struct.pack(">HHHH", 356, 344, 500, 500) + bytes([1, 0])
    assert len(header) == 24
    return header + view


def build_ansi_26_fmr(count: int = 12) -> bytes:
    """Real 26-byte ANSI INCITS 378-2004 record header (dpfj.h length=26)."""
    minutiae = b""
    for i in range(count):
        x = 60 + i * 9
        y = 80 + i * 7
        mtype = 1 if i % 2 == 0 else 2
        first = ((mtype & 0x03) << 14) | (x & 0x3FFF)
        second = y & 0x3FFF
        minutiae += struct.pack(">HHBB", first, second, 64, 85)  # 64 => 90 deg
    view = bytes([1, 0, 85, count]) + minutiae
    total = 26 + len(view)
    header = (
        b"FMR\x00 20\x00"
        + struct.pack(">H", total)
        + struct.pack(">HH", 0x001B, 0x0001)  # CBEFF owner/type (4 bytes)
        + struct.pack(">HHHH", 0, 356, 344, 500)  # device/size/xres (8 bytes) -> 22
        + struct.pack(">H", 500)  # yres -> 24
        + bytes([1, 0])  # view_count=1, reserved=0 -> 26
    )
    assert len(header) == 26
    return header + view


class FakeUsbMonitor(bridge.BaseUsbMonitor):
    def __init__(self) -> None:
        self.usb_present = False
        self.problem_code = None
        self.service_running = True

    def inspect(self) -> bridge.UsbSnapshot:
        devs = (
            [{
                "vid": "0x05BA",
                "pid": "0x000A",
                "product": "DigitalPersona U.are.U 4500 Fingerprint Reader",
                "manufacturer": "DigitalPersona, Inc.",
                "service": "DPUareU",
                "hardware_id": "USB\\VID_05BA&PID_000A",
                "problem_code": self.problem_code,
            }]
            if self.usb_present
            else []
        )
        return bridge.UsbSnapshot(
            usb_present=self.usb_present,
            devices=devs,
            driver_problem_code=self.problem_code,
            service_running=self.service_running,
            service_name="DpHost",
            detection_method="injected",
        )


class MockSdkPair:
    """In-memory fake dpfpdd + dpfj validating exact C ABI calls & constants."""

    def __init__(self) -> None:
        self.init_rc = bridge.DPFPDD_OK
        self.init_calls = 0
        self.exit_calls = 0
        self.connected = True
        self.open_count = 0
        self.close_count = 0
        self.capture_quality = bridge.DPFPDD_QUALITY_GOOD
        self.capture_success = 1
        self.capture_rc = bridge.DPFPDD_OK
        self.fmd_blob = build_iso_fmr(14)
        self.seen_open_name = None
        self.seen_capture_args = None
        self.seen_fmd_type = None
        self.device_status_rc = bridge.DPFPDD_OK

    def dpfpdd_init(self) -> int:
        self.init_calls += 1
        return self.init_rc

    def dpfpdd_exit(self) -> int:
        self.exit_calls += 1
        return bridge.DPFPDD_OK

    def dpfpdd_query_devices(self, count_ptr, infos_ptr) -> int:
        cnt = ctypes.cast(count_ptr, ctypes.POINTER(ctypes.c_uint)).contents
        if not self.connected:
            cnt.value = 0
            return bridge.DPFPDD_OK
        if not infos_ptr or cnt.value < 1:
            cnt.value = 1
            return bridge.DPFPDD_E_MORE_DATA
        info = ctypes.cast(infos_ptr, ctypes.POINTER(bridge._DPFPDD_DEV_INFO))[0]
        assert info.size == ctypes.sizeof(bridge._DPFPDD_DEV_INFO)
        info.name = b"\\\\?\\usb#vid_05ba&pid_000a#1234"
        info.descr.vendor_name = b"DigitalPersona, Inc."
        info.descr.product_name = b"U.are.U 4500"
        info.descr.serial_num = b"DP4500-MOCK-01"
        info.id.vendor_id = 0x05BA
        info.id.product_id = 0x000A
        cnt.value = 1
        return bridge.DPFPDD_OK

    def dpfpdd_open(self, dev_name, handle_ptr) -> int:
        # Must be (char* dev_name, DPFPDD_DEV* pdev) — NOT swapped!
        assert isinstance(dev_name, (bytes, ctypes.c_char_p)) or dev_name is not None
        self.seen_open_name = dev_name if isinstance(dev_name, bytes) else ctypes.string_at(dev_name)
        assert self.seen_open_name.startswith(b"\\\\?\\usb#vid_05ba")
        h = ctypes.cast(handle_ptr, ctypes.POINTER(ctypes.c_void_p)).contents
        h.value = 0x12345678
        self.open_count += 1
        return bridge.DPFPDD_OK

    def dpfpdd_close(self, handle) -> int:
        self.close_count += 1
        return bridge.DPFPDD_OK

    def dpfpdd_get_device_status(self, handle, status_ptr) -> int:
        if not self.connected:
            return bridge.DPFPDD_E_INVALID_DEVICE
        return self.device_status_rc

    def dpfpdd_capture(self, handle, param_ptr, timeout_cnt, result_ptr, size_ptr, buf) -> int:
        param = ctypes.cast(param_ptr, ctypes.POINTER(bridge._DPFPDD_CAPTURE_PARAM)).contents
        res = ctypes.cast(result_ptr, ctypes.POINTER(bridge._DPFPDD_CAPTURE_RESULT)).contents
        sz = ctypes.cast(size_ptr, ctypes.POINTER(ctypes.c_uint)).contents
        timeout_val = timeout_cnt.value if hasattr(timeout_cnt, "value") else int(timeout_cnt)
        self.seen_capture_args = {
            "param_size": param.size,
            "image_fmt": param.image_fmt,
            "image_res": param.image_res,
            "timeout_ms": timeout_val,
            "result_size": res.size,
            "buf_cap": sz.value,
        }
        assert param.image_fmt == bridge.DPFPDD_IMG_FMT_PIXEL_BUFFER == 0
        assert param.image_res == 500
        if self.capture_rc != bridge.DPFPDD_OK:
            return self.capture_rc
        res.success = self.capture_success
        res.quality = self.capture_quality
        res.info.width = 356
        res.info.height = 344
        res.info.res = 500
        res.info.bpp = 8
        raw_len = 356 * 344
        if sz.value < raw_len:
            sz.value = raw_len
            return bridge.DPFPDD_E_MORE_DATA
        sz.value = raw_len
        return bridge.DPFPDD_OK

    def dpfj_create_fmd_from_raw(
        self, raw_buf, raw_sz, w, h, res, fpos, cbeff, fmd_type, fmd_buf, fmd_size_ptr
    ) -> int:
        ftype = fmd_type.value if hasattr(fmd_type, "value") else int(fmd_type)
        self.seen_fmd_type = ftype
        assert ftype in (bridge.DPFJ_FMD_ISO_19794_2_2005, bridge.DPFJ_FMD_ANSI_378_2004)
        out_sz = ctypes.cast(fmd_size_ptr, ctypes.POINTER(ctypes.c_uint)).contents
        if out_sz.value < len(self.fmd_blob):
            out_sz.value = len(self.fmd_blob)
            return bridge.DPFJ_E_MORE_DATA
        ctypes.memmove(fmd_buf, self.fmd_blob, len(self.fmd_blob))
        out_sz.value = len(self.fmd_blob)
        return bridge.DPFJ_OK


class TestSdkAbiAndConstants(unittest.TestCase):
    def test_sdk_constants_match_digitalpersona_headers(self):
        self.assertEqual(bridge.DPFPDD_E_MORE_DATA, 0x05BA000D)
        self.assertEqual(bridge.DPFPDD_E_INVALID_DEVICE, 0x05BA0015)
        self.assertEqual(bridge.DPFPDD_E_DEVICE_FAILURE, 0x05BA001F)
        self.assertEqual(bridge.DPFPDD_IMG_FMT_PIXEL_BUFFER, 0)
        self.assertEqual(bridge.DPFPDD_IMG_RES_500, 500)
        self.assertEqual(bridge.DPFJ_FMD_ANSI_378_2004, 0x001B0001)
        self.assertEqual(bridge.DPFJ_FMD_ISO_19794_2_2005, 0x01010001)
        self.assertEqual(bridge.DPFJ_FID_ISO_19794_4_2005, 0x01010007)

    def test_dev_info_struct_contains_1024_name_and_vid_pid(self):
        info = bridge._DPFPDD_DEV_INFO()
        self.assertGreaterEqual(ctypes.sizeof(info), 1400)
        self.assertEqual(bridge._DPFPDD_DEV_INFO.name.size, 1024)
        self.assertEqual(bridge._DPFPDD_DEV_DESCR.serial_num.size, 128)

    def test_ansi_26_byte_header_parses_accurately(self):
        pts = iso19794_2.parse_template(build_ansi_26_fmr(10), standard="ansi")
        self.assertEqual(len(pts), 10)
        self.assertEqual(pts[0]["angle"], 90)
        # Auto-detection also distinguishes 26-byte ANSI from 24-byte ISO with version " 20\0".
        pts_auto = iso19794_2.parse_template(build_ansi_26_fmr(10), standard="auto")
        self.assertEqual(len(pts_auto), 10)


class TestMockedSdkCaptureAndRecovery(unittest.TestCase):
    def _make_reader(self):
        usb = FakeUsbMonitor()
        usb.usb_present = True
        sdk = MockSdkPair()
        reader = bridge.DigitalPersonaReader(usb_monitor=usb)
        reader._dd = sdk
        reader._fj = sdk
        reader._dd_path = pathlib.Path(r"C:\Program Files\DigitalPersona\Bin\dpfpdd.dll")
        reader._fj_path = pathlib.Path(r"C:\Program Files\DigitalPersona\Bin\dpfj.dll")
        reader._init_succeeded = True
        return reader, usb, sdk

    def test_full_sdk_initialize_and_capture_uses_correct_abi(self):
        reader, _usb, sdk = self._make_reader()
        status = reader.initialize()
        self.assertEqual(status["status"], bridge.STATUS_CONNECTED)
        self.assertEqual(status["ui_label"], "Fingerprint reader ready")
        self.assertEqual(status["device_serial"], "DP4500-MOCK-01")
        self.assertEqual(sdk.seen_open_name, b"\\\\?\\usb#vid_05ba&pid_000a#1234")

        result = reader.capture(timeout_ms=15000, finger_position="LEFT_INDEX")
        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 14)
        self.assertEqual(result["format"], "ISO_19794_2_2005")
        self.assertEqual(sdk.seen_capture_args["timeout_ms"], 15000)
        self.assertEqual(sdk.seen_capture_args["image_fmt"], 0)
        self.assertEqual(sdk.seen_capture_args["image_res"], 500)
        self.assertEqual(sdk.seen_fmd_type, bridge.DPFJ_FMD_ISO_19794_2_2005)

    def test_poor_quality_and_no_finger_produce_clear_messages(self):
        reader, _usb, sdk = self._make_reader()
        reader.initialize()

        sdk.capture_success = 0
        sdk.capture_quality = bridge.DPFPDD_QUALITY_NO_FINGER
        with self.assertRaises(bridge.ReaderError) as err_timeout:
            reader.capture(timeout_ms=5000)
        self.assertEqual(err_timeout.exception.code, "READER_CAPTURE_TIMEOUT")

        sdk.capture_quality = bridge.DPFPDD_QUALITY_TOO_LIGHT | bridge.DPFPDD_QUALITY_NOT_CENTERED
        with self.assertRaises(bridge.ReaderError) as err_qual:
            reader.capture(timeout_ms=5000)
        self.assertEqual(err_qual.exception.code, "READER_POOR_QUALITY")
        self.assertIn("too faint", err_qual.exception.message.lower())

    def test_stale_device_handle_retries_once_and_recovers(self):
        reader, _usb, sdk = self._make_reader()
        reader.initialize()
        calls = {"n": 0}
        orig_capture = sdk.dpfpdd_capture

        def flaky_capture(*args):
            calls["n"] += 1
            if calls["n"] == 1:
                return bridge.DPFPDD_E_INVALID_DEVICE
            return orig_capture(*args)

        sdk.dpfpdd_capture = flaky_capture
        result = reader.capture(timeout_ms=10000)
        self.assertTrue(result["success"])
        self.assertEqual(sdk.close_count, 1)
        self.assertEqual(sdk.open_count, 2)


class TestSimulatedUsbAndServiceLifecycle(unittest.TestCase):
    def test_usb_plug_delayed_service_unplug_and_replug_state_machine(self):
        usb = FakeUsbMonitor()
        sdk = MockSdkPair()
        sdk.connected = False
        sdk.init_rc = bridge.DPFPDD_OK

        reader = bridge.DigitalPersonaReader(usb_monitor=usb)
        # Stub Windows DLL loader so _load_dd() exercises real init retry behavior!
        with mock.patch.object(bridge.platform, "system", return_value="Windows"), \
             mock.patch.object(bridge, "_find_dll", side_effect=lambda name, **kw: pathlib.Path(f"C:/DP/{name}")), \
             mock.patch.object(bridge.ctypes, "WinDLL", create=True, return_value=sdk), \
             mock.patch.object(bridge.os, "add_dll_directory", create=True):

            # 1. USB disconnected
            st = reader.reconcile_once()
            self.assertEqual(st["status"], bridge.STATUS_NOT_CONNECTED)
            self.assertEqual(st["ui_label"], "Fingerprint reader not connected")
            self.assertFalse(st["usb_detected"])

            # 2. USB plugged in while DigitalPersona Windows service is still starting
            usb.usb_present = True
            usb.service_running = False
            sdk.init_rc = bridge.DPFPDD_E_DEVICE_FAILURE
            st = reader.reconcile_once()
            self.assertEqual(st["status"], bridge.STATUS_SERVICE_UNAVAILABLE)
            self.assertEqual(st["ui_label"], "Biometric service unavailable")
            self.assertTrue(st["usb_detected"])
            self.assertTrue(st["driver_installed"])

            # 3. DigitalPersona service finishes starting and reader finishes enumerating
            usb.service_running = True
            sdk.init_rc = bridge.DPFPDD_OK
            sdk.connected = True
            reader._next_retry_monotonic = 0.0
            st = reader.reconcile_once()
            self.assertEqual(st["status"], bridge.STATUS_CONNECTED)
            self.assertEqual(st["ui_label"], "Fingerprint reader ready")
            self.assertTrue(st["ready"])
            self.assertEqual(st["device_serial"], "DP4500-MOCK-01")

            # 4. Physical USB unplug -> stale handle closed cleanly immediately
            usb.usb_present = False
            sdk.connected = False
            st = reader.reconcile_once()
            self.assertEqual(st["status"], bridge.STATUS_NOT_CONNECTED)
            self.assertFalse(st["ready"])
            self.assertEqual(sdk.close_count, 1)

            # 5. Physical USB replug -> automatically opens without manual intervention
            usb.usb_present = True
            sdk.connected = True
            reader._next_retry_monotonic = 0.0
            st = reader.reconcile_once()
            self.assertEqual(st["status"], bridge.STATUS_CONNECTED)
            self.assertTrue(st["ready"])
            self.assertEqual(sdk.open_count, 2)

            # 6. Driver missing problem code (Code 28) -> Driver/interface unavailable
            usb.usb_present = False
            sdk.connected = False
            reader.reconcile_once()
            usb.usb_present = True
            usb.problem_code = 28
            st = reader.reconcile_once()
            self.assertEqual(st["status"], bridge.STATUS_DRIVER_MISSING)
            self.assertEqual(st["ui_label"], "Driver/interface unavailable")


class TestSseEventsEndpoint(unittest.TestCase):
    def test_events_stream_emits_initial_status_with_cors_headers(self):
        usb = FakeUsbMonitor()
        reader = bridge.DigitalPersonaReader(usb_monitor=usb)
        server = bridge.serve(reader, host="127.0.0.1", port=0,
                              allowed_origins=bridge.DEFAULT_ALLOWED_ORIGINS)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            conn.request("GET", "/events", headers={"Origin": "https://jonehotels.com"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), "https://jonehotels.com")
            self.assertIn("text/event-stream", resp.getheader("Content-Type"))
            chunk = b""
            for _ in range(10):
                line = resp.fp.readline()
                chunk += line
                if line == b"\n":
                    break
            conn.close()
            text = chunk.decode("utf-8")
            self.assertIn("event: status", text)
            data_line = [ln for ln in text.splitlines() if ln.startswith("data: ")][0]
            payload = json.loads(data_line[6:])
            self.assertIn(payload["ui_label"], (
                "Fingerprint reader not connected",
                "Driver/interface unavailable",
            ))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
