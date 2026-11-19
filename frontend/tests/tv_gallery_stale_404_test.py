"""Browser test: a cached 404 can no longer mask repaired gallery media.

WHAT THIS PROVES
================
TV gallery errors in production were reported as "404 Not Found (from service
worker)". Two behaviours made that permanent:

  * ``cachedMedia()`` answered EVERY request for a downloaded file from the
    download cache, so once a media URL had been stored the server was never
    consulted again — a backend that started serving the file could not be
    noticed;
  * nothing recorded that the stored generation had become stale.

After the 2026-11-20 repair, while ONLINE, the server answers first: a non-OK
answer is returned as-is (not replaced with cached bytes) and a 404/410 marks
the downloaded generation ``outdated`` so the admin page prompts a re-sync.
Offline playback is unchanged.

This runs the REAL service worker, the real offline.js and a real HTTP server
serving both the static frontend and the gallery API on one origin (127.0.0.1
is a secure context, which the service worker requires). Nothing about the
worker is stubbed.

    python frontend/tests/tv_gallery_stale_404_test.py

Exits non-zero on failure. Requires Python Playwright + Chromium.
"""
import json
import os
import struct
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1]

STATE = {"media_status": 200}


def png_bytes(width=120, height=80, colour=(30, 90, 160)):
    def chunk(tag, data):
        payload = tag + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)

    raw = b""
    for _ in range(height):
        raw += b"\x00" + bytes(colour) * width
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


MEDIA = png_bytes()
CATALOG = {
    "success": True,
    "data": {
        "rooms": [{
            "id": 7, "number": "202", "type": "Deluxe",
            "media": [{
                "id": 7, "kind": "image",
                "url": None,          # filled in per request (absolute)
                "thumbnail": None,
                "available": True,
            }],
        }],
        "slideshow_seconds": 3,
        "music_url": None,
        "music_autoplay": False,
        "music_volume": 35,
    },
}


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
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        base = "http://127.0.0.1:%d" % self.server.server_address[1]

        if path == "/api/rooms-gallery/catalog/":
            payload = json.loads(json.dumps(CATALOG))
            media = payload["data"]["rooms"][0]["media"][0]
            media["url"] = base + "/api/rooms-gallery/media/7/display/"
            media["thumbnail"] = base + "/api/rooms-gallery/media/7/thumb/"
            body = json.dumps(payload).encode()
            return self._send(200, body, "application/json")

        if path.startswith("/api/rooms-gallery/media/"):
            if STATE["media_status"] != 200:
                body = json.dumps({"success": False, "code": "GALLERY_MEDIA_FILE_MISSING"}).encode()
                return self._send(STATE["media_status"], body, "application/json")
            return self._send(200, MEDIA, "image/jpeg")

        if path.rstrip("/") == "/rooms-gallery":
            path = "/rooms-gallery/index.html"
        target = (FRONTEND / path.lstrip("/")).resolve()
        if not str(target).startswith(str(FRONTEND)) or not target.is_file():
            return self._send(404, b"not found", "text/plain")
        content_type = {
            ".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml",
            ".webmanifest": "application/manifest+json",
        }.get(target.suffix, "application/octet-stream")
        return self._send(200, target.read_bytes(), content_type)


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright is not installed (pip install playwright && playwright install chromium)")
        return 2

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port

    ok = True
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.goto(base + "/rooms-gallery/", wait_until="load")
            page.evaluate("() => navigator.serviceWorker.ready.then(() => true)")

            # 1. Download the gallery for offline use while the API serves media.
            result = page.evaluate("""async () => {
                const catalog = await JoneOffline.freshCatalog();
                await JoneOffline.sync(catalog, ['7'], []);
                return await JoneOffline.status();
            }""")
            if not result.get("ready"):
                print("FAIL: initial synchronisation did not become ready:", result)
                ok = False

            # 2. The backend now refuses the media (the production 404 shape).
            STATE["media_status"] = 404
            page.reload(wait_until="load")
            page.evaluate("() => navigator.serviceWorker.ready.then(() => true)")

            observed = page.evaluate("""async () => {
                const res = await fetch('http://127.0.0.1:%d/api/rooms-gallery/media/7/display/', {cache: 'no-store'});
                const state = await JoneSyncDB.read();
                return {status: res.status, outdated: !!(state && state.outdated)};
            }""" % port)
            if observed["status"] != 404:
                print("FAIL: a cached copy was served for a 404 (got %s) — stale 404s would survive"
                      % observed["status"])
                ok = False
            if not observed["outdated"]:
                print("FAIL: the download generation was not flagged outdated after a 404")
                ok = False

            # 3. The offline copy is NOT destroyed by that flag.
            page.evaluate("() => navigator.serviceWorker.ready.then(() => true)")
            still_cached = page.evaluate("""async () => {
                const state = await JoneSyncDB.read();
                if (!state || !state.activeCache) return false;
                const cache = await caches.open(state.activeCache);
                return !!(await cache.match('http://127.0.0.1:%d/api/rooms-gallery/media/7/display/'));
            }""" % port)
            if not still_cached:
                print("FAIL: the downloaded media was discarded instead of being flagged")
                ok = False

            # 4. Once the backend serves it again, the network answer wins again.
            STATE["media_status"] = 200
            recovered = page.evaluate("""async () => {
                const res = await fetch('http://127.0.0.1:%d/api/rooms-gallery/media/7/display/', {cache: 'no-store'});
                return res.status;
            }""" % port)
            if recovered != 200:
                print("FAIL: media did not recover once the server served it again (got %s)" % recovered)
                ok = False

            # 5. Offline playback still works from the download.
            context.set_offline(True)
            offline = page.evaluate("""async () => {
                const res = await fetch('http://127.0.0.1:%d/api/rooms-gallery/media/7/display/');
                return res.status;
            }""" % port)
            if offline != 200:
                print("FAIL: offline playback lost the downloaded media (got %s)" % offline)
                ok = False

            browser.close()
    finally:
        server.shutdown()
        server.server_close()

    print("PASS: online 404 is never masked by the cache, the generation is flagged, "
          "the download is preserved and offline playback still works" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
