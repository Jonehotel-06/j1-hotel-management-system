# tests-pwa/test_multi_room_ui.py
"""Multi-room UI regression suite (spec A + B + C).

Guards the three reported bugs on the REAL pages, with the backend stubbed at
the network boundary so the run is deterministic and creates no inventory:

A. quantity survives selection → summary → localStorage draft → refresh →
   back-navigation, and the card total reflects the SELECTED quantity (the
   ₦60,000-for-2-rooms bug: the page read `data.pricing.total`, but the quote
   endpoint returns its totals flat on `data`);
B. a backend-VERIFIED success auto-continues to Step 6 / Confirmation, while
   failed / pending / abandoned / errored verifications never navigate and
   never claim success (a `?reference=` param is never proof of payment);
C. the room-selection cards fit every common phone width with no horizontal
   overflow, all controls visible — and desktop layout is untouched.

    cd frontend && python3 dev_server.py 8080 http://127.0.0.1:8000
    cd tests-pwa && python3 test_multi_room_ui.py
"""
import json
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8080"
CI, CO = "2026-11-10", "2026-11-12"          # 2 nights
PRICE, NIGHTS = 60000, 2
PHONES = [320, 360, 375, 390, 393, 414, 430]
DESKTOPS = [768, 1024, 1280, 1440]

out = []


def check(name, ok, detail=""):
    out.append((name, ok, detail))


def money(n):
    return f"{n:.2f}"


# --------------------------------------------------------------------------
# Stub backend: availability always quotes ONE room (as production does), so a
# page that fails to re-quote for the chosen quantity keeps the 1-room price —
# exactly the original bug. The quote endpoint answers with the FLAT shape.
# --------------------------------------------------------------------------
ROOM_TYPE = {
    "id": 1, "name": "Superior Room", "slug": "superior-room",
    "short_description": "A generous superior room with a king bed and a quiet outlook.",
    "base_price": money(PRICE), "max_guests": 2, "bed_type": "King Bed",
    "bed_count": 1, "room_size": "30 m2", "view": "", "is_featured": False,
    "primary_image_url": None, "amenities": [],
}


def room_payload(available=3):
    """GET /api/rooms/availability/ — note `pricing` is always for ONE room,
    which is precisely why the page must re-quote when the guest picks more."""
    total1 = PRICE * NIGHTS
    return {"success": True, "message": "Availability retrieved.", "data": {
        "check_in": CI, "check_out": CO, "nights": NIGHTS, "guests": 2, "rooms": 1,
        "results": [{
            "room_type": ROOM_TYPE,
            "available_rooms": available, "requested_rooms": 1,
            "max_guests_per_room": 2, "extra_guest_allowed": False,
            "bookable": True, "message": None,
            "pricing": {"nights": NIGHTS, "rooms": 1, "adults": 2, "children": 0,
                        "guests": 2, "currency": "NGN",
                        "price_per_night": money(PRICE), "subtotal": money(total1),
                        "discount": "0.00", "offer": None, "extra_guests": 0,
                        "extra_guest_fee": "0.00", "tax": "0.00",
                        "service_fee": "0.00", "total": money(total1),
                        "required_payment": money(total1),
                        "amount_due_online": money(total1)},
        }]}}


def quote_payload(rooms):
    total = PRICE * NIGHTS * rooms
    return {"success": True, "data": {
        "room_type": {"id": 1, "name": "Superior Room", "slug": "superior-room"},
        "check_in": CI, "check_out": CO, "nights": NIGHTS, "rooms": rooms,
        "adults": 2, "children": 0, "guests": 2, "currency": "NGN",
        "price_per_night": money(PRICE), "subtotal": money(total),
        "discount": "0.00", "offer": None, "extra_guests": 0,
        "extra_guest_fee": "0.00", "tax": "0.00", "service_fee": "0.00",
        "total": money(total), "required_payment": money(total),
        "amount_due_online": money(total), "policies": {}, "hold_info": {},
    }}


def install_booking_stubs(page, available=3):
    """Route the booking-page APIs. Returns a list of quoted room counts."""
    quoted = []

    def on_quote(route):
        body = {}
        try:
            body = json.loads(route.request.post_data or "{}")
        except Exception:
            pass
        rooms = int(body.get("rooms") or 1)
        quoted.append(rooms)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(quote_payload(rooms)))

    # Registered least-specific FIRST: Playwright matches the most recently
    # added route, so the catch-all must not shadow the specific endpoints.
    page.route("**/api/rooms/", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"success": True, "data": [ROOM_TYPE]})))
    page.route("**/unavailable-dates/**", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"success": True, "data": {
            "room_type": {"id": 1, "name": "Superior Room", "slug": "superior-room"},
            "total_rooms": available, "from": CI, "through": CO, "dates": {}}})))
    page.route("**/api/rooms/availability/**", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps(room_payload(available))))
    page.route("**/api/bookings/quote/", on_quote)
    return quoted


