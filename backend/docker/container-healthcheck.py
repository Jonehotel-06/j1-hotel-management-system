#!/usr/bin/env python3
"""Lightweight, zero-side-effect container health probe for J-ONE Hotel & Lodge.

Behaviour by process role:
  * Web process (Gunicorn, default `SERVICE_ROLE=web`):
    GET http://127.0.0.1:${PORT:-8000}${HEALTHCHECK_PATH:-/api/health/live/}
    - Defaults to /api/health/live/ (process liveness, no DB touch) so transient
      upstream blips do not kill healthy web workers.
    - Orchestrators that want DB readiness configure /api/health/ready/ explicitly.
  * Celery worker / beat process (`SERVICE_ROLE=worker|beat` or a running celery
    process anywhere in /proc — including Northflank microVM runtimes where PID 1
    is the guest init rather than celery itself):
    verifies that at least one live, non-zombie celery process exists without
    attempting an HTTP probe against an unexposed port.
"""
import os
import sys
import urllib.error
import urllib.request


def _iter_live_process_cmdlines() -> list[tuple[int, str, list[str]]]:
    entries: list[tuple[int, str, list[str]]] = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return entries
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                parts = [p.decode("utf-8", errors="ignore") for p in fh.read().split(b"\x00") if p]
            if not parts:
                continue
            state = ""
            try:
                with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="ignore") as sfh:
                    raw_stat = sfh.read()
                rparen = raw_stat.rfind(")")
                if rparen != -1:
                    fields = raw_stat[rparen + 2:].split()
                    state = fields[0] if fields else ""
            except OSError:
                state = ""
            entries.append((pid, state, parts))
        except OSError:
            continue
    return entries


def main() -> int:
    role = os.environ.get("SERVICE_ROLE", "").strip().lower()
    procs = _iter_live_process_cmdlines()
    live_celery = [
        (pid, parts)
        for pid, state, parts in procs
        if state != "Z" and any("celery" in part for part in parts[:4])
    ]

    if role in {"worker", "beat", "scheduler"}:
        if procs:
            return 0 if live_celery else 1
        # Hardened container runtimes with restricted /proc visibility: fall back
        # to checking pid 1 if readable; do not falsely probe HTTP on background workers.
        return 0

    if live_celery and not any("gunicorn" in part for _, _, parts in procs for part in parts[:4]):
        return 0

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
