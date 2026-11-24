"""Regression tests: "the bridge is running but says so differently now".

Two defects fixed on 2026-11-22 that are easy to reintroduce:

1. ``dpfpdd_init()`` failing (DigitalPersona service stopped) was reported as
   ``driver_missing`` — technicians then reinstalled a driver that was already
   installed. It must be ``service_unavailable``, with ``driver_installed`` true.

2. The desk page could not tell "the operator has not allowed local network
   access yet" from "the bridge is not running", so it printed the
   start-the-bridge sentence for both.

``frontend/js/biometrics.js``:
  * a ``SecurityError`` while reading the permission state means "not asked yet"
    (``prompt``), never ``unsupported`` and never ``denied``;
  * when a status read is refused, ``probeBridgeViaPreflight()`` asks the bridge
    with a deliberately invalid ``/capture`` body. A reachable bridge (any HTTP
    answer) must change the verdict; a failed probe must leave it exactly as it
    was.
"""
from __future__ import annotations

from unittest import mock

import ctypes
import importlib.util
import json
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
BRIDGE_DIR = REPO / "workstation_bridge"
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

import fingerprint_bridge_service as bridge  # noqa: E402


class InitFailureIsNotDriverMissing(unittest.TestCase):
    """The vendor DLL loaded; its init call failed."""

    def setUp(self):
        self.reader = bridge.DigitalPersonaReader()
        patcher = mock.patch.object(bridge.platform, "system", return_value="Windows")
        patcher.start()
        self.addCleanup(patcher.stop)
        # Pretend the DLL was found and loaded, and that dpfpdd_init() returned
        # DPFPDD_E_DEVICE_FAILURE (the code recorded when the DigitalPersona
        # service is stopped).
        self.reader._dd = ctypes.CDLL(None)
        self.reader._dd_path = pathlib.Path(r"C:\Program Files\DigitalPersona\Bin\dpfpdd.dll")
        self.reader._init_failed = True
        self.reader._init_rc = 0x05BA0005
        self.reader._last_error = "dpfpdd_init returned 0x05BA0005."
        self.reader._load_dd = lambda: False   # the same branch _load_dd takes
        # Keep the SDK half out of the way: this test is about the driver init.
        self.reader._load_fj = lambda: True

    def test_status_reports_service_unavailable(self):
        state = self.reader.status()
        self.assertEqual(state["status"], bridge.STATUS_SERVICE_UNAVAILABLE)
        self.assertTrue(state["driver_installed"], "the driver IS installed")
        self.assertEqual(int(state["init_status"], 16), 0x05BA0005)
        self.assertIn("service", state["detail"].lower())

    def test_status_is_not_reported_as_driver_missing(self):
        self.assertNotEqual(self.reader.status()["status"], bridge.STATUS_DRIVER_MISSING)

    def test_the_status_vocabulary_is_documented(self):
        self.assertIn(bridge.STATUS_SERVICE_UNAVAILABLE, bridge.STATUS_DETAILS)
        self.assertTrue(bridge.STATUS_DETAILS[bridge.STATUS_SERVICE_UNAVAILABLE].strip())

    def test_capture_reports_the_service_fault_and_is_retryable(self):
        with self.assertRaises(bridge.ReaderError) as caught:
            self.reader.capture(timeout_ms=5000)
        self.assertEqual(caught.exception.code, "DRIVER_SERVICE_UNAVAILABLE")
        self.assertEqual(caught.exception.status, bridge.STATUS_SERVICE_UNAVAILABLE)
        self.assertTrue(caught.exception.retry, "recoverable once the service starts")


BIOMETRICS_JS = REPO / "frontend" / "js" / "biometrics.js"


def node_available() -> bool:
    import shutil
    return bool(shutil.which("node"))


class FrontendClassification(unittest.TestCase):
    """Drive the real biometrics.js in Node with a stubbed browser."""

    def _run(self, script: str):
        import subprocess
        prelude = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BIOMETRICS_JS))}, 'utf8');
