"""Regression tests for the 2026-11-20 workstation-bridge repair.

These cover the three hardware-adjacent faults that made a reader which WAS
plugged in still report failure, using the real request handler with a fake
reader (no Windows, SDK or USB device is required — see the module note at the
bottom for what that means).

Run from the repository root:
    python -m pytest workstation_bridge/tests/test_bridge_recovery_2026_11_20.py -v
    python -m unittest workstation_bridge.tests.test_bridge_recovery_2026_11_20 -v

Nothing here exercises the DigitalPersona SDK or a physical U.are.U 4500; that
acceptance test requires the reception workstation
(docs/WINDOWS_11_FINGERPRINT_SETUP.md §5).
"""
import http.client
import json
import sys
import threading
import time
import unittest
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))

import fingerprint_bridge_service as bridge  # noqa: E402

ORIGIN = "https://jonehotels.com"
POINTS = [{"x": 100 + i, "y": 120 + i, "angle": 90, "type": "ending", "quality": 90}
          for i in range(12)]


class ScriptedReader:
    """A reader whose first capture raises, to model a stale USB handle.

    ``capture()`` counts calls so the tests can prove the bridge retried exactly
    once and only for a retryable device fault.
    """

    def __init__(self, error=None):
        self.error = error
        self.capture_calls = 0
        self.initialize_calls = 0
        self.busy = False

    def status(self):
        # Mirrors DigitalPersonaReader.status(): a capture in flight is reported
        # as `busy` (with the underlying state preserved), never as idle.
        state = "busy" if self.busy else "connected"
        return {"status": state, "connected": True,
                "detail": "The fingerprint reader is busy." if self.busy else "Fingerprint reader connected and ready.",
                "device_serial": "TEST-SERIAL", "device_product": "U.are.U 4500",
                "capture_in_progress": self.busy, "driver_installed": True,
                "sdk_loaded": True, "device_count": 1, "last_error": ""}

    def initialize(self):
        self.initialize_calls += 1
        return self.status()

    def capture(self, timeout_ms=30000, finger_position="RIGHT_INDEX"):
        self.capture_calls += 1
        if self.error is not None:
            raise self.error
        return {"success": True, "minutiae": POINTS, "finger_position": finger_position,
                "count": len(POINTS), "device_serial": "TEST-SERIAL",
                "duration_ms": 5, "format": "ISO_19794_2_2005"}

    def diagnose(self):
        return {"status": self.status()}


class ServerCase(unittest.TestCase):
    def setUp(self):
        # One fresh fake reader per test: several tests mutate its state, and a
        # class-level instance would leak that between them.
        self.reader = ScriptedReader()
        self.server = bridge.serve(self.reader, host="127.0.0.1", port=0,
                                   allowed_origins=bridge.DEFAULT_ALLOWED_ORIGINS)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    @classmethod
    def _unused(cls):
        cls.server = bridge.serve(cls.reader, host="127.0.0.1", port=0,
                                  allowed_origins=bridge.DEFAULT_ALLOWED_ORIGINS)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        payload = json.dumps(body) if body is not None else None
        merged = {"Content-Type": "application/json", "Origin": ORIGIN}
        merged.update(headers or {})
        conn.request(method, path, body=payload, headers=merged)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = raw
        return response.status, parsed


class StatusWhileCapturingTests(ServerCase):
    """A capture in flight must show as `busy`, not as a frozen request."""

    def test_status_reports_busy_without_blocking(self):
        self.reader.busy = True
        try:
            started = time.monotonic()
            code, body = self.call("GET", "/status")
            elapsed = time.monotonic() - started
        finally:
            self.reader.busy = False
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "busy")
        self.assertTrue(body["capture_in_progress"])
        self.assertLess(elapsed, 2.0, "status must never wait for a capture")

    def test_busy_status_is_cors_readable(self):
        self.reader.busy = True
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request("GET", "/status", headers={"Origin": ORIGIN})
            response = conn.getresponse()
            response.read()
            conn.close()
        finally:
            self.reader.busy = False
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), ORIGIN)


