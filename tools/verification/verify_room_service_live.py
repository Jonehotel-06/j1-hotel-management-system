#!/usr/bin/env python3
"""Item 8, mode 4 — room service, against a REAL in-house stay.

The dataset had no booking, so this script creates one through the application's
own staff API (manual walk-in booking → check-in → in-house stay with an open
folio), then drives `dashboard/pos.html` in the browser exactly like a cashier:

    search guest → pick the stay → two lines → press "Create draft" twice

and checks the DATABASE through the API that exactly one ROOM_SERVICE draft was
created, that it is linked to the stay/guest, that no kitchen ticket appeared for
a DRAFT, and that the cart was only cleared once the server confirmed.

Cleanup is non-destructive and explicit: the draft is CANCELLED (never deleted),
then the booking is checked out with the outstanding balance preserved
(`allow_balance_due`) so the room is released and the stay is closed — no payment
is fabricated and no record is removed.

Run:  python tools/verification/verify_room_service_live.py
"""
import asyncio
import sys
import time

import requests
from jone_verify import API_BASE as API, MANAGER, Session, WEB_BASE as BASE, artifact


RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("PASS" if ok else "FAIL"), name, "-", detail)


class Api:
    def __init__(self, session):
        self.h = {"Authorization": "Bearer " + session.tokens["access"]}

    def get(self, path, **params):
        return requests.get(API + path, headers=self.h, params=params, timeout=30)

    def post(self, path, body=None):
        return requests.post(API + path, headers=self.h, json=body or {}, timeout=60)

    def rows(self, path, **params):
        body = self.get(path, **params).json()
        rows = body.get("data")
        if isinstance(rows, dict):
            rows = rows.get("results", rows.get("items", []))
        return rows or []


def open_stay(api, reference):
    return next((s for s in api.rows("/api/admin/pos/room-service-stays/", page_size=100)
                 if s.get("booking_reference") == reference), None)


def create_in_house_stay(api):
    """Walk-in booking → check-in, through the staff API (no direct DB writes)."""
    today = time.strftime("%Y-%m-%d")
    room_types = api.get("/api/rooms/").json().get("data") or []
    if isinstance(room_types, dict):
        room_types = room_types.get("results", [])
    if not room_types:
        return None, "no room type published by /api/rooms/"
    slug = room_types[0]["slug"]

    check_out = time.strftime("%Y-%m-%d", time.localtime(time.time() + 2 * 86400))
    created = api.post("/api/admin/bookings/", {
        "room_type": slug,
        "check_in": today,
        "check_out": check_out,
        "rooms": 1,
        "adults": 1,
        "children": 0,
        "source": "WALK_IN",
        "guest": {"first_name": "Audit", "last_name": "Roomservice",
                  "email": f"audit.roomservice+{int(time.time())}@example.test",
                  "phone": "08010000000"},
    })
    if created.status_code != 201:
        return None, f"booking create HTTP {created.status_code}: {created.text[:200]}"
    booking = created.json()["data"]
    reference = booking["booking_reference"]

    checked_in = api.post(f"/api/admin/bookings/{reference}/check-in/", {})
    if checked_in.status_code != 200:
        return None, f"check-in HTTP {checked_in.status_code}: {checked_in.text[:200]}"
    return reference, ""