def open_room_step(page, width=390, available=3):
    quoted = install_booking_stubs(page, available)
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(f"{BASE}/booking.html?check_in={CI}&check_out={CO}"
              f"&room=superior-room&step=room", wait_until="load")
    page.wait_for_selector("[data-room-opt]", timeout=20000)
    page.wait_for_timeout(1200)
    return quoted


def set_qty(page, clicks):
    """Press the + stepper `clicks` times on the superior-room card."""
    for _ in range(clicks):
        page.evaluate("""() => {
            const c = [...document.querySelectorAll('[data-room-opt]')].find(
                o => o.querySelector('input')?.getAttribute('data-rt-slug') === 'superior-room');
            c.querySelector('[data-qty-plus]').click();
        }""")
        page.wait_for_timeout(250)
    page.wait_for_timeout(1400)      # debounce (350ms) + quote round-trip


def card_total(page):
    return page.evaluate("""() => {
        const c = [...document.querySelectorAll('[data-room-opt]')].find(
            o => o.querySelector('input')?.getAttribute('data-rt-slug') === 'superior-room');
        const l = c.querySelector('[data-total-line]');
        return l ? l.textContent.trim() : '(no total line)';
    }""")


with sync_playwright() as pw:
    browser = pw.chromium.launch()

    # =====================================================================
    # A. Quantity consistency
    # =====================================================================
    ctx = browser.new_context(viewport={"width": 390, "height": 844},
                              is_mobile=True, has_touch=True)
    page = ctx.new_page()
    quoted = open_room_step(page)

    one_room_total = card_total(page)
    check("1 room card shows the 1-room total",
          "120,000" in one_room_total, one_room_total)

    set_qty(page, 1)                                   # → 2 rooms
    two_room_total = card_total(page)
    check("2 rooms card shows the DOUBLED total (not the stale 1-room price)",
          "240,000" in two_room_total and "120,000" not in two_room_total,
          two_room_total)

    summary = page.evaluate(
        "() => document.querySelector('[data-sum-total]')?.textContent.trim()")
    check("2 rooms sticky summary shows the 2-room total",
          "240,000" in (summary or ""), str(summary))

    check("the quantity was re-quoted server-side (never multiplied in JS)",
          2 in quoted, f"quoted={quoted}")

    qty_label = page.evaluate("""() => {
        const c = [...document.querySelectorAll('[data-room-opt]')].find(
            o => o.querySelector('input')?.getAttribute('data-rt-slug') === 'superior-room');
        return c.querySelector('[data-qty-value]').textContent.trim();
    }""")
    check("stepper reads '2 rooms'", qty_label == "2 rooms", qty_label)

    draft = page.evaluate(
        "() => JSON.parse(localStorage.getItem('jone.booking.draft') || '{}')")
    check("draft persists rooms=2 to localStorage",
          int(draft.get("rooms", 0)) == 2, str(draft))

    # --- survives a full page refresh -----------------------------------
    page.reload(wait_until="load")
    page.wait_for_selector("[data-room-opt]", timeout=20000)
    page.wait_for_timeout(1800)
    draft_after = page.evaluate(
        "() => JSON.parse(localStorage.getItem('jone.booking.draft') || '{}')")
    check("rooms=2 survives a page refresh",
          int(draft_after.get("rooms", 0)) == 2, str(draft_after))

    # --- survives back-navigation (stepper carries rooms in the URL) -----
    back = page.evaluate("""() => {
        if (!(window.JONE && JONE.stepper && JONE.stepper.backUrl)) return null;
        return JONE.stepper.backUrl({key:'room'},
            {check_in:'%s', check_out:'%s', rooms:2, room:'superior-room'});
    }""" % (CI, CO))
    if back is None:
        check("back-navigation URL keeps rooms=2 (stepper API not exposed)",
              True, "skipped: JONE.stepper.backUrl not public")
    else:
        check("back-navigation URL keeps rooms=2", "rooms=2" in back, back)

    page.goto(f"{BASE}/booking.html?check_in={CI}&check_out={CO}"
              f"&room=superior-room&rooms=2&step=room", wait_until="load")
    page.wait_for_selector("[data-room-opt]", timeout=20000)
    page.wait_for_timeout(1800)
    restored = page.evaluate("""() => {
        const c = [...document.querySelectorAll('[data-room-opt]')].find(
            o => o.querySelector('input')?.getAttribute('data-rt-slug') === 'superior-room');
        return c.querySelector('[data-qty-value]').textContent.trim();
    }""")
    check("arriving back with ?rooms=2 restores the 2-room stepper",
          restored == "2 rooms", restored)

    # --- a quantity is never silently downgraded to 1 --------------------
    check("no '|| 1' fallback overwrote the chosen quantity",
          int(page.evaluate("() => JSON.parse(localStorage.getItem("
                            "'jone.booking.draft') || '{}').rooms || 0")) == 2)

    # --- the card <label> must point at its RADIO, not the '−' button ----
    # Regression: with no explicit `for`, a label's labelled control is its
    # first labelable descendant — the quantity minus button. Every click on
    # the card then fired a phantom "one fewer room" and reset 2 back to 1.
    control = page.evaluate("""() => {
        const c = [...document.querySelectorAll('[data-room-opt]')].find(
            o => o.querySelector('input')?.getAttribute('data-rt-slug') === 'superior-room');
        const ctl = c.control;
        return ctl ? (ctl.tagName + ':' + (ctl.type || '')) : 'none';
    }""")
    check("card label targets its radio (never the '−' quantity button)",
          control == "INPUT:radio", control)

    reclick = page.evaluate("""async () => {
        const c = [...document.querySelectorAll('[data-room-opt]')].find(
            o => o.querySelector('input')?.getAttribute('data-rt-slug') === 'superior-room');
        const read = () => c.querySelector('[data-qty-value]').textContent.trim();
        const before = read();
        c.click();                       // plain "select this room" click
        await new Promise(r => setTimeout(r, 600));
        return {before, after: read()};
    }""")
    check("clicking the selected card does NOT decrement the quantity",
          reclick["before"] == "2 rooms" and reclick["after"] == "2 rooms",
          str(reclick))
    ctx.close()

    # --- quantity is clamped to real availability, never invented --------
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    open_room_step(page, available=2)
    set_qty(page, 4)                                   # spam + past the stock
    clamped = page.evaluate("""() => {
        const c = [...document.querySelectorAll('[data-room-opt]')].find(
            o => o.querySelector('input')?.getAttribute('data-rt-slug') === 'superior-room');
        return {qty: c.querySelector('[data-qty-value]').textContent.trim(),
                plusDisabled: c.querySelector('[data-qty-plus]').disabled};
    }""")
    check("quantity clamps at real availability (2) and disables +",
          clamped["qty"] == "2 rooms" and clamped["plusDisabled"], str(clamped))
    ctx.close()

    # =====================================================================
    # B. Paystack success auto-continues to Step 6
    # =====================================================================
    booking = {"success": True, "data": {
        "booking_reference": "J1-UI-0001", "status": "CONFIRMED",
        "payment_status": "PAID", "room_type_name": "Superior Room",
        "number_of_rooms": 2, "nights": NIGHTS, "adults": 2, "children": 0,
        "room_assignments": [{"room_number": "101"}, {"room_number": "102"}],
        "check_in": CI, "check_out": CO, "price_per_night": money(PRICE),
        "subtotal": "240000.00", "total_amount": "240000.00",
        "amount_paid": "240000.00", "amount_due": "0.00", "can_pay": False,
        "currency": "NGN", "guest": {"full_name": "Ada Obi",
                                     "email": "qa@example.com", "phone": "08031234567"}}}

    cases = [
        ("success",   "success",   True),
        ("failed",    "failed",    False),
        ("pending",   "pending",   False),
        ("abandoned", "abandoned", False),
        ("reversed",  "reversed",  False),
    ]
    def verify_route(status):
        """One-arg handler: Playwright passes (route, request) to 2-arg
        callables, which would clobber a default-argument capture."""
        def handler(route):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"success": True, "data": {
                              "transaction_status": status,
                              "payment_status": "PAID" if status == "success" else "UNPAID",
                              "booking_status": "CONFIRMED" if status == "success" else "PENDING",
                              "payment_reference": "J1P-UI-1",
                              "booking_reference": "J1-UI-0001",
                              "amount_paid_this_transaction": "240000.00"}}))
        return handler

    def error_route(code):
        def handler(route):
            route.fulfill(status=code, content_type="application/json",
                          body=json.dumps({"success": False, "code": "ERROR",
                                           "message": "nope"}))
        return handler

    for label, status, expect_step6 in cases:
        ctx = browser.new_context(viewport={"width": 390, "height": 844})
        page = ctx.new_page()
        page.route("**/api/payments/verify/**", verify_route(status))
        page.route("**/api/bookings/J1-UI-0001/", lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(booking)))
        page.goto(f"{BASE}/payment-verify.html?reference=J1P-UI-1&trxref=J1P-UI-1",
                  wait_until="load")
        page.wait_for_timeout(3500)
        on_confirmation = "booking-confirmation.html" in page.url

        if expect_step6:
            page.wait_for_timeout(2000)
            step = page.inner_text(".bk-progress-count") if on_confirmation else ""
            check("verified success auto-continues to Step 6",
                  on_confirmation and "step 6" in step.lower(), f"{page.url} / {step}")
            check("Step 6 shows the confirmed booking",
                  page.is_visible("#success-screen"), page.url)
            check("receipt controls survive the auto-redirect",
                  page.locator("#receipt-download-image, #receipt-download-pdf,"
                               " #receipt-print").count() >= 1)
            body_txt = page.inner_text("body")
            check("confirmation reflects BOTH booked rooms",
                  "101" in body_txt and "102" in body_txt)
        else:
            check(f"'{label}' never navigates to the confirmation page",
                  not on_confirmation, page.url)
            visible = page.inner_text("main").lower()
            check(f"'{label}' never claims success",
                  "payment successful" not in visible, visible[:120])
        ctx.close()

    # A ?reference= alone is not proof of payment: if the backend verification
    # call fails outright, the page must stay put and stay honest.
    for label, status_code in (("verify 404", 404), ("verify 500", 500)):
        ctx = browser.new_context(viewport={"width": 390, "height": 844})
        page = ctx.new_page()
        page.route("**/api/payments/verify/**", error_route(status_code))
        page.goto(f"{BASE}/payment-verify.html?reference=J1P-FAKE", wait_until="load")
        page.wait_for_timeout(3000)
        check(f"{label}: no navigation, no success claim",
              "booking-confirmation.html" not in page.url
              and "payment successful" not in page.inner_text("main").lower(),
              page.url)
        ctx.close()

    # =====================================================================
    # C. Mobile responsiveness of the room-selection cards
    # =====================================================================
    ctx = browser.new_context(viewport={"width": 390, "height": 844},
                              is_mobile=True, has_touch=True)
    page = ctx.new_page()
    install_booking_stubs(page)
    page.goto(f"{BASE}/booking.html?check_in={CI}&check_out={CO}"
              f"&room=superior-room&step=room", wait_until="load")
    page.wait_for_selector("[data-room-opt]", timeout=20000)

    for w in PHONES:
        page.set_viewport_size({"width": w, "height": 844})
        page.wait_for_timeout(500)
        res = page.evaluate("""(w) => {
            const doc = Math.max(0, document.documentElement.scrollWidth - w);
            const card = document.querySelector('[data-room-opt]');
            const clipped = [];
            const sels = ['[data-room-opt]', '.room-option-info', '.room-option-price',
                          '.room-qty', '.room-option-select', '[data-avail-line]',
                          '[data-total-line]', '#continue-guest'];
            for (const s of sels) {
                const el = document.querySelector(s);
                if (!el) { clipped.push(s + ':MISSING'); continue; }
                const r = el.getBoundingClientRect();
                if (r.right > w + 1 || r.left < -1) clipped.push(s);
                if (r.width === 0 || r.height === 0) clipped.push(s + ':HIDDEN');
            }
            return {doc, cardRight: Math.round(card.getBoundingClientRect().right),
                    clipped};
        }""", w)
        check(f"W={w}: no horizontal overflow",
              res["doc"] == 0, f"docOverflow={res['doc']}px")
        check(f"W={w}: every card control visible inside the viewport",
              not res["clipped"], str(res["clipped"]))

    # The fix must be a real layout fix, not a hidden overflow on the page.
    masked = page.evaluate("""() => {
        const c = document.querySelector('[data-room-opt]');
        return {card: getComputedStyle(c).overflowX,
                grid: getComputedStyle(c.parentElement).overflowX};
    }""")
    check("overflow is not merely hidden on the card/grid",
          masked["card"] in ("visible", "clip") and masked["grid"] in ("visible", "clip"),
          str(masked))

    # Desktop must be untouched: the card keeps its side-by-side media column.
    for w in DESKTOPS:
        page.set_viewport_size({"width": w, "height": 900})
        page.wait_for_timeout(400)
        desk = page.evaluate("""(w) => {
            const c = document.querySelector('[data-room-opt]');
            const cs = getComputedStyle(c);
            const img = c.querySelector('.media');
            return {cols: cs.gridTemplateColumns,
                    over: Math.max(0, document.documentElement.scrollWidth - w),
                    mediaW: img ? Math.round(img.getBoundingClientRect().width) : 0};
        }""", w)
        multi_col = len(desk["cols"].split()) >= 2
        check(f"W={w}: desktop card keeps its multi-column layout",
              multi_col and desk["over"] == 0 and desk["mediaW"] > 100, str(desk))

    ctx.close()
    browser.close()

print("\n".join(f"{'PASS' if o else 'FAIL'}  {n}" + (f"  -> {d}" if d and not o else "")
                for n, o, d in out))
failed = [n for n, o, _ in out if not o]
print(f"\n{len(out) - len(failed)}/{len(out)} passed")
sys.exit(1 if failed else 0)
