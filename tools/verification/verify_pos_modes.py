#!/usr/bin/env python3
"""Item 8 — POS "Create draft" across ALL FOUR modes, in a real browser.

Per mode the script drives `dashboard/pos.html` exactly like an operator:
add two lines → press "Create draft" (and a second time immediately, to try to
duplicate it) → then checks the DATABASE through the API (not the page's own
list) that exactly one order appeared, that no kitchen ticket was created for a
DRAFT, and that the cart is only cleared once the server confirmed.

Setup/cleanup for the two modes that need a context:
  * restaurant — a registered table "T-AUDIT" is created, a session opened,
    used, then closed and the table deactivated (reversible, non-destructive);
  * room service — needs an in-house stay with an open folio. This dataset has
    none (no bookings), so the script asserts the mode's guard instead (an
    actionable "select an in-house guest" error and NO order), and reports the
    happy path as covered by backend tests + the identical create_order path.

Run:  python tools/verification/verify_pos_modes.py
"""
import asyncio
import sys
import time

import requests
from jone_verify import API_BASE as API, MANAGER, Session, WEB_BASE as BASE, artifact


MODES = {
    "TAKEAWAY": "takeaway",
    "BAR": "bar",
    "RESTAURANT": "restaurant (dine-in)",
    "ROOM_SERVICE": "room service",
}
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

    def patch(self, path, body=None):
        return requests.patch(API + path, headers=self.h, json=body or {}, timeout=60)

    def rows(self, path, **params):
        body = self.get(path, **params).json()
        rows = body.get("data")
        if isinstance(rows, dict):
            rows = rows.get("results", rows.get("items", []))
        return rows or []


def setup_restaurant(api):
    """Create an inactive-by-default audit table and open a session on it."""
    existing = api.rows("/api/admin/pos/restaurant-tables/", active="all", page_size=100)
    table = next((t for t in existing if t["code"] == "T-AUDIT"), None)
    if table is None:
        created = api.post("/api/admin/pos/restaurant-tables/",
                           {"code": "T-AUDIT", "name": "Audit verification table", "section": "Test",
                            "seats": 2, "is_active": True})
        if created.status_code not in (200, 201):
            return None, None, f"table create HTTP {created.status_code}: {created.text[:120]}"
        table = created.json()["data"]
    elif not table.get("is_active"):
        # a previous verification run deactivated it; bring it back for this run
        api.patch(f"/api/admin/pos/restaurant-tables/{table['id']}/", {"is_active": True})
        table = {**table, "is_active": True}

    sessions = api.rows("/api/admin/pos/restaurant-table-sessions/", status="OPEN", page_size=100)
    session = next((s for s in sessions
                    if s.get("table_code") == "T-AUDIT" and s.get("status") == "OPEN"), None)
    if session is None:
        # A fresh idempotency key per run: a reused key would hand back the
        # session a previous run had already closed.
        key = f"audit-verify-session-{int(time.time())}"
        opened = api.post("/api/admin/pos/restaurant-table-sessions/",
                          {"table_id": table["id"], "covers": 2, "idempotency_key": key})
        if opened.status_code not in (200, 201):
            return table, None, f"session open HTTP {opened.status_code}: {opened.text[:120]}"
        session = opened.json()["data"]
        if session.get("status") != "OPEN":
            return table, None, f"session not open ({session.get('status')})"
    return table, session, ""


def teardown(api, table, session, created_references):
    """Leave nothing behind: the drafts this run created are CANCELLED (never
    deleted — the audit trail keeps them), then the session closes and the audit
    table is deactivated. The server refuses both while a draft is in progress,
    which is exactly the integrity rule being exercised."""
    note = []
    for reference in created_references:
        cancelled = api.post(f"/api/admin/pos/orders/{reference}/status/",
                             {"status": "CANCELLED", "note": "created by the audit verification run; cleaned up"})
        note.append(f"{reference} cancelled={cancelled.status_code}")
    if session and session.get("status") == "OPEN":
        closed = api.post(f"/api/admin/pos/restaurant-table-sessions/{session['reference']}/close/",
                          {"close_note": "audit verification finished"})
        note.append(f"session closed={closed.status_code}")
    if table:
        off = api.patch(f"/api/admin/pos/restaurant-tables/{table['id']}/", {"is_active": False})
        note.append(f"table deactivated={off.status_code}")
    return ", ".join(note)


def counts(api, mode):
    orders = api.rows("/api/admin/pos/orders/", page_size=100, mode=mode, status="DRAFT")
    tickets = api.rows("/api/admin/pos/kitchen-tickets/", page_size=100)
    return orders, tickets


async def press_create_twice(page):
    """One press, then an immediate second press while the first is in flight."""
    submit = page.locator('#pos-create-form button[type="submit"]').first
    await submit.click(no_wait_after=True)
    try:
        await submit.click(no_wait_after=True, timeout=1500)
    except Exception:
        pass  # the in-flight guard may already have blocked/disabled it — that is the point