const listeners = {{}};
function res() {{ return {{ okLabel: '' }}; }}
const state = {{ permission: 'prompt', throwSecurity: false, fetchImpl: null, calls: [] }};
const window = {{
  location: {{ hostname: 'jonehotels.com', protocol: 'https:', href: 'https://jonehotels.com/dashboard/workforce.html' }},
  navigator: {{
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/141 Safari/537.36',
    onLine: true,
    permissions: {{
      query: async () => {{
        state.calls.push('permissions.query');
        if (state.throwSecurity) {{ const e = new Error('SecurityError'); e.name = 'SecurityError'; throw e; }}
        return {{ state: state.permission }};
      }},
      request: async () => ({{ state: state.permission }})
    }}
  }},
  isSecureContext: true,
  sessionStorage: {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }},
  APP_CONFIG: {{ FINGERPRINT_BRIDGE_URL: 'http://127.0.0.1:8787' }},
  fetch: async (url, options) => state.fetchImpl(url, options),
  setTimeout, clearTimeout, AbortController, console,
  Promise, Array, Object, String, Number, Math, JSON, Error, isFinite, RegExp
}};
window.window = window;
const vm = require('vm');
const context = vm.createContext(window);
context.window = window;
vm.runInContext(source, context);
"""
        full = prelude + script
        result = subprocess.run(["node", "-e", full], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    @unittest.skipUnless(node_available(), "node is not installed")
    def test_security_error_means_not_asked_yet(self):
        out = self._run("""
(async () => {
  state.throwSecurity = true;
  const value = await window.JONE.biometrics.localNetworkPermissionState();
  console.log(JSON.stringify({ value }));
})();
""")
        self.assertEqual(out["value"], "prompt",
                         "a SecurityError must not be reported as unsupported/denied")

    @unittest.skipUnless(node_available(), "node is not installed")
    def test_a_reachable_bridge_is_never_reported_as_missing(self):
        out = self._run("""
(async () => {
  // The browser reports the grant as given, yet the status GET is refused at
  // the network level: the ambiguous case the POST probe exists for.
  state.permission = 'granted';
  state.fetchImpl = async (url, options) => {
    if (options && options.method === 'POST') {
      return { status: 400, ok: false, text: async () => JSON.stringify({ success: false, error: 'BAD_REQUEST' }) };
    }
    throw new TypeError('Failed to fetch');
  };
  const report = await window.JONE.biometrics.describeReader({ initialize: false });
  console.log(JSON.stringify({ report }));
})();
""")
        report = out["report"]
        self.assertTrue(report["bridge_reachable"], "a bridge that answered must be reachable")
        self.assertNotIn("bridge is not reachable", report["detail"].lower())
        self.assertFalse(report["ready"], "reachable is not the same as a ready reader")

    @unittest.skipUnless(node_available(), "node is not installed")
    def test_a_page_that_was_never_granted_access_is_not_told_to_start_the_bridge(self):
        out = self._run("""
(async () => {
  state.permission = 'prompt';           // the browser has not asked yet
  state.fetchImpl = async () => { throw new TypeError('Failed to fetch'); };
  const report = await window.JONE.biometrics.describeReader({ initialize: false });
  console.log(JSON.stringify({ report }));
})();
""")
        detail = out["report"]["detail"]
        self.assertIn("has not yet been given permission", detail)
        self.assertNotIn("is not reachable at", detail,
                         "a page that was never granted access must not be told the bridge is missing")
        self.assertEqual(out["report"]["status"], "permission_blocked")

    @unittest.skipUnless(node_available(), "node is not installed")
    def test_a_failed_probe_changes_nothing(self):
        out = self._run("""
(async () => {
  state.permission = 'denied';
  state.fetchImpl = async () => { throw new TypeError('Failed to fetch'); };
  const report = await window.JONE.biometrics.describeReader({ initialize: false });
  console.log(JSON.stringify({ report }));
})();
""")
        report = out["report"]
        self.assertFalse(report["bridge_reachable"])
        self.assertIn("bridge", (report["detail"] or "").lower())

    @unittest.skipUnless(node_available(), "node is not installed")
    def test_the_probe_never_captures(self):
        out = self._run("""
(async () => {
  const seen = [];
  state.permission = 'granted';
  state.fetchImpl = async (url, options) => {
    seen.push({ url, method: options && options.method, body: options && options.body });
    if (options && options.method === 'POST') {
      return { status: 400, ok: false, text: async () => '{}' };
    }
    throw new TypeError('Failed to fetch');
  };
  await window.JONE.biometrics.getReaderStatus({ retries: 0 });
  console.log(JSON.stringify({ seen }));
})();
""")
        seen = out["seen"]
        self.assertEqual(len(seen), 2, seen)
        # The refused status read, then the one probe the browser does allow.
        self.assertTrue(seen[0]["url"].endswith("/status"))
        self.assertTrue(seen[1]["url"].endswith("/capture"))
        self.assertEqual(seen[1]["method"], "POST")
        body = json.loads(seen[1]["body"])
        self.assertLess(body.get("timeout_ms", 999999), 5000,
                        "the probe must be rejected by the bridge before any device access")
        self.assertNotIn("finger_position", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
