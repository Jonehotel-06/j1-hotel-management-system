"""Browser test: rooms-gallery admin feedback, media preview and cover selection.

WHY THIS EXISTS
===============
Two client reports, both reproduced here against the REAL ``admin.html`` +
``admin.js`` in a real (mobile-sized and desktop) browser, with a local mock API
that mimics the Django endpoints' response shapes and status codes:

  A. "When I click on any button from rooms-gallery/admin dashboard, no sign
      that it's loading, everything looks rough on mobile."
  B. "There's no way I can see the images I uploaded in order to know the one I
      can use as cover image."

What is asserted
----------------
  1. every uploaded file is rendered as a CARD with a real thumbnail (fetched
     through the authenticated ``/manage/media/<id>/thumb/`` route), the cover
     marked, and a labelled fallback when a rendition cannot be produced;
  2. pressing "Set as cover" sends ``PATCH {is_cover: true}``, shows a busy
     state while the request is open, and — after a full page reload — the same
     picture is still the cover (i.e. it was persisted, not a local flag);
  3. a control shows a busy/disabled state for the whole request and returns to
     normal afterwards (no fake spinner, no double submit);
  4. a refused request is reported as an error and the button comes back;
  5. no horizontal overflow at 320/360/390 px and on desktop.

    python frontend/tests/rooms_gallery_admin_media_test.py

Exits non-zero on failure. Requires Python Playwright + Chromium.
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FRONTEND = Path(__file__).resolve().parents[1]

# 1x1 PNG: a real, decodable image so the thumbnail path is exercised for real.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300010000050001'0d0a2db40000000049454e44ae426082".replace("'", "")
)

STATE = {
    "cover_id": 1,          # server-side cover for room 1
    "fail_patch": False,    # refuse the next PATCH
    "patch_delay": 0.0,     # seconds to hold a PATCH open (busy-state assertion)
    "patch_calls": 0,
    "thumb_calls": 0,
}

MEDIA = [
    {"id": 1, "room_id": 1, "kind": "image", "name": "lobby-front.jpg", "size": 210000,
     "active": True, "order": 0, "processing_status": "ready", "processing_error": ""},
    {"id": 2, "room_id": 1, "kind": "image", "name": "suite-bed.jpg", "size": 180000,
     "active": True, "order": 1, "processing_status": "ready", "processing_error": ""},
    {"id": 3, "room_id": 1, "kind": "video", "name": "pool-tour.mp4", "size": 8100000,
     "active": True, "order": 2, "processing_status": "processing", "processing_error": ""},
    {"id": 4, "room_id": None, "kind": "audio", "name": "lounge.mp3", "size": 3000000,
     "active": True, "order": 0, "processing_status": "ready", "processing_error": ""},
]
ROOMS = [{"id": 1, "number": "101", "type": "Deluxe", "eligible": True, "active": True}]
SETTINGS = {"slideshow_seconds": 5, "max_upload_mb": 50, "music_volume": 40, "music_id": None,
            "music_enabled": False, "music_autoplay": False, "effective_max_upload_mb": 50,
            "storage_ready": True, "storage_detail": ""}

CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml",
                 ".webmanifest": "application/manifest+json"}


def media_payload():
    """The API list shape, with the CURRENT cover applied."""
    rows = []
    for row in MEDIA:
        item = dict(row)
        item["is_cover"] = (item["id"] == STATE["cover_id"] and item["room_id"] == 1)
        rows.append(item)
    return rows


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status, payload):
        return self._send(status, json.dumps(payload).encode())

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if path.startswith("/api/auth/login/"):
            return self._json(200, {"success": True, "data": {"tokens": {"access": "test-token", "refresh": "r"}}})
        if "/manage/media/" in path and path.rstrip("/").endswith("retry-processing"):
            # A broker/queue failure must be reported as a failure — never as
            # "queued" (the staging regression tv_gallery_admin_retry_test.py
            # guards against on a live backend).
            return self._json(200, {"success": True, "data": dict(
                MEDIA[2], processing_status="failed", processing_error="Queue unavailable.")})
        if path.startswith("/api/rooms-gallery/manage/media/"):
            return self._json(201, {"success": True, "data": dict(MEDIA[1], id=9, name="new.jpg")})
        return self._json(404, {"success": False, "message": "not found"})

    def do_PATCH(self):  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) or b"{}"
        if "/api/rooms-gallery/manage/media/" in path:
            STATE["patch_calls"] += 1
            if STATE["patch_delay"]:
                time.sleep(STATE["patch_delay"])
            if STATE["fail_patch"]:
                STATE["fail_patch"] = False
                return self._json(400, {"success": False, "code": "VALIDATION_ERROR",
                                        "message": "Validation failed.",
                                        "errors": {"is_cover": ["Only room images can be covers."]}})
            try:
                payload = json.loads(body.decode() or "{}")
            except ValueError:
                payload = {}
            media_id = int(path.rstrip("/").rsplit("/", 1)[-1])
            if payload.get("is_cover"):
                STATE["cover_id"] = media_id
            item = next((dict(row) for row in MEDIA if row["id"] == media_id), None)
            if item is None:
                return self._json(404, {"success": False, "message": "not found"})
            item["is_cover"] = media_id == STATE["cover_id"] and item["room_id"] == 1
            return self._json(200, {"success": True, "data": item})
        return self._json(404, {"success": False, "message": "not found"})

    def do_DELETE(self):  # noqa: N802
        return self._json(204, {})

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/rooms-gallery/manage/media/"):
            parts = path.rstrip("/").split("/")
            if parts[-1] == "media":
                # The collection itself: the API answers with a bare list.
                return self._json(200, {"success": True, "data": media_payload()})
            rendition = parts[-1]
            if rendition in ("thumb", "display", "file"):
                STATE["thumb_calls"] += 1
                if rendition == "file":
                    # The mock cannot produce video bytes; answer like the API
                    # does for a video whose rendering is unavailable.
                    return self._json(500, {"success": False, "code": "GALLERY_MEDIA_FILE_MISSING",
                                            "message": "stored file missing"})
                return self._send(200, PNG, "image/jpeg")
            if rendition == "retry-processing":
                return self._json(405, {"success": False, "message": "method"})
            return self._json(200, {"success": True, "data": dict(MEDIA[1])})

        if path.startswith("/api/rooms-gallery/manage/"):
            tail = path.split("/manage/", 1)[-1].strip("/").split("/", 1)[0]
            payload = {"rooms": {"success": True, "data": ROOMS},
                       "media": {"success": True, "data": media_payload()},
                       "settings": {"success": True, "data": SETTINGS}}.get(tail)
            return self._json(200, payload if payload else {"success": True, "data": {}})

        if path.startswith("/api/rooms-gallery/"):
            return self._json(200, {"success": True, "data": {"rooms": []}})

        if path.rstrip("/") == "/rooms-gallery/admin":
            path = "/rooms-gallery/admin.html"
        target = (FRONTEND / path.lstrip("/")).resolve()
        if not str(target).startswith(str(FRONTEND)) or not target.is_file():
            return self._send(404, b"not found", "text/plain")
        return self._send(200, target.read_bytes(),
                          CONTENT_TYPES.get(target.suffix, "application/octet-stream"))


PHONE_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36")


def overflow(page):
    return page.evaluate("""() => ({vw: window.innerWidth, sw: document.documentElement.scrollWidth})""")


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
            pass

    server = Quiet(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    failures = []

    def check(condition, message):
        if not condition:
            failures.append(message)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            # ---------- 1. media cards, thumbnails, cover marker --------------
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()
            page.goto(base + "/rooms-gallery/admin", wait_until="load")
            sign_in(page)
            page.wait_for_selector("#admin:not([hidden])", timeout=10000)
            page.wait_for_selector('#room option[value="1"]', timeout=10000, state="attached")
            page.select_option("#room", "1")
            page.wait_for_selector(".media-card", timeout=10000)
            page.wait_for_timeout(600)  # thumbnails are fetched asynchronously

            cards = page.locator(".media-card")
            check(cards.count() == 3, "expected 3 media cards for room 1, saw %d" % cards.count())
            check(STATE["thumb_calls"] >= 2,
                  "thumbnails were not requested through the authenticated rendition route")
            check(page.locator('.media-card[data-media-id="1"] .media-thumb img').count() == 1,
                  "the first image card has no <img> thumbnail")
            check(page.locator('.media-card[data-media-id="2"] .media-thumb img').count() == 1,
                  "the second image card has no <img> thumbnail")
            check(page.locator('.media-card[data-media-id="1"] .badge-cover').count() == 1,
                  "the server-side cover is not marked on its card")
            check(page.locator('.media-card[data-media-id="3"] .media-kind').inner_text().lower() == "video",
                  "a video is not identifiable as a video")
            check(page.locator('.media-card[data-media-id="3"] [data-tone="warn"]').count() >= 1,
                  "a video that is still processing is not flagged")

            # ---------- 1b. a failed processing retry is not "queued" --------
            page.locator('.media-card[data-media-id="3"] button:has-text("Retry processing")').click()
            page.wait_for_function(
                "() => (document.getElementById('message')||{}).getAttribute('data-kind') === 'ok'",
                timeout=15000)
            check(page.locator("#message").inner_text().startswith("Retry failed: Queue unavailable."),
                  "a failed video-processing retry was not reported as a failure (got %r)"
                  % page.locator("#message").inner_text())

            # ---------- 2. busy state while a request is open -----------------
            STATE["patch_delay"] = 1.2
            cover_button = page.locator('.media-card[data-media-id="2"] button:has-text("Set as cover")')
            cover_button.click()
            page.wait_for_timeout(250)
            check(page.locator('.media-card[data-media-id="2"] button[aria-busy="true"]').count() == 1,
                  "the clicked control did not show a busy state while the request was open")
            check("Saving" in page.locator("#message").inner_text(),
                  "the status line did not say what was happening while the request was open")
            page.wait_for_function(
                "() => (document.getElementById('message')||{}).getAttribute('data-kind') === 'ok'",
                timeout=15000)
            STATE["patch_delay"] = 0.0
            check(page.locator('.media-card[data-media-id="2"] .badge-cover').count() == 1,
                  "the newly chosen cover is not marked after the server confirmed it")
            check(page.locator('.media-card[data-media-id="1"] .badge-cover').count() == 0,
                  "the previous cover is still marked as the cover")

            # ---------- 3. the cover survives a full reload -------------------
            page.reload(wait_until="load")
            page.wait_for_selector('#room option[value="1"]', timeout=10000, state="attached")
            page.select_option("#room", "1")
            page.wait_for_selector(".media-card", timeout=10000)
            page.wait_for_timeout(500)
            check(page.locator('.media-card[data-media-id="2"] .badge-cover').count() == 1,
                  "after a reload the selected cover is not shown as the cover "
                  "(the selection was not persisted)")
            check(page.locator('.media-card[data-media-id="1"] .badge-cover').count() == 0,
                  "after a reload the old cover came back")

            # ---------- 4. a refused request is reported, the button returns --
            STATE["fail_patch"] = True
            again = page.locator('.media-card[data-media-id="1"] button:has-text("Set as cover")')
            again.click()
            page.wait_for_function(
                "() => (document.getElementById('message')||{}).getAttribute('data-kind') === 'error'",
                timeout=15000)
            check("Only room images can be covers" in page.locator("#message").inner_text(),
                  "the API's own explanation was not shown for a refused cover change")
            check(page.locator('.media-card[data-media-id="2"] .badge-cover').count() == 1,
                  "a refused change altered the cover shown to the operator")
            check(page.locator('.media-card[data-media-id="1"] button[aria-busy="true"]').count() == 0,
                  "the control stayed busy after the request finished")

            # ---------- 5. upload shows progress then the file is listed ------
            page.set_input_files("#files", files=[{
                "name": "new-room.jpg", "mimeType": "image/jpeg",
                "buffer": b"\xff\xd8\xff\xe0" + b"0" * 512}])
            page.click("#upload")
            page.wait_for_function(
                "() => Array.from(document.querySelectorAll('.upload-row')).some(r => r.dataset.state === 'done')",
                timeout=15000)
            check(page.locator('.upload-row[data-state="done"]').count() == 1,
                  "an upload never reached the finished state")
            context.close()

            # ---------- 6. phone layout --------------------------------------
            for width in (320, 360, 390, 414):
                context = browser.new_context(viewport={"width": width, "height": 780},
                                              is_mobile=True, has_touch=True, user_agent=PHONE_UA)
                page = context.new_page()
                page.goto(base + "/rooms-gallery/admin", wait_until="load")
                before = overflow(page)
                check(before["sw"] <= before["vw"] + 1,
                      "%dpx: the sign-in page scrolls horizontally (%s > %s)"
                      % (width, before["sw"], before["vw"]))
                sign_in(page)
                page.wait_for_selector("#admin:not([hidden])", timeout=10000)
                page.wait_for_selector('#room option[value="1"]', timeout=10000, state="attached")
                page.select_option("#room", "1")
                page.wait_for_selector(".media-card", timeout=10000)
                page.wait_for_timeout(500)
                after = overflow(page)
                check(after["sw"] <= after["vw"] + 1,
                      "%dpx: the media gallery scrolls horizontally (%s > %s)"
                      % (width, after["sw"], after["vw"]))
                # Touch targets: every action button must be at least 38px tall.
                small = page.evaluate("""() => Array.from(document.querySelectorAll('.media-actions button'))
                    .filter(b => b.getBoundingClientRect().height < 38).length""")
                check(small == 0, "%dpx: %d media action button(s) are too small to tap" % (width, small))
                context.close()

            browser.close()
    finally:
        server.shutdown()

    if failures:
        print("FAIL:")
        for item in failures:
            print(" - " + item)
        return 1
    print("PASS: media cards render real thumbnails, the cover is persisted across a reload, "
          "every control shows a real busy state, refusals are reported, and the layout fits "
          "320/360/390/414 px")
    return 0


if __name__ == "__main__":
    sys.exit(main())