async def drive_pos(pw, session, booking_reference, guest_name):
    browser = await pw.chromium.launch()
    ctx = await browser.new_context(viewport={"width": 1440, "height": 950})
    await ctx.add_init_script(session.init_script())
    page = await ctx.new_page()
    errors, posts, responses = [], [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("request", lambda r: posts.append(r.method) if "/pos/orders/" in r.url and r.method == "POST" else None)
    page.on("response", lambda r: responses.append(r.status) if "/pos/orders/" in r.url and r.request.method == "POST" else None)
    await page.goto(BASE + "/dashboard/pos.html", wait_until="networkidle")
    await page.wait_for_timeout(1200)

    api = Api(session)
    orders_before = api.rows("/api/admin/pos/orders/", page_size=100, mode="ROOM_SERVICE", status="DRAFT")
    tickets_before = api.rows("/api/admin/pos/kitchen-tickets/", page_size=100)

    await page.select_option("#pos-mode", "ROOM_SERVICE")
    await page.wait_for_timeout(500)
    await page.fill("#pos-stay-search", booking_reference)
    await page.click("#pos-find-stay")
    await page.wait_for_timeout(1500)
    picked = await page.evaluate("""() => {
        const btn = document.querySelector('[data-pos-stay]');
        if (!btn) return null;
        btn.click();
        return btn.textContent.trim();
    }""")
    await page.wait_for_timeout(400)
    selected_label = await page.inner_text("[data-pos-selected-stay]")
    if not picked:
        await page.screenshot(path=artifact("ui-08-pos-room_service-no-stay.png"))
        await browser.close()
        return False, "the stay was not offered by the room-service guest picker", {}, None

    for _ in range(2):
        await page.select_option("#pos-item", index=1)
        await page.wait_for_timeout(200)
        await page.click("#pos-add-line")
        await page.wait_for_timeout(250)
    cart_before = await page.inner_text("[data-pos-cart]")

    submit = page.locator('#pos-create-form button[type="submit"]').first
    await submit.click(no_wait_after=True)
    try:
        await submit.click(no_wait_after=True, timeout=1500)
    except Exception:
        pass
    await page.wait_for_timeout(5000)

    orders_after = api.rows("/api/admin/pos/orders/", page_size=100, mode="ROOM_SERVICE", status="DRAFT")
    tickets_after = api.rows("/api/admin/pos/kitchen-tickets/", page_size=100)
    before_refs = {o["reference"] for o in orders_before}
    new_orders = [o for o in orders_after if o["reference"] not in before_refs]
    banner = await page.evaluate(
        "() => { const e = document.querySelector('[data-pos-create-error]'); return e && !e.hidden ? e.textContent.trim() : ''; }")
    cart_after = await page.inner_text("[data-pos-cart]")

    detail = {}
    if new_orders:
        order = api.get(f"/api/admin/pos/orders/{new_orders[0]['reference']}/").json().get("data") or {}
        detail = {"reference": order.get("reference"), "mode": order.get("mode"),
                  "guest_name": order.get("guest_name"), "status": order.get("status"),
                  "lines": len(order.get("lines", []) or []), "total": order.get("total_amount"),
                  "kitchen_ticket": order.get("kitchen_ticket_reference") or order.get("has_kitchen_ticket")}

    await page.screenshot(path=artifact("ui-08-pos-room_service.png"))
    await browser.close()

    created = len(new_orders)
    new_tickets = len(tickets_after) - len(tickets_before)
    ok = (created == 1 and new_tickets == 0 and len(posts) == 1 and responses[:1] == [201]
          and "No items yet" in cart_after and not errors
          and (detail.get("guest_name") or "").startswith(guest_name.split()[0]))
    summary = (f"orders +{created}, POST {len(posts)} -> {responses}, tickets +{new_tickets}, "
               f"cart cleared={'No items yet' in cart_after}, selected={selected_label.splitlines()[-1][:64]!r}, "
               f"order={detail.get('reference')} guest={detail.get('guest_name')!r} status={detail.get('status')} "
               f"lines={detail.get('lines')} ticket={detail.get('kitchen_ticket')!r}, banner={banner[:40]!r}, "
               f"pageerrors={errors[:1] or 'none'}")
    return ok, summary, detail, new_orders[0]["reference"] if new_orders else None


async def main():
    session = Session(MANAGER)
    api = Api(session)

    reference, problem = create_in_house_stay(api)
    if not reference:
        record("room service: one draft for a real in-house stay", False, problem)
        return 1
    stay = open_stay(api, reference)
    print(f"· in-house stay ready: booking {reference} stay={(stay or {}).get('reference')} "
          f"guest={(stay or {}).get('guest_name')!r} rooms={(stay or {}).get('room_numbers')}")

    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        ok, summary, detail, order_reference = await drive_pos(
            pw, session, reference, (stay or {}).get("guest_name") or "Audit")

    record("room service: exactly one draft for a real in-house stay, linked to that guest", ok, summary)

    # ---- cleanup (non-destructive)
    cleanup = []
    if order_reference:
        cancelled = api.post(f"/api/admin/pos/orders/{order_reference}/status/",
                             {"status": "CANCELLED", "note": "created by the audit verification run; cleaned up"})
        cleanup.append(f"draft cancelled={cancelled.status_code}")
    checked_out = api.post(f"/api/admin/bookings/{reference}/check-out/", {"allow_balance_due": True})
    cleanup.append(f"booking checked out (balance preserved)={checked_out.status_code}")
    still_open = open_stay(api, reference)
    cleanup.append(f"stay still in house={bool(still_open)}")
    print("· cleanup:", ", ".join(cleanup))

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} room-service checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
