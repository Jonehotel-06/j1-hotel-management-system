"""Browser integration test: the desk page <-> workstation fingerprint bridge.

WHAT IS EXERCISED
=================
The REAL ``workstation_bridge/fingerprint_bridge_service.py`` HTTP layer (its
own CORS/OPTIONS handling, /health, /status, /capture and error envelopes) is
started on loopback, and the REAL ``frontend/js/biometrics.js`` transport calls
it from a real Chromium page. The reader itself is a stub object injected into
the handler, because there is no DigitalPersona 4500 in this environment — the
SDK/device half of the chain is therefore NOT tested here and is reported as
such (see reports/test_report.md).

Asserted:
  1. the OPTIONS preflight and the actual POST both carry the allowlisted
     origin's grant (this is the request the client saw fail);
  2. ``describeReader()`` reports ``connected`` when the reader answers;
  3. a capture returns the minutiae list the API validates;
  4. a reader that is unplugged, busy, or fails to extract a template produces
     FOUR DIFFERENT, actionable errors — not one "bridge not reachable";
  5. with the bridge stopped, the page says the bridge is unreachable and does
     not pretend a capture happened;
  6. a page whose origin is not allowlisted gets no CORS grant (the bridge is
     not opened up to arbitrary sites).

    python frontend/tests/fingerprint_station_browser_test.py

Exits non-zero on failure. Requires Python Playwright + Chromium. The bridge
module itself is pure standard library.
"""
import json
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
sys.path.insert(0, str(REPO / "workstation_bridge"))

import fingerprint_bridge_service as bridge  # noqa: E402

POINTS = [{"x": 40 + i, "y": 60 + i, "angle": (i * 7) % 360,
           "type": "ending" if i % 2 else "bifurcation", "quality": 90} for i in range(14)]


class StubReader:
    """A reader whose state the test drives: 'ready' | 'unplugged' | 'busy' | 'no_template'."""

    def __init__(self):
        self.mode = "ready"
        self.captures = 0

    def status(self):
        if self.mode == "unplugged":
            return {"status": "not_connected", "connected": False,
                    "detail": bridge.STATUS_DETAILS["not_connected"], "device_serial": "",
                    "device_product": "", "capture_in_progress": False,
                    "driver_installed": True, "sdk_loaded": True, "device_count": 0,
                    "last_error": ""}
        if self.mode == "busy":
            return {"status": "busy", "connected": True, "captured_state": "connected",
                    "detail": bridge.STATUS_DETAILS["busy"], "device_serial": "STUB-4500",
                    "device_product": "U.are.U 4500", "capture_in_progress": True,
                    "driver_installed": True, "sdk_loaded": True, "device_count": 1,
                    "last_error": ""}
        return {"status": "connected", "connected": True,
                "detail": bridge.STATUS_DETAILS["connected"], "device_serial": "STUB-4500",
                "device_product": "U.are.U 4500", "capture_in_progress": False,
                "driver_installed": True, "sdk_loaded": True, "device_count": 1,
                "last_error": ""}

    def initialize(self):
        return self.status()

    def diagnose(self):
        return {"service": "stub", "status": self.status()}

    def shutdown(self):
        pass

    def capture(self, timeout_ms=30000, finger_position="RIGHT_INDEX"):
        if self.mode == "unplugged":
            raise bridge.ReaderError("READER_NOT_CONNECTED",
                                     bridge.STATUS_DETAILS["not_connected"],
                                     status="not_connected")
        if self.mode == "busy":
            raise bridge.ReaderError("READER_BUSY", bridge.STATUS_DETAILS["busy"], status="busy")
        if self.mode == "no_template":
            raise bridge.ReaderError("TEMPLATE_EXTRACTION_FAILED",
                                     "The reader captured a finger but the SDK could not produce "
                                     "usable minutiae.", status="communication_failure")
        if self.mode == "timeout":
            raise bridge.ReaderError("READER_CAPTURE_TIMEOUT",
                                     "No fingerprint was captured within 30 seconds. Place the "
                                     "finger flat on the sensor and try again.",
                                     status="communication_failure")
        self.captures += 1
        return {"success": True, "minutiae": POINTS, "finger_position": finger_position,
                "count": len(POINTS), "device_serial": "STUB-4500", "duration_ms": 120,
                "format": "ISO_19794_2_2005"}


