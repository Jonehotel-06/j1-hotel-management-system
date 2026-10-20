#!/usr/bin/env python3
"""Lightweight, zero-side-effect container health probe for J-ONE Hotel & Lodge.

Behaviour by process role:
  * Web process (Gunicorn, default): GET http://127.0.0.1:${PORT:-8000}${HEALTHCHECK_PATH:-/api/health/live/}
    - Defaults to /api/health/live/ (process liveness, no DB touch) so transient
      upstream blips do not kill healthy web workers.
    - Compose / orchestrators that want DB readiness set HEALTHCHECK_PATH=/api/health/ready/.
  * Celery worker / beat process (when the same image runs background jobs):
    verifies PID 1 is alive and running Celery without opening unnecessary network
    or broker round-trips on every interval tick.
"""
import os
import sys
import urllib.error
import urllib.request


def _pid1_args() -> list[str]:
    try:
        with open("/proc/1/cmdline", "rb") as fh:
            raw = fh.read().split(b"\x00")
        return [part.decode("utf-8", errors="ignore") for part in raw if part]
    except OSError:
        return []


def main() -> int:
    cmd = _pid1_args()
    if any("celery" in part for part in cmd[:3]):
        # Background worker or beat scheduler shares this image without an HTTP port.
        return 0 if cmd else 1

    port = os.environ.get("PORT", "8000").strip() or "8000"
    path = os.environ.get("HEALTHCHECK_PATH", "/api/health/live/").strip() or "/api/health/live/"
    if not path.startswith("/"):
        path = "/" + path

    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "jone-container-healthcheck/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            return 0 if 200 <= resp.status < 300 else 1
    except (urllib.error.URLError, TimeoutError, OSError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
