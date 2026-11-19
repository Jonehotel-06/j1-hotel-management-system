"""Browser end-to-end test: the real biometrics.js against a real bridge.

WHAT THIS PROVES (and what it does NOT)
=======================================
A page served from the PRODUCTION origin (https://jonehotels.com) runs the real
``js/biometrics.js`` and talks to a real ``fingerprint_bridge_service.py``
listening on 127.0.0.1 — i.e. the exact public→loopback hop that Chromium's
Local Network Access now gates. Three scenarios:

  1. permission granted  -> /status, /initialize and /capture succeed and the
                            minutiae come from the bridge (never synthesised);
  2. permission denied   -> the page reports BRIDGE_PERMISSION_BLOCKED, NOT
                            "start the bridge";
  3. no bridge process   -> BRIDGE_UNREACHABLE with the start-the-bridge text.

The bridge here uses an INJECTED FAKE READER. No DigitalPersona SDK and no USB
hardware are involved: this proves the software path and the browser security
model, and nothing about a physical U.are.U 4500. That acceptance test needs the
reception workstation (docs/WINDOWS_11_FINGERPRINT_SETUP.md §5).

    python frontend/tests/fingerprint_bridge_browser_test.py
"""
import asyncio
import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "workstation_bridge"))

BRIDGE_PORT = 8799  # deliberately not 8787, so a bridge already running is untouched
ORIGIN = "https://jonehotels.com"

POINTS = [{"x": 100 + i, "y": 120 + i, "angle": (i * 17) % 360,
           "type": "ending" if i % 2 == 0 else "bifurcation", "quality": 88}
          for i in range(16)]


