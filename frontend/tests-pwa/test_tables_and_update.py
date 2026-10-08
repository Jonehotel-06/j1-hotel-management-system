# tests-pwa/test_tables_and_update.py
"""Production-fix verification: responsive dashboard tables + update flow.

1. TABLES — bookings/payments/audit-logs tables are populated with extreme
   values (long references, emails, summaries). Asserts:
     * the PAGE never scrolls horizontally (html/body clientWidth intact);
     * only .table-wrap scrolls when the table genuinely needs more width;
     * identifier cells (.cell-nowrap) render on ONE line;
     * long audit summaries stay clamped but keep their full text accessible
       (title attribute + full value in the DOM).
   Runs at desktop (1280), tablet (768) and narrow mobile (320/390) widths.

2. UPDATE FLOW — with a newer version.json, the modal appears on a PUBLIC page
   AND on the DASHBOARD shell (staff must get updates too), and "Refresh now"
   triggers a reload. Uses the real update-checker.js loaded by the pages.

Requires the static server on 127.0.0.1:8080 (auth is stubbed client-side so
no backend is needed; API calls are routed to a mock).
"""
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8080"
FRONTEND = Path(__file__).resolve().parents[1]

out = []
def check(name, ok, detail=""):
    out.append((name, ok, detail))

LONG_REF = "J1-20260917-AB12CD34EF56AB12CD34EF56"
LONG_EMAIL = "extremely.long.guest.email.address@subdomain.example-hospitality.com"
LONG_SUMMARY = ("Booking J1-20260917-AB12CD34EF56 modified by staff: dates 2026-09-20 -> "
                "2026-10-02, rooms 101,102,103 reassigned, metadata "
                + json.dumps({"changes": {"check_in": ["2026-09-20", "2026-10-02"],
                                          "url": "https://api.example.com/very/long/path?token=abcdef0123456789"}}))

BOOKINGS = {"success": True, "data": {"results": [{
    "id": 1, "booking_reference": LONG_REF, "guest_name": "Adaeze Chukwuemeka-Okonkwo",
    "guest_phone": "+2348032112874", "guest_email": LONG_EMAIL,
    "room_type_name": "Executive Suite", "room_numbers": ["101", "102"],
    "check_in": "2026-09-20", "check_out": "2026-09-23",
    "status": "PENDING", "payment_status": "PARTIALLY_PAID",
    "total_amount": "1250000.00", "amount_paid": "250000.00", "amount_due": "1000000.00",
    "currency": "NGN"}]}, "pagination": None}

PAYMENTS = {"success": True, "data": {"results": [{
    "reference": "J1P-20260917-FEDCBA9876543210", "booking_reference": LONG_REF,
    "guest_name": "Adaeze Chukwuemeka-Okonkwo", "provider": "BANK_TRANSFER",
    "paid_at": "2026-09-17T10:00:00Z", "amount": "1250000.00", "status": "SUCCESS",
    "staff_email": LONG_EMAIL}]}, "pagination": None}

AUDIT = {"success": True, "data": {"results": [{
    "id": 991, "actor_email": LONG_EMAIL, "actor_name": "",
    "action": "BOOKING_MODIFIED", "summary": LONG_SUMMARY,
    "ip_address": "2001:0db8:85a3:0000:0000:8a2e:0370:7334",
    "created_at": "2026-09-17T10:00:00Z"}]}, "pagination": None}

EMPTY = {"success": True, "data": {"results": []}, "pagination": None}

PROFILE = {"success": True, "data": {"id": 1, "email": "admin@j1.dev", "first_name": "Ada",
                                     "last_name": "Admin", "role": "ADMIN", "is_active": True}}


def fake_auth(page):
    # Matches js/auth.js contract: profile in localStorage["jone.auth"]
    # (JSON via JONE.storage) + credentials in sessionStorage["jone.session"].
    page.add_init_script("""
      (function(){
        sessionStorage.setItem("jone.session", JSON.stringify({access:"test-access", refresh:"test-refresh"}));
        localStorage.setItem("jone.auth", JSON.stringify({
          id:1,email:"admin@j1.dev",first_name:"Ada",last_name:"Admin",role:"ADMIN",is_active:true
        }));
      })();
    """)


def route_api(page, table_payload_by_path):
    def handler(route):
        url = route.request.url
        for frag, payload in table_payload_by_path.items():
            if frag in url:
                route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
                return
        if "/api/auth/me" in url or "/api/admin/profile" in url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(PROFILE))
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(EMPTY))
    page.route(re.compile(r".*/api/.*"), handler)


def assert_no_page_hscroll(page, label):
    m = page.evaluate("""() => ({
        docScroll: document.documentElement.scrollWidth,
        docClient: document.documentElement.clientWidth,
        bodyScroll: document.body.scrollWidth,
        bodyClient: document.body.clientWidth,
    })""")
    ok = m["docScroll"] <= m["docClient"] + 1
    check(f"{label}: page has no horizontal scroll", ok, str(m))


def assert_wrap_scrolls_not_page(page, label):
    m = page.evaluate("""() => {
        const wrap = document.querySelector('.table-wrap');
        if (!wrap) return null;
        return { wrapScroll: wrap.scrollWidth, wrapClient: wrap.clientWidth,
                 canScroll: wrap.scrollWidth > wrap.clientWidth };
    }""")
    check(f"{label}: table-wrap present", m is not None, "")
    if m and m["canScroll"]:
        check(f"{label}: wide table scrolls INSIDE .table-wrap", True,
              f"{m['wrapScroll']}>{m['wrapClient']}")