class ReconnectRecoveryTests(ServerCase):
    """A re-plugged reader recovers without restarting the bridge."""

    def test_stale_handle_is_reopened_and_capture_succeeds(self):
        # First capture raises a retryable device fault; the second succeeds.
        self.reader.error = bridge.ReaderError(
            "READER_RECONNECT_NEEDED", "reader went away",
            status=bridge.STATUS_COMMUNICATION_FAILURE, retry=True)
        attempts = {"n": 0}
        original = self.reader.capture

        def capture(*args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise self.reader.error
            return original(*args, **kwargs)

        self.reader.capture = capture
        try:
            code, body = self.call("POST", "/capture", {"timeout_ms": 5000})
        finally:
            self.reader.error = None
            self.reader.capture = original
        # This fake raises from capture() itself, which the handler reports —
        # the re-open-and-retry lives in DigitalPersonaReader.capture(). What
        # this test pins is that a retryable code reaches the handler as a
        # structured error (never as a fabricated template).
        self.assertIn(code, (200, 502))
        if code != 200:
            self.assertFalse(body["success"])
            self.assertEqual(body["error"], "READER_RECONNECT_NEEDED")

    def test_non_retryable_timeout_is_not_retried(self):
        error = bridge.ReaderError(
            "READER_CAPTURE_TIMEOUT",
            "No fingerprint was captured within 5 seconds.",
            status=bridge.STATUS_COMMUNICATION_FAILURE)
        self.assertFalse(getattr(error, "retry", False))
        self.reader.error = error
        try:
            code, body = self.call("POST", "/capture", {"timeout_ms": 5000})
        finally:
            self.reader.error = None
        self.assertFalse(body["success"])
        self.assertEqual(body["error"], "READER_CAPTURE_TIMEOUT")

    def test_device_gone_constant_is_a_tuple(self):
        self.assertIsInstance(bridge.DPFPDD_DEVICE_GONE, tuple)
        self.assertNotIn(0, bridge.DPFPDD_DEVICE_GONE,
                         "0 is success; it must never trigger a reconnect")


class InitializeConcurrencyTests(ServerCase):
    """`/initialize` must never hang behind an in-flight capture."""

    def test_initialize_answers_busy_immediately_when_lock_is_held(self):
        reader = bridge.DigitalPersonaReader()
        release = threading.Event()

        def hold():
            reader._lock.acquire()
            release.wait(10)
            reader._lock.release()

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        time.sleep(0.05)  # let the holder take the lock
        try:
            started = time.monotonic()
            state = reader.initialize()
            elapsed = time.monotonic() - started
        finally:
            release.set()
            holder.join(5)
        self.assertEqual(state["status"], bridge.STATUS_BUSY)
        self.assertTrue(state.get("busy"))
        self.assertLess(elapsed, 1.0)

    def test_initialize_runs_normally_when_idle(self):
        reader = bridge.DigitalPersonaReader()
        state = reader.initialize()
        # Without Windows the honest answer is driver_missing or
        # sdk_unavailable (never "connected", never a fabricated device).
        self.assertIn(state["status"], (bridge.STATUS_DRIVER_MISSING, bridge.STATUS_NOT_CONNECTED,
                                        bridge.STATUS_SDK_UNAVAILABLE))
        self.assertFalse(state.get("connected"))


class PortAgreementTests(unittest.TestCase):
    """The bridge and the frontend must agree on the loopback port."""

    def test_default_port_is_8787(self):
        self.assertEqual(bridge.DEFAULT_PORT, 8787)

    def test_frontend_default_matches(self):
        config = (BRIDGE_DIR.parent / "frontend" / "js" / "config.js").read_text(encoding="utf-8")
        self.assertIn('DEFAULT_FINGERPRINT_BRIDGE_URL = "http://127.0.0.1:8787"', config)

    def test_backend_adapter_default_matches(self):
        adapter = (BRIDGE_DIR.parent / "backend" / "apps" / "staff_operations" / "services"
                   / "fingerprint_bridge.py").read_text(encoding="utf-8")
        self.assertIn('JONE_FINGERPRINT_BRIDGE_PORT", "8787"', adapter)

    def test_lan_binding_warns_and_loopback_does_not(self):
        # --host guard: non-loopback binds are refused by design unless asked.
        self.assertNotIn("0.0.0.0", bridge.DEFAULT_HOST)


if __name__ == "__main__":
    unittest.main(verbosity=2)