class FakeReader:
    """A stand-in for DigitalPersonaReader — NEVER a hardware claim."""

    def __init__(self):
        self.connected = True
        self.captures = 0

    def status(self):
        return {
            "status": "connected" if self.connected else "not_connected",
            "connected": self.connected,
            "detail": "Fingerprint reader connected and ready." if self.connected
                      else "No fingerprint reader detected. Connect the DigitalPersona 4500 reader to this workstation.",
            "device_serial": "FAKE-4500" if self.connected else "",
            "device_product": "U.are.U 4500 (fake reader)",
            "capture_in_progress": False,
            "driver_installed": True,
            "sdk_loaded": True,
            "device_count": 1 if self.connected else 0,
            "last_error": "",
        }

    def initialize(self):
        return self.status()

    def capture(self, timeout_ms=30000, finger_position="RIGHT_INDEX"):
        import fingerprint_bridge_service as bridge
        if not self.connected:
            raise bridge.ReaderError("READER_NOT_CONNECTED", "No fingerprint reader detected.",
                                     status=bridge.STATUS_NOT_CONNECTED)
        self.captures += 1
        return {"success": True, "minutiae": POINTS, "finger_position": finger_position,
                "count": len(POINTS), "device_serial": "FAKE-4500", "duration_ms": 12,
                "format": "ISO_19794_2_2005"}

    def diagnose(self):
        return {"status": self.status()}


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>bridge probe</title></head>
<body><script src="/js/biometrics.js"></script></body></html>"""


async def run():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("SKIP: playwright is not installed")
        return 2

    import fingerprint_bridge_service as bridge

    reader = FakeReader()
    server = bridge.serve(reader, host="127.0.0.1", port=BRIDGE_PORT,
                          allowed_origins=(ORIGIN,))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    results = {}

    def check(name, condition, detail=""):
        results[name] = bool(condition)
        print(("PASS " if condition else "FAIL ") + name + (("  — " + detail) if detail and not condition else ""))

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            # ---------------------------------------------------- 1. granted
            ctx = await browser.new_context()
            page = await ctx.new_page()
            errors = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            await ctx.grant_permissions(["local-network-access"], origin=ORIGIN)
            async def serve_page(route):
                await route.fulfill(status=200, content_type="text/html", body=PAGE)
            await page.route(ORIGIN + "/probe", serve_page)
            await page.route(ORIGIN + "/js/biometrics.js", lambda route: asyncio.ensure_future(
                route.fulfill(status=200, content_type="application/javascript",
                              body=(ROOT / "frontend" / "js" / "biometrics.js").read_bytes())))
            await page.goto(ORIGIN + "/probe")
            await page.evaluate("(port) => { window.APP_CONFIG = window.APP_CONFIG || {}; "
                                "window.APP_CONFIG.FINGERPRINT_BRIDGE_URL = 'http://127.0.0.1:' + port; }",
                                BRIDGE_PORT)

            report = await page.evaluate("() => window.JONE.biometrics.describeReader()")
            check("granted: reader reported ready", report["ready"] is True, json.dumps(report)[:200])
            check("granted: status is clearly from the bridge", report["source"] == "bridge")
            check("granted: device serial surfaced", report["device_serial"] == "FAKE-4500")

            probe = await page.evaluate("() => window.JONE.biometrics.captureMinutiaeProbe({})")
            check("granted: capture returned minutiae from the bridge", len(probe) == len(POINTS),
                  "got %s points" % len(probe))
            check("granted: the bridge actually captured once", reader.captures == 1,
                  "capture calls=%s" % reader.captures)
            check("granted: no CORS/LNA error in the console",
                  not any("loopback" in e for e in errors), "; ".join(errors[:2]))

            # ------------------------------------------------------ 2. denied
            ctx2 = await browser.new_context()
            page2 = await ctx2.new_page()
            await page2.route(ORIGIN + "/probe", lambda route: asyncio.ensure_future(
                route.fulfill(status=200, content_type="text/html", body=PAGE)))
            await page2.route(ORIGIN + "/js/biometrics.js", lambda route: asyncio.ensure_future(
                route.fulfill(status=200, content_type="application/javascript",
                              body=(ROOT / "frontend" / "js" / "biometrics.js").read_bytes())))
            await page2.goto(ORIGIN + "/probe")
            await page2.evaluate("(port) => { window.APP_CONFIG = window.APP_CONFIG || {}; "
                                 "window.APP_CONFIG.FINGERPRINT_BRIDGE_URL = 'http://127.0.0.1:' + port; }",
                                 BRIDGE_PORT)
            # Simulate the operator dismissing the prompt once and forever.
            await page2.evaluate("""() => {
                navigator.permissions.query = async () => ({ state: 'denied' });
            }""")
            denied = await page2.evaluate("() => window.JONE.biometrics.describeReader()")
            check("denied: classified as a browser block, not a dead bridge",
                  denied["status"] == "permission_blocked", json.dumps(denied)[:220])
            check("denied: tells the operator about local network access",
                  "local network access" in denied["detail"].lower())
            check("denied: does NOT tell them to start the bridge",
                  "Start the J-ONE Fingerprint Bridge" not in denied["detail"])
            try:
                await page2.evaluate("() => window.JONE.biometrics.captureMinutiaeProbe({})")
                check("denied: capture refused", False, "capture unexpectedly succeeded")
            except Exception as exc:  # noqa: BLE001 - the message is what matters
                check("denied: capture refused with BRIDGE_PERMISSION_BLOCKED",
                      "BRIDGE_PERMISSION_BLOCKED" in str(exc) or "permission" in str(exc).lower(),
                      str(exc)[:200])

            # --------------------------------------------------- 3. no bridge
            # A fresh context: the denial remembered in step 2 is a per-page
            # state by design, and this scenario is a bridge that is simply not
            # started.
            ctx3 = await browser.new_context()
            page3 = await ctx3.new_page()
            await ctx3.grant_permissions(["local-network-access"], origin=ORIGIN)
            await page3.route(ORIGIN + "/probe", lambda route: asyncio.ensure_future(
                route.fulfill(status=200, content_type="text/html", body=PAGE)))
            await page3.route(ORIGIN + "/js/biometrics.js", lambda route: asyncio.ensure_future(
                route.fulfill(status=200, content_type="application/javascript",
                              body=(ROOT / "frontend" / "js" / "biometrics.js").read_bytes())))
            await page3.goto(ORIGIN + "/probe")
            await page3.evaluate("""() => {
                window.APP_CONFIG = window.APP_CONFIG || {};
                window.APP_CONFIG.FINGERPRINT_BRIDGE_URL = 'http://127.0.0.1:1';
            }""")
            down = await page3.evaluate("() => window.JONE.biometrics.describeReader()")
            check("no bridge: reported unreachable", down["code"] == "BRIDGE_UNREACHABLE", json.dumps(down)[:200])
            check("no bridge: start-the-bridge guidance kept",
                  "Start the J-ONE Fingerprint Bridge" in down["detail"])
        finally:
            await browser.close()
            server.shutdown()
            server.server_close()

    failed = [name for name, value in results.items() if not value]
    print("\n%d/%d checks passed" % (len(results) - len(failed), len(results)))
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    print("PASS: browser ↔ local bridge transport verified (fake reader — no hardware claim)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