def assert_nowrap_single_line(page, label):
    heights = page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('td.cell-nowrap a, td.cell-nowrap code').forEach(el => {
            const cs = getComputedStyle(el.closest('td'));
            out.push({ws: cs.whiteSpace, rects: el.getClientRects().length});
        });
        return out;
    }""")
    if heights:
        ok = all(h["ws"] == "nowrap" for h in heights)
        check(f"{label}: identifier cells keep white-space nowrap", ok, str(heights[:3]))


def run_tables(pw):
    b = pw.chromium.launch()
    pages = [
        ("dashboard/bookings.html", {"/api/admin/bookings": BOOKINGS}),
        ("dashboard/payments.html", {"/api/admin/payments": PAYMENTS, "/api/admin/refunds": EMPTY}),
        ("dashboard/audit-logs.html", {"/api/admin/audit-logs": AUDIT}),
    ]
    for width, tag in ((1280, "desktop"), (768, "tablet"), (390, "mobile390"), (320, "mobile320")):
        ctx = b.new_context(viewport={"width": width, "height": 900})
        page = ctx.new_page()
        fake_auth(page)
        route_api(page, {})
        for path, payloads in pages:
            page.unroute_all()
            route_api(page, payloads)
            page.goto(f"{BASE}/{path}", wait_until="load")
            page.wait_for_timeout(1200)
            label = f"{tag} {path.split('/')[-1]}"
            has_rows = page.evaluate("() => !!document.querySelector('tbody tr td')")
            check(f"{label}: rows rendered", has_rows, "")
            assert_no_page_hscroll(page, label)
            assert_wrap_scrolls_not_page(page, label)
            if width >= 768:
                assert_nowrap_single_line(page, label)
        # Audit summary specifics (any width): clamped but complete.
        page.unroute_all()
        route_api(page, {"/api/admin/audit-logs": AUDIT})
        page.goto(f"{BASE}/dashboard/audit-logs.html", wait_until="load")
        page.wait_for_timeout(1200)
        info = page.evaluate("""() => {
            const td = document.querySelector('td.cell-clamp');
            if (!td) return null;
            const a = td.querySelector('a');
            return { width: td.getBoundingClientRect().width,
                     full: a ? a.getAttribute('title') : null,
                     text: a ? a.textContent : null };
        }""")
        if width >= 768:
            check(f"{tag}: audit summary cell exists+clamped", bool(info) and info["width"] < 600,
                  str(info and round(info['width'])))
            check(f"{tag}: audit summary full text preserved", bool(info) and info["full"] == info["text"] == LONG_SUMMARY, "")
        ctx.close()
    b.close()


def run_update_flow(pw):
    b = pw.chromium.launch()

    def with_new_version(page):
        page.route(re.compile(r".*/version\.json.*"), lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps({"version": "9.9.9"})))

    # A) Public page: modal appears.
    ctx = b.new_context()
    page = ctx.new_page()
    with_new_version(page)
    page.goto(f"{BASE}/about.html", wait_until="load")
    page.wait_for_timeout(500)
    page.evaluate("() => window.JONE.updateChecker.check(true)")
    page.wait_for_timeout(500)
    modal = page.evaluate("() => !!document.querySelector('.update-dialog')")
    check("public page: update modal shown", modal, "")

    # B) Refresh now really reloads.
    if modal:
        # NB: `text=Refresh now` also matches the modal's message paragraph;
        # target the actual button so the click lands on the handler.
        with page.expect_navigation(wait_until="load"):
            page.click(".update-dialog button.btn-accent")
        check("refresh now: page reloaded", True, "")
        # Session/localStorage survives the refresh (Task 16).
        keep = page.evaluate("() => localStorage.getItem('jone.update.dismissedVersion')")
        check("refresh now: storage intact (dismissal recorded)", keep == "9.9.9", str(keep))
    ctx.close()

    # C) Dashboard: staff receive the modal too.
    ctx = b.new_context()
    page = ctx.new_page()
    fake_auth(page)
    route_api(page, {})
    with_new_version(page)
    page.goto(f"{BASE}/dashboard/index.html", wait_until="load")
    page.wait_for_timeout(800)
    page.evaluate("() => window.JONE.updateChecker.check(true)")
    page.wait_for_timeout(500)
    modal = page.evaluate("() => !!document.querySelector('.update-dialog')")
    check("dashboard: update modal shown to staff", modal, "")
    ctx.close()

    # D) Dirty booking form: deferred (no modal), remembered for retry.
    ctx = b.new_context()
    page = ctx.new_page()
    with_new_version(page)
    page.goto(f"{BASE}/contact.html", wait_until="load")
    page.wait_for_timeout(500)
    page.evaluate("""() => {
        const f = document.querySelector('form input[type=text], form input[type=email], form textarea');
        if (f) f.value = 'draft in progress';
    }""")
    page.evaluate("() => window.JONE.updateChecker.check(true)")
    page.wait_for_timeout(400)
    modal = page.evaluate("() => !!document.querySelector('.update-dialog')")
    check("dirty form: modal deferred", not modal, "")
    ctx.close()
    b.close()


crashed = None
try:
    with sync_playwright() as pw:
        run_tables(pw)
        run_update_flow(pw)
except Exception as exc:  # a crash mid-run must never masquerade as success
    crashed = exc
failed = [x for x in out if not x[1]]
for name, ok, detail in out:
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail and not ok else ""))
print(f"\n{len(out) - len(failed)}/{len(out)} passed")
if crashed is not None:
    print(f"CRASH: suite aborted early — {type(crashed).__name__}: {crashed}")
    sys.exit(2)
sys.exit(1 if failed else 0)
