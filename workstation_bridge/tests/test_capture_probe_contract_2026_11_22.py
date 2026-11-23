"""Probe-contract repair tests (2026-11-22) — the /capture timeout gate.

Root cause being pinned here
============================
The browser's reachability probe (frontend/js/biometrics.js
probeBridgeViaPreflight) sends POST /capture with timeout_ms: 1 to keep the
probe instant. The service used to clamp ANY submitted timeout to its 30 s
default, so every probe drove a REAL 5-second capture on the reception reader
(LED flash, sensor busy, finger requested) just to answer "is the bridge up".
The repair (fingerprint_bridge_service.py, service 2.1.0) rejects an EXPLICIT
timeout_ms below 5000 with 400 INVALID_CAPTURE_TIMEOUT BEFORE any device
access, while the missing-timeout default (30 s) and in-range values behave
exactly as before.

No Windows, SDK or USB reader required: a fake reader counts capture calls so
the tests prove the rejection happens BEFORE the device is touched.
"""
import http.client
import json
import sys
import threading
import unittest
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))

import fingerprint_bridge_service as bridge  # noqa: E402
from test_fingerprint_bridge_service import FakeReader, ORIGIN  # noqa: E402


class ProbeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reader = FakeReader()
        cls.server = bridge.serve(cls.reader, host="127.0.0.1", port=0,
                                  allowed_origins=bridge.DEFAULT_ALLOWED_ORIGINS)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def post_capture(self, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/capture", body=json.dumps(body),
                     headers={"Content-Type": "application/json", "Origin": ORIGIN})
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response, json.loads(raw.decode("utf-8")) if raw else {}

    def setUp(self):
        # One reader serves the whole class (the server is built once); the
        # capture counter is per-test so "never touched" assertions are exact.
        self.reader.capture_calls = 0

    def test_explicit_too_small_timeout_is_rejected_before_device_access(self):
        # Exactly the probe the browser sends (timeout_ms: 1): it must be a
        # client error, it must name the contract, and the reader must never
        # be touched — no LED flash, no sensor busy window, no finger prompt.
        response, payload = self.post_capture({"timeout_ms": 1})
        self.assertEqual(response.status, 400)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"], "INVALID_CAPTURE_TIMEOUT")
        self.assertEqual(payload["min_timeout_ms"], 5000)
        self.assertEqual(payload["max_timeout_ms"], 60000)
        self.assertEqual(self.reader.capture_calls, 0)

    def test_explicit_zero_and_negative_timeouts_are_rejected(self):
        for timeout in (0, -1, -30000):
            response, payload = self.post_capture({"timeout_ms": timeout})
            self.assertEqual(response.status, 400, "timeout_ms=%r" % timeout)
            self.assertEqual(payload["error"], "INVALID_CAPTURE_TIMEOUT")
        self.assertEqual(self.reader.capture_calls, 0)

    def test_missing_timeout_still_defaults_to_30s_and_captures(self):
        # The documented default is unchanged: capture without timeout_ms keeps
        # the 30-second default (clamped to the service maximum).
        response, payload = self.post_capture({})
        self.assertEqual(response.status, 200)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["format"], "ISO_19794_2_2005")
        self.assertEqual(self.reader.capture_calls, 1)

    def test_in_range_timeout_still_captures(self):
        response, payload = self.post_capture({"timeout_ms": 30000})
        self.assertEqual(response.status, 200)
        self.assertTrue(payload["success"])
        self.assertEqual(self.reader.capture_calls, 1)

    def test_service_version_is_210(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/health", headers={"Origin": ORIGIN})
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["version"], "2.1.0")


if __name__ == "__main__":
    unittest.main()