async def run_mode(pw, session, mode, table_session=None):
    browser = await pw.chromium.launch()
    ctx = await browser.new_context(viewport={"width": 1440, "height": 950})
    await ctx.add_init_script(session.init_script())
    page = await ctx.new_page()
    errors, posts = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("request", lambda r: posts.append(r.method) if "/pos/orders/" in r.url and r.method == "POST" else None)
    await page.goto(BASE + "/dashboard/pos.html", wait_until="networkidle")
    await page.wait_for_timeout(1200)

    api = Api(session)
    orders_before, tickets_before = counts(api, mode)

    await page.select_option("#pos-mode", mode)
    await page.wait_for_timeout(600)
    if mode == "RESTAURANT":
        await page.select_option("#pos-table-session", table_session)
        await page.wait_for_timeout(400)

    for _ in range(2):                                    # two lines on the cart
        await page.select_option("#pos-item", index=1)
        await page.wait_for_timeout(200)
        await page.click("#pos-add-line")
        await page.wait_for_timeout(250)
    cart_before = await page.inner_text("[data-pos-cart]")
    if "No items yet" in cart_before:
        await browser.close()
        return False, "cart did not accept a line"

    await press_create_twice(page)
    await page.wait_for_timeout(5000)

    orders_after, tickets_after = counts(api, mode)
    created_references = [o["reference"] for o in orders_after
                          if o["reference"] not in {x["reference"] for x in orders_before}]
    created = len(created_references)
    new_tickets = len(tickets_after) - len(tickets_before)
    cart_after = await page.inner_text("[data-pos-cart]")
    banner = await page.evaluate(
        "() => { const e = document.querySelector('[data-pos-create-error]'); return e && !e.hidden ? e.textContent.trim() : ''; }")
    
    await page.screenshot(path=artifact(f"ui-08-pos-{mode.lower()}.png"))
    await browser.close()

    run_mode.created_references = created_references
    ok = (created == 1 and new_tickets == 0 and "No items yet" in cart_after
          and len(posts) == 1 and not errors
          # a second press on an emptied cart must be refused, not create an order
          and (not banner or "Add at least one menu item" in banner))
    detail = (f"orders +{created}, POST {len(posts)}, tickets +{new_tickets}, "
              f"cart cleared={'No items yet' in cart_after}, post-clear banner={banner[:48]!r}, "
              f"pageerrors={errors[:1] or 'none'}")
    return ok, detail


async def run_room_service_guard(pw, session):
    """No in-house stay exists here: the mode must refuse, visibly, and create nothing."""
    browser = await pw.chromium.launch()
    ctx = await browser.new_context(viewport={"width": 1440, "height": 950})
    await ctx.add_init_script(session.init_script())
    page = await ctx.new_page()
    await page.goto(BASE + "/dashboard/pos.html", wait_until="networkidle")
    await page.wait_for_timeout(1200)

    api = Api(session)
    orders_before, _ = counts(api, "ROOM_SERVICE")
    await page.select_option("#pos-mode", "ROOM_SERVICE")
    await page.wait_for_timeout(500)
    await page.select_option("#pos-item", index=1)
    await page.wait_for_timeout(200)
    await page.click("#pos-add-line")
    await page.wait_for_timeout(300)
    await press_create_twice(page)
    await page.wait_for_timeout(2500)
    orders_after, tickets_after = counts(api, "ROOM_SERVICE")
    banner = await page.evaluate(
        "() => { const e = document.querySelector('[data-pos-create-error]'); return e && !e.hidden ? e.textContent.trim() : ''; }")
    cart_after = await page.inner_text("[data-pos-cart]")
    # the guest picker must be present and must query the room-service stay search
    await page.fill("#pos-stay-search", "no-such-guest")
    await page.click("#pos-find-stay")
    await page.wait_for_timeout(1200)
    picker_text = await page.inner_text("[data-pos-stay-results]")
    await page.screenshot(path=artifact("ui-08-pos-room_service.png"))
    await browser.close()

    created = len(orders_after) - len(orders_before)
    ok = (created == 0 and "Select an in-house guest" in banner and "No items yet" not in cart_after)
    return ok, (f"orders +{created}, banner={banner[:60]!r}, cart kept={'No items yet' not in cart_after}, "
                f"guest search={'no match' if 'No in-house' in picker_text or 'no ' in picker_text.lower() else picker_text[:40]!r}")


async def main():
    session = Session(MANAGER)
    api = Api(session)
    table, restaurant_session, problem = setup_restaurant(api)
    print(f"· restaurant setup: table={(table or {}).get('code', '—')} "
          f"session={(restaurant_session or {}).get('reference', '—')} {problem}")

    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        created = []
        for mode, label in MODES.items():
            try:
                if mode == "ROOM_SERVICE":
                    ok, detail = await run_room_service_guard(pw, session)
                    record(f"{label}: refuses without an in-house guest and creates nothing", ok, detail)
                elif mode == "RESTAURANT" and not restaurant_session:
                    record(f"{label}: one draft, no duplicate", False, problem or "no open session")
                else:
                    run_mode.created_references = []
                    ok, detail = await run_mode(pw, session, mode,
                                                table_session=(restaurant_session or {}).get("reference"))
                    created.extend(run_mode.created_references)
                    record(f"{label}: one draft, no duplicate, cart cleared, no kitchen ticket", ok, detail)
            except Exception as exc:
                record(f"{label}: one draft, no duplicate", False, f"crashed: {type(exc).__name__}: {exc}")

    print("· cleanup:", teardown(api, table, restaurant_session, created))

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} POS mode checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
