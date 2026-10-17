#!/usr/bin/env python3
"""Live E2E for item 14 — recover a booking on the device that made it.

Talks to a REAL runserver over HTTP, no Django test client. The steps mirror
what a phone actually does:

  1. POST /api/bookings/ with an Idempotency-Key         (the browser's own key)
  2. throw the 201 response away                          (the lost token)
  3. POST /api/bookings/<ref>/access/ {idempotency_key}   (reclaim)
  4. GET  /api/bookings/<ref>/ with the new token         (detail opens)
  5. POST /api/payments/initialize/ with the new token    (reaches the view)
  6. do it again behind a STALE Bearer                    (must not 401 first)
  7. wrong key / foreign key                              (still a uniform 404)
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8013"


def call(method, path, body=None, headers=None, expect=None, label=""):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {"_raw": raw[:200].decode("utf-8", "replace")}
    mark = "OK " if (expect is None or status == expect) else "BAD"
    print(f"{mark} {label or method + ' ' + path} -> {status}")
    if expect is not None and status != expect:
        print("    expected", expect, "body:", json.dumps(payload)[:500])
    return status, payload


def main():
    failures = 0
    email = "e2e.recover@example.test"

    status, created = call(
        "POST", "/api/bookings/",
        {
            "room_type": "e2e-recover",
            "check_in": "2026-11-20",
            "check_out": "2026-11-22",
            "rooms": 1,
            "adults": 2,
            "children": 0,
            "guest": {
                "first_name": "E2E",
                "last_name": "Recover",
                "email": email,
                "phone": "08030000009",
            },
        },
        headers={"Idempotency-Key": "e2e-recover-key-0001"},
        expect=201,
        label="1. create booking (browser key)",
    )
    if status != 201:
        print(json.dumps(created)[:800])
        return 1
    ref = created["data"]["booking_reference"]
    first_token = created["data"]["guest_access_token"]
    print("    booking:", ref)
    print("    key stored for reclaim: e2e-recover-key-0001")

    # 2. The 201 response is "lost" — only ref + key survive, as in the browser.
    del first_token

    status, reclaimed = call(
        "POST", f"/api/bookings/{ref}/access/",
        {"idempotency_key": "e2e-recover-key-0001"},
        expect=200, label="2. reclaim with the creation key (no email)",
    )
    token = reclaimed.get("data", {}).get("guest_access_token")
    if not token:
        print("    no guest_access_token in response")
        return 1
    print("    fresh token:", token[:12] + "…")

    status, detail = call(
        "GET", f"/api/bookings/{ref}/",
        headers={"X-Guest-Access-Token": token},
        expect=200, label="3. booking detail with the reclaimed token",
    )
    if status == 200:
        print("    reference in body:", detail["data"]["booking_reference"])

    status, pay = call(
        "POST", "/api/payments/initialize/",
        {"booking_reference": ref},
        headers={"X-Guest-Access-Token": token},
        expect=503, label="4. initialize reaches the view (503 = no gateway key)",
    )
    if status == 503:
        print("    code:", pay.get("code"), "| message:", str(pay.get("message"))[:90])

    # 5. The reported bug: a dashboard JWT left in the same browser, expired.
    stale = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0b2tlbl90eXBlIjoiYWNjZXNzIiwiZXhwIjoxfQ.bad"
    status, _ = call(
        "POST", "/api/payments/initialize/",
        {"booking_reference": ref},
        headers={"X-Guest-Access-Token": token, "Authorization": stale},
        expect=503, label="5. stale Bearer no longer wins (was 401)",
    )
    if status != 503:
        failures += 1

    status, detail2 = call(
        "GET", f"/api/bookings/{ref}/",
        headers={"X-Guest-Access-Token": token, "Authorization": stale},
        expect=200, label="6. booking page also ignores the stale Bearer",
    )
    if status != 200:
        failures += 1

    # 7. A key that is not this booking's is indistinguishable from "no booking".
    status, _ = call(
        "POST", f"/api/bookings/{ref}/access/",
        {"idempotency_key": "e2e-recover-key-NOPE"},
        expect=404, label="7. wrong key -> 404 (not an oracle)",
    )
    if status != 404:
        failures += 1

    status, _ = call(
        "POST", f"/api/bookings/{ref}/access/", {},
        expect=400, label="8. empty request -> 400",
    )
    if status != 400:
        failures += 1

    # 9. The email path still exists and still answers 200 for any input.
    status, _ = call(
        "POST", f"/api/bookings/{ref}/access-link/",
        {"email": email},
        expect=200, label="9. emailed secure link still available",
    )
    if status != 200:
        failures += 1

    # 10. Reclaim rotates: the token superseded by step 2 must be dead.
    status, again = call(
        "POST", f"/api/bookings/{ref}/access/",
        {"idempotency_key": "e2e-recover-key-0001"},
        expect=200, label="10. reclaim again (rotation)",
    )
    new_token = again.get("data", {}).get("guest_access_token")
    status, _ = call(
        "GET", f"/api/bookings/{ref}/",
        headers={"X-Guest-Access-Token": token},
        expect=404, label="11. superseded token is refused",
    )
    if status != 404:
        failures += 1
    status, _ = call(
        "GET", f"/api/bookings/{ref}/",
        headers={"X-Guest-Access-Token": new_token},
        expect=200, label="12. newest token works",
    )
    if status != 200:
        failures += 1

    # 13. The pay retry after reclaim — same reference, still 503 not 401.
    status, _ = call(
        "POST", "/api/payments/initialize/",
        {"booking_reference": ref},
        headers={"X-Guest-Access-Token": new_token},
        expect=503, label="13. payment retry after rotation reaches the view",
    )
    if status != 503:
        failures += 1

    print()
    print("REF", ref, "FAILURES", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