READER = StubReader()
SERVERS = {}


def start_bridge(origins):
    """Start the real bridge HTTP layer on a free loopback port."""
    handler = bridge.make_handler(READER, origins)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


class PageServer:
    """Serves the frontend files plus a small harness page for the test.

    The harness reads the bridge origin from ``?bridge=`` so the page server can
    be started first (its port is what the bridge must allowlist) and no
    restarts are needed.
    """

    def start(self):
        # config.js OVERWRITES window.__APP_CONFIG__ with the resolved
        # application config, so the bridge origin has to arrive through the
        # deployment-owned source that config.js reads: js/runtime-config.js.
        # The page server therefore serves a test-authored runtime-config.js
        # (no repository file is modified).
        self.runtime_config = (
            'window.__APP_CONFIG__ = Object.assign({}, window.__APP_CONFIG__, {'
            ' FINGERPRINT_BRIDGE_URL: new URLSearchParams(location.search).get("bridge") || "" });'
        ).encode()
        harness = """<!doctype html><html><head><meta charset="utf-8">
<title>fingerprint station harness</title></head><body>
<script src="/js/runtime-config.js"></script>
<script src="/js/utils.js"></script>
<script src="/js/config.js"></script>
<script src="/js/biometrics.js"></script>
</body></html>""".encode()

        import http.server as http_server

        class Handler(http_server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path in ("/", "/harness.html"):
                    body, ctype = harness, "text/html; charset=utf-8"
                elif path == "/js/runtime-config.js":
                    body, ctype = self.server.runtime_config, "application/javascript; charset=utf-8"
                else:
                    target = (FRONTEND / path.lstrip("/")).resolve()
                    if not str(target).startswith(str(FRONTEND)) or not target.is_file():
                        self.send_response(404)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    body = target.read_bytes()
                    ctype = ("application/javascript; charset=utf-8"
                             if target.suffix == ".js" else "text/plain")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.runtime_config = self.runtime_config
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, server.server_address[1]


def preflight(url, origin):
    """Send the exact preflight the browser sends, from Python, to inspect headers."""
    import http.client
    from urllib.parse import urlparse
    parsed = urlparse(url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    conn.request("OPTIONS", parsed.path, headers={
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
        "Access-Control-Request-Private-Network": "true",
    })
    response = conn.getresponse()
    headers = {k.lower(): v for k, v in response.getheaders()}
    conn.close()
    return response.status, headers


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright is not installed (pip install playwright && playwright install chromium)")
        return 2

    failures = []

    def check(condition, message):
        if not condition:
            failures.append(message)

    # ---- the page first (its port is the origin the bridge must allowlist) --
    page_server, page_port = PageServer().start()
    page_origin = "http://127.0.0.1:%d" % page_port
    bridge_server, bridge_port = start_bridge(
        [page_origin, "https://jonehotels.com", "https://www.jonehotels.com"])
    bridge_url = "http://127.0.0.1:%d" % bridge_port
    SERVERS["bridge"] = (bridge_server, bridge_port)
    SERVERS["page"] = (page_server, page_port)

    try:
        # ---- 1. the CORS preflight the client reported ----------------------
        status, headers = preflight(bridge_url + "/capture", page_origin)
        check(status in (200, 204), "the bridge did not answer the OPTIONS preflight (got %s)" % status)
        check(headers.get("access-control-allow-origin") == page_origin,
              "the preflight did not grant this origin (got %r)" % headers.get("access-control-allow-origin"))
        check("POST" in (headers.get("access-control-allow-methods") or ""),
              "the preflight does not allow POST")
        check("content-type" in (headers.get("access-control-allow-headers") or "").lower(),
              "the preflight does not allow the Content-Type header the capture sends")
        check(headers.get("vary", "").lower() == "origin", "the preflight does not Vary: Origin")

        denied_status, denied_headers = preflight(bridge_url + "/capture", "https://evil.example")
        check(denied_status == 403, "an unknown origin's preflight was not refused (got %s)" % denied_status)
        check("access-control-allow-origin" not in denied_headers,
              "an unknown origin was granted CORS access to the reader")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            def new_page():
                context = browser.new_context()
                page = context.new_page()
                page.goto(page_origin + "/harness.html?bridge=" + bridge_url, wait_until="load")
                return context, page

            # ---- 2. reader ready -------------------------------------------
            READER.mode = "ready"
            context, page = new_page()
            report = page.evaluate("() => window.JONE.biometrics.describeReader({initialize:true})")
            check(report["status"] == "connected" and report["ready"] is True,
                  "a connected reader was not reported as ready: %r" % (report.get("status"),))
            check(report["bridge_reachable"] is True, "the bridge was reported unreachable while running")
            check(report["device_serial"] == "STUB-4500", "the reader serial was not reported")

            # ---- 3. capture returns the minutiae the API validates ----------
            captured = page.evaluate("() => window.JONE.biometrics.captureMinutiaeProbe({})")
            check(isinstance(captured, list) and len(captured) == len(POINTS),
                  "the bridge capture did not produce the minutiae list (%r)" % (captured,))
            check(READER.captures == 1, "exactly one capture should have happened")

            # ---- 4. four different failures, four different messages --------
            messages = {}
            for mode in ("unplugged", "busy", "no_template", "timeout"):
                READER.mode = mode
                outcome = page.evaluate("""async () => {
                    try { await window.JONE.biometrics.captureMinutiaeProbe({timeout: 5000}); return {ok: true}; }
                    catch (err) { return {ok: false, code: err.code || '', message: err.message || ''}; }
                }""")
                check(outcome["ok"] is False, "a failed capture (%s) reported success" % mode)
                messages[mode] = (outcome["code"], outcome["message"])
            check(messages["unplugged"][0] == "READER_NOT_CONNECTED",
                  "an unplugged reader produced %r" % (messages["unplugged"][0],))
            check(messages["busy"][0] == "READER_BUSY", "a busy reader produced %r" % (messages["busy"][0],))
            check(messages["no_template"][0] == "TEMPLATE_EXTRACTION_FAILED",
                  "a template failure produced %r" % (messages["no_template"][0],))
            check(messages["timeout"][0] == "READER_CAPTURE_TIMEOUT",
                  "a capture timeout produced %r" % (messages["timeout"][0],))
            check(len({m[1] for m in messages.values()}) == 4,
                  "two different reader failures produced the same message: %r" % (messages,))
            context.close()

            # ---- 5. the bridge stopped --------------------------------------
            READER.mode = "ready"
            stopped = bridge_server.server_address[1]
            bridge_server.shutdown()
            bridge_server.server_close()
            context, page = new_page()
            report = page.evaluate("() => window.JONE.biometrics.describeReader({initialize:true})")
            check(report["bridge_reachable"] is False,
                  "a stopped bridge was reported as reachable")
            check(report["ready"] is False, "a stopped bridge reported a ready reader")
            check("bridge" in (report["detail"] or "").lower(),
                  "the message for a stopped bridge does not mention the bridge: %r" % report["detail"])
            capture = page.evaluate("""async () => {
                try { await window.JONE.biometrics.captureMinutiaeProbe({timeout: 3000}); return {ok: true}; }
                catch (err) { return {ok: false, code: err.code || '', message: err.message || ''}; }
            }""")
            check(capture["ok"] is False, "a capture 'succeeded' with no bridge running")
            check(capture["code"] not in ("", None), "a capture with no bridge produced no error code")
            context.close()
            browser.close()
    finally:
        for name in ("bridge", "page"):
            if name in SERVERS:
                try:
                    SERVERS[name][0].shutdown()
                except Exception:
                    pass

    if failures:
        print("FAIL:")
        for item in failures:
            print(" - " + item)
        return 1
    print("PASS: the real bridge answered the OPTIONS preflight and the capture POST for the "
          "allowlisted origin, refused an unknown origin, and the desk page reported reader-ready, "
          "capture, four distinct reader failures and a stopped bridge with four distinct messages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
