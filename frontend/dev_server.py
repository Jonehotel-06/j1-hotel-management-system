#!/usr/bin/env python3
# dev_server.py
"""
J-ONE HOTEL & LODGE — local development server.

Serves the static frontend from this directory and transparently proxies
/api/ and /media/ to the Django backend (default http://127.0.0.1:8000),
so the site runs exactly as it will in production (same-origin API).

The frontend itself is pure HTML/CSS/JS — this file is only a convenience
for local development / single-box demos. In production, serve the static
files from any web server (or Django) and place /api/ behind the same origin
(or set API_BASE_URL in js/config.js).

Usage:
    python3 dev_server.py [port] [backend_url]
    python3 dev_server.py 5500 http://127.0.0.1:8000
"""

import http.client
import http.server
import socketserver
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 5500
BACKEND = (sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8000").rstrip("/")
PROXY_PREFIXES = ("/api/", "/media/")

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-encoding",
}


class ProxyMixin:
    """Forwards /api/ and /media/ to the Django backend.

    The original Host header and X-Forwarded-Proto are passed through so the
    backend builds absolute URLs (image_url etc.) against the origin the
    visitor is actually using — not 127.0.0.1.
    """

    def _proxy(self):
        parsed = urllib.parse.urlsplit(BACKEND)
        conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), timeout=60)

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        # Ask the backend for identity encoding so we can re-emit the body
        # with a correct Content-Length without decompressing anything.
        fwd = {}
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in HOP_BY_HOP or lk == "accept-encoding":
                continue
            fwd[k] = v
        fwd["Host"] = self.headers.get("Host") or parsed.netloc
        fwd["Accept-Encoding"] = "identity"
        if "x-forwarded-proto" not in {k.lower() for k in fwd}:
            fwd["X-Forwarded-Proto"] = self.headers.get("X-Forwarded-Proto", "http")
        fwd["X-Forwarded-Host"] = self.headers.get("Host", "")

        try:
            conn.request(self.command, self.path, body=body, headers=fwd)
            up = conn.getresponse()
            payload = up.read()
            self.send_response(up.status)
            for k, v in up.getheaders():
                # Content-Length is re-emitted once below with the exact
                # proxied byte count (avoids duplicate headers).
                if k.lower() not in HOP_BY_HOP and k.lower() != "content-length":
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        except Exception as e:  # backend down / timeout — honest 502
            msg = ("Backend unreachable (%s). Is the Django server running on %s?"
                   % (type(e).__name__, BACKEND)).encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(msg)
        finally:
            conn.close()


class Handler(ProxyMixin, http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def do_GET(self):
        if self.path.startswith(PROXY_PREFIXES):
            return self._proxy()
        return super().do_GET()

    def do_HEAD(self):
        if self.path.startswith(PROXY_PREFIXES):
            return self._proxy()
        return super().do_HEAD()

    def do_POST(self):
        return self._proxy()

    def do_PATCH(self):
        return self._proxy()

    def do_PUT(self):
        return self._proxy()

    def do_DELETE(self):
        return self._proxy()

    def do_OPTIONS(self):
        return self._proxy()

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    print("J-ONE frontend  : http://0.0.0.0:%d  (static root: %s)" % (PORT, ROOT))
    print("Proxying to API : %s/api/  and  %s/media/" % (BACKEND, BACKEND))
    with Server(("0.0.0.0", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
