"""Browser test: the TV gallery admin page stays usable on a phone.

WHY THIS EXISTS
===============
Production 2026-11-20, third report: "The admin page is not responsive on
mobile."

It was not a layout fault — the page fits 320/360/390/414 px with no horizontal
scroll (measured, and still asserted here). It was ``load()``:

    var data = await Promise.all([rooms, media, settings]);
    $('login-panel').hidden = true; $('admin').hidden = false;

``Promise.all`` makes the whole panel hostage to its slowest member. On a phone,
one of those three parallel requests is far likelier to be dropped, and when one
failed the page never left the sign-in form: no panel, no spinner, no reason on
screen. To a manager holding a phone that is a frozen page. A request that never
answered at all froze it permanently, because nothing capped the wait.

This test drives the REAL admin.html + admin.js in a real phone viewport against
a local API (mocked — no production data is touched) and proves:

  1. no horizontal overflow at four phone widths, before and after sign-in;
  2. if ONE of the three collections fails, the panel still opens and the
     failure is named (the exact production symptom);
  3. a request that stalls does not freeze the page: taps keep working.

    python frontend/tests/tv_gallery_admin_mobile_test.py

Exits non-zero on failure. Requires Python Playwright + Chromium.
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1]

# Which collection the fake API should refuse, and whether to stall instead.
STATE = {"fail": "", "stall": False}

MEDIA = {"success": True, "data": [
    {"id": 1, "name": "lobby.jpg", "kind": "image", "room_id": 1, "order": 0, "active": True,
     "is_cover": True, "processing_status": "ready", "processing_error": ""},
    {"id": 9, "name": "pool.jpg", "kind": "image", "room_id": None, "order": 1, "active": True,
     "is_cover": False, "processing_status": "ready", "processing_error": ""},
]}
ROOMS = {"success": True, "data": [
    {"id": 1, "number": "101", "type": "Deluxe", "eligible": True, "active": True}]}
SETTINGS = {"success": True, "data": {
    "slideshow_seconds": 3, "max_upload_mb": 50, "music_volume": 40, "music_id": None,
    "music_enabled": False, "music_autoplay": False}}

CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml",
                 ".webmanifest": "application/manifest+json"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser navigated away mid-response (stall scenario)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = json.dumps({"success": True, "data": {"tokens": {"access": "test-token", "refresh": "r"}}})
        return self._send(200, body.encode(), "application/json")

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]

        if path.startswith("/api/auth/login/"):
            return self.do_POST()

        if path.startswith("/api/rooms-gallery/manage/"):
            tail = path.split("/manage/", 1)[-1].strip("/").split("/", 1)[0]
            section = tail if tail in ("rooms", "media", "settings") else "settings"
            if STATE["fail"] == section:
                return self._send(503, json.dumps(
                    {"success": False, "message": "Service unavailable"}).encode(), "application/json")
            if STATE["stall"]:
                time.sleep(25)
            payload = {"rooms": ROOMS, "media": MEDIA, "settings": SETTINGS}[section]
            return self._send(200, json.dumps(payload).encode(), "application/json")

        if path.startswith("/api/rooms-gallery/"):
            return self._send(200, json.dumps({"success": True, "data": {}}).encode(), "application/json")

        if path.rstrip("/") == "/rooms-gallery/admin":
            path = "/rooms-gallery/admin.html"
        target = (FRONTEND / path.lstrip("/")).resolve()
        if not str(target).startswith(str(FRONTEND)) or not target.is_file():
            return self._send(404, b"not found", "text/plain")
        return self._send(200, target.read_bytes(),
                          CONTENT_TYPES.get(target.suffix, "application/octet-stream"))


PHONE_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36")


def measure(page):
    return page.evaluate("""() => ({
        vw: window.innerWidth,
        scrollWidth: document.documentElement.scrollWidth,
        adminHidden: document.getElementById('admin').hidden,
        loginHidden: document.getElementById('login-panel').hidden,
        message: (document.getElementById('message') || {}).textContent || '',
        rows: document.querySelectorAll('#media-list tr').length
    })""")


def sign_in(page):
    page.fill("#email", "manager@example.com")
    page.fill("#password", "x")
    page.click("#login button")


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright is not installed (pip install playwright && playwright install chromium)")
        return 2

    class Quiet(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            # A phone that navigates away mid-response is normal here (the stall
            # scenario); it is not a test failure and must not spam the output.
            pass

    server = Quiet(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    failures = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            # ---- 1. layout at every phone width, signed out and signed in ----
            for width in (320, 360, 390, 414):
                STATE["fail"] = ""
                context = browser.new_context(viewport={"width": width, "height": 780},
                                              is_mobile=True, has_touch=True, user_agent=PHONE_UA)
                page = context.new_page()
                page.goto(base + "/rooms-gallery/admin", wait_until="load")
                before = measure(page)
                if before["scrollWidth"] > before["vw"] + 1:
                    failures.append("%dpx: page scrolls horizontally before sign-in (%s > %s)"
                                    % (width, before["scrollWidth"], before["vw"]))
                sign_in(page)
                try:
                    page.wait_for_selector("#admin:not([hidden])", timeout=8000)
                except Exception:
                    failures.append("%dpx: the admin panel never opened after sign-in" % width)
                after = measure(page)
                if after["scrollWidth"] > after["vw"] + 1:
                    failures.append("%dpx: page scrolls horizontally after sign-in (%s > %s)"
                                    % (width, after["scrollWidth"], after["vw"]))
                context.close()

            # ---- 2. ONE failed collection must not lock the manager out ----
            for section in ("rooms", "media", "settings"):
                STATE["fail"] = section
                context = browser.new_context(viewport={"width": 390, "height": 844},
                                              is_mobile=True, has_touch=True, user_agent=PHONE_UA)
                page = context.new_page()
                page.goto(base + "/rooms-gallery/admin", wait_until="load")
                sign_in(page)
                try:
                    page.wait_for_selector("#admin:not([hidden])", timeout=8000)
                except Exception:
                    failures.append(
                        "%s failed: the page stayed on the sign-in form — this is the "
                        "'admin page is not responsive' report" % section)
                    context.close()
                    continue
                try:
                    page.wait_for_function(
                        "() => (document.getElementById('message') || {}).textContent.length > 0",
                        timeout=8000)
                except Exception:
                    pass
                state = measure(page)
                if section not in state["message"]:
                    failures.append("%s failed: the panel opened but the failure was not named "
                                    "(message was %r)" % (section, state["message"]))
                # The page must still be operable, not just visible.
                page.click("#refresh-media", timeout=5000)
                page.wait_for_timeout(400)
                context.close()

            # ---- 3. a stalled request must not freeze the page ----
            STATE["fail"] = ""
            STATE["stall"] = True
            context = browser.new_context(viewport={"width": 390, "height": 844},
                                          is_mobile=True, has_touch=True, user_agent=PHONE_UA)
            page = context.new_page()
            page.goto(base + "/rooms-gallery/admin", wait_until="load")
            sign_in(page)
            page.wait_for_timeout(1200)  # requests are now stalled server-side
            latency = page.evaluate("""async () => {
                const t = performance.now();
                await new Promise(r => requestAnimationFrame(() => setTimeout(r, 0)));
                return performance.now() - t;
            }""")
            if latency > 250:
                failures.append("the main thread is blocked while a request stalls (%.0f ms)" % latency)
            try:
                page.click("#refresh-media", timeout=5000)
            except Exception:
                failures.append("controls are unusable while a request stalls (the frozen-page report)")
            STATE["stall"] = False
            context.close()

            browser.close()
    finally:
        server.shutdown()

    if failures:
        print("FAIL:")
        for item in failures:
            print(" - " + item)
        return 1
    print("PASS: the admin page is usable at 320/360/390/414 px, survives a failed "
          "collection and is not frozen by a stalled request")
    return 0


if __name__ == "__main__":
    sys.exit(main())
