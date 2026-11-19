"""Workstation bridge tests — no Windows, SDK or USB reader required.

Run from the repository root:
    python -m unittest discover -s workstation_bridge/tests -v

The HTTP tests inject a fake reader into the real request handler, so the
exact bytes a browser at https://jonehotels.com would exchange are covered
(health, status, CORS preflight, Private-Network-Access, capture errors).
They say nothing about the physical reader — that acceptance test needs the
reception workstation (docs/WINDOWS_11_FINGERPRINT_SETUP.md).
"""
import http.client
import json
import struct
import sys
import threading
import unittest
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))

import fingerprint_bridge_service as bridge  # noqa: E402
import iso19794_2  # noqa: E402

ORIGIN = "https://jonehotels.com"
POINTS = [{"x": 100 + i, "y": 120 + i, "angle": 90, "type": "ending", "quality": 90}
          for i in range(12)]


class FakeReader:
    def __init__(self, capture_error=None):
        self.capture_error = capture_error
        self.capture_calls = 0

    def status(self):
        return {"status": "connected", "connected": True,
                "detail": "Fingerprint reader connected and ready.",
                "device_serial": "TEST-SERIAL", "device_product": "U.are.U 4500",
                "capture_in_progress": False, "driver_installed": True,
                "sdk_loaded": True, "device_count": 1, "last_error": ""}

    def initialize(self):
        return self.status()

    def capture(self, timeout_ms=30000, finger_position="RIGHT_INDEX"):
        self.capture_calls += 1
        if self.capture_error is not None:
            raise self.capture_error
        return {"success": True, "minutiae": POINTS, "finger_position": finger_position,
                "count": len(POINTS), "device_serial": "TEST-SERIAL",
                "duration_ms": 5, "format": "ISO_19794_2_2005"}

    def diagnose(self):
        return {"status": self.status()}


class HttpCase(unittest.TestCase):
    reader = None

    @classmethod
    def setUpClass(cls):
        cls.server = bridge.serve(cls.reader or FakeReader(), host="127.0.0.1", port=0,
                                  allowed_origins=bridge.DEFAULT_ALLOWED_ORIGINS)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps(body) if body is not None else None
        all_headers = {"Content-Type": "application/json"}
        all_headers.update(headers or {})
        conn.request(method, path, body=payload, headers=all_headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response, raw

    def json_request(self, method, path, body=None, headers=None):
        response, raw = self.request(method, path, body=body, headers=headers)
        return response, json.loads(raw.decode("utf-8")) if raw else {}


class TestBridgeHttp(HttpCase):
    def test_health(self):
        response, payload = self.json_request("GET", "/health", headers={"Origin": ORIGIN})
        self.assertEqual(response.status, 200)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["service"], "jone-fingerprint-bridge")
        self.assertTrue(payload["loopback_only"])
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), ORIGIN)

    def test_status(self):
        response, payload = self.json_request("GET", "/status", headers={"Origin": ORIGIN})
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["status"], "connected")
        self.assertTrue(payload["connected"])

    def test_preflight_allowed_origin(self):
        response, raw = self.request("OPTIONS", "/capture", headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
            "Access-Control-Request-Private-Network": "true",
        })
        self.assertEqual(response.status, 204)
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), ORIGIN)
        self.assertIn("POST", response.getheader("Access-Control-Allow-Methods"))
        self.assertEqual(response.getheader("Access-Control-Allow-Private-Network"), "true")
        self.assertEqual(raw, b"")

    def test_preflight_initialize_allowed_origin(self):
        response, _raw = self.request("OPTIONS", "/initialize", headers={
            "Origin": ORIGIN, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        })
        self.assertEqual(response.status, 204)
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), ORIGIN)

    def test_rejected_origin_gets_no_cors_grant(self):
        response, payload = self.json_request("GET", "/status",
                                              headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status, 403)
        self.assertEqual(payload["error"], "ORIGIN_NOT_ALLOWED")
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))

    def test_initialize(self):
        response, payload = self.json_request("POST", "/initialize", body={},
                                              headers={"Origin": ORIGIN})
        self.assertEqual(response.status, 200)
        self.assertTrue(payload["connected"])

    def test_capture_success(self):
        response, payload = self.json_request(
            "POST", "/capture", body={"timeout_ms": 30000, "finger_position": "RIGHT_INDEX"},
            headers={"Origin": ORIGIN})
        self.assertEqual(response.status, 200)
        self.assertTrue(payload["success"])
        self.assertEqual(len(payload["minutiae"]), 12)

    def test_malformed_json_is_a_client_error(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/capture", body="{not json",
                     headers={"Content-Type": "application/json", "Origin": ORIGIN})
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()
        self.assertEqual(response.status, 400)
        self.assertEqual(payload["error"], "BAD_REQUEST")

    def test_wrong_method_and_unknown_path(self):
        response, payload = self.json_request("GET", "/capture", headers={"Origin": ORIGIN})
        self.assertEqual(response.status, 405)
        response, payload = self.json_request("GET", "/nope", headers={"Origin": ORIGIN})
        self.assertEqual(response.status, 404)


class TestBridgeCaptureErrors(unittest.TestCase):
    """Each reader failure stays an honest error — never fake minutiae."""

    def run_capture(self, error):
        server = bridge.serve(FakeReader(capture_error=error), host="127.0.0.1", port=0,
                              allowed_origins=bridge.DEFAULT_ALLOWED_ORIGINS)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("POST", "/capture", body=json.dumps({}),
                         headers={"Content-Type": "application/json", "Origin": ORIGIN})
            response = conn.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            conn.close()
            return payload
        finally:
            server.shutdown()
            server.server_close()

    def test_reader_not_connected(self):
        payload = self.run_capture(bridge.ReaderError(
            "READER_NOT_CONNECTED", "No fingerprint reader detected.",
            status=bridge.STATUS_NOT_CONNECTED))
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"], "READER_NOT_CONNECTED")
        self.assertNotIn("minutiae", payload)

    def test_reader_busy(self):
        payload = self.run_capture(bridge.ReaderError(
            "READER_BUSY", "The fingerprint reader is busy.",
            status=bridge.STATUS_BUSY))
        self.assertFalse(payload["success"])
        self.assertEqual(payload["status"], "busy")

    def test_capture_timeout(self):
        payload = self.run_capture(bridge.ReaderError(
            "READER_CAPTURE_TIMEOUT", "No fingerprint was captured within 30 seconds.",
            status=bridge.STATUS_COMMUNICATION_FAILURE, reader_status=0))
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"], "READER_CAPTURE_TIMEOUT")


class TestBridgeConfiguration(unittest.TestCase):
    def test_default_port_is_8787(self):
        self.assertEqual(bridge.DEFAULT_PORT, 8787)
        self.assertEqual(bridge.DEFAULT_HOST, "127.0.0.1")

    def test_production_origins_allowlisted(self):
        self.assertIn("https://jonehotels.com", bridge.DEFAULT_ALLOWED_ORIGINS)
        self.assertIn("https://www.jonehotels.com", bridge.DEFAULT_ALLOWED_ORIGINS)
        self.assertNotIn("*", bridge.DEFAULT_ALLOWED_ORIGINS)

    def test_capture_does_not_deadlock_when_device_was_never_opened(self):
        # Regression: capture() holds the device lock while _capture_raw()
        # re-enters initialize(). With a plain Lock this blocked forever and
        # the browser reported BRIDGE_TIMEOUT on the very first capture.
        reader = bridge.DigitalPersonaReader()
        reader._load_dd = lambda: True  # skip the Windows-only DLL load
        reader._load_fj = lambda: True
        outcome = {}

        def attempt():
            try:
                outcome["error"] = reader.capture(timeout_ms=1000)
            except bridge.ReaderError as exc:
                outcome["error"] = exc.code

        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive(), "capture deadlocked on the device lock")
        self.assertIn("error", outcome)


def build_template(version, angle_byte, angle_degrees_note=""):
    """Hand-built FMR record: 2 minutiae with known field values."""
    minutiae = b""
    for x, y, mtype, quality in ((100, 200, 1, 90), (300, 400, 2, 80)):
        first = ((mtype & 0x03) << 14) | (x & 0x3FFF)
        second = y & 0x3FFF
        minutiae += struct.pack(">HHBB", first, second, angle_byte, quality)
    view = bytes([1, 0, 80, 2]) + minutiae
    total = 24 + len(view)
    header = b"FMR\x00" + version + struct.pack(">I", total) + struct.pack(">H", 0)
    header += struct.pack(">HHHH", 256, 400, 500, 500) + bytes([1, 0])
    assert len(header) == 24
    return header + view


class TestIso19794Parser(unittest.TestCase):
    def test_iso_2005_template(self):
        points = iso19794_2.parse_template(build_template(b" 20\x00", 45))
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0], {"x": 100, "y": 200, "angle": 90,
                                     "type": "ending", "quality": 90})
        self.assertEqual(points[1]["type"], "bifurcation")
        self.assertEqual(points[1]["x"], 300)

    def test_ansi_378_template_angle_scaling(self):
        # ANSI angle units are 360/256 degrees: byte 64 == 90 degrees.
        points = iso19794_2.parse_template(build_template(b" 30\x00", 64))
        self.assertEqual(points[0]["angle"], 90)

    def test_rejects_garbage(self):
        for bad in (b"", b"not a template at all........", b"FMR\x00 20\x00"):
            with self.assertRaises(iso19794_2.TemplateParseError):
                iso19794_2.parse_template(bad)

    def test_rejects_truncated_minutiae(self):
        template = build_template(b" 20\x00", 45)[:-3]
        with self.assertRaises(iso19794_2.TemplateParseError):
            iso19794_2.parse_template(template)


if __name__ == "__main__":
    unittest.main()
