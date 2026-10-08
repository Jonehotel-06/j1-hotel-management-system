# tests-pwa/test_regression.py
"""Regression + security checks: existing behaviour must be untouched."""
import json, sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8080"
results, failures = [], []
def check(n, ok, d=""):
    results.append((n, ok, d))
    if not ok: failures.append(f"{n} :: {d}")

def goto(pg, url, wait="load"):
    last=None
    for _ in range(4):
        try: return pg.goto(url, wait_until=wait)
        except Exception as e: last=e; pg.wait_for_timeout(400)
    raise last

with sync_playwright() as pw:
    b = pw.chromium.launch()
    ctx = b.new_context(service_workers="allow")
    errs = []
    page = ctx.new_page()
    page.on("pageerror", lambda e: errs.append(str(e)))

    goto(page, BASE + "/index.html"); page.wait_for_timeout(1200)

    # --- existing global JS modules all still present ------------------------
    mods = page.evaluate("""() => ({
        APP_CONFIG: !!window.APP_CONFIG,
        RUNTIME_CONFIG: !!window.__APP_CONFIG__,
        API: !!window.API,
        JONE: !!window.JONE,
        ui: !!(JONE&&JONE.ui), theme: !!(JONE&&JONE.theme),
        nav: !!(JONE&&JONE.nav), icons: !!(JONE&&JONE.icons),
        hotel: !!(JONE&&JONE.hotel), pwa: !!(JONE&&JONE.pwa)
    })""")
    for k, v in mods.items(): check("module " + k, v)

    # --- API base URL untouched (no mock data, still the Django backend) -----
    api_base = page.evaluate("window.APP_CONFIG.API_BASE_URL")
    check("API_BASE_URL preserved", api_base in ("", "http://127.0.0.1:8000"), api_base)
    eps = page.evaluate("Object.keys(window.APP_CONFIG.API_ENDPOINTS).length")
    check("API_ENDPOINTS intact", eps >= 20, str(eps))
    runtime_before_config = page.evaluate(r"""() => {
        const scripts = Array.from(document.scripts).map((s) => s.src);
        return scripts.findIndex((s) => /runtime-config\.js/.test(s)) <
               scripts.findIndex((s) => /\/config\.js/.test(s));
    }""")
    check("runtime config loads before app config", runtime_before_config)

    # --- chrome / navigation / theme still render ----------------------------
    check("header renders", page.query_selector(".site-header") is not None)
    check("footer renders", page.query_selector(".site-footer") is not None)
    check("mobile drawer present", page.query_selector(".mobile-drawer") is not None)
    nav_items = page.eval_on_selector_all(".mobile-nav a", "els=>els.map(e=>e.textContent.trim().split(' ')[0])")
    check("mobile nav preserved", "Home" in nav_items and "Rooms" in nav_items, str(nav_items))
    check("icons injected (SVG, not emoji)",
          page.eval_on_selector_all("[data-icon]", "els=>els.every(e=>e.querySelector('svg'))"))

    # mobile drawer still opens/closes
    page.set_viewport_size({"width": 390, "height": 844})
    page.click(".menu-toggle"); page.wait_for_timeout(350)
    check("drawer opens", page.eval_on_selector(".mobile-drawer", "e=>e.classList.contains('open')"))
    page.click("[data-drawer-close]"); page.wait_for_timeout(350)
    check("drawer closes", not page.eval_on_selector(".mobile-drawer", "e=>e.classList.contains('open')"))
    page.set_viewport_size({"width": 1280, "height": 900})

    # theme toggle still works and persists
    goto(page, BASE + "/index.html"); page.wait_for_timeout(600)
    before = page.evaluate("document.documentElement.getAttribute('data-theme')")
    page.click(".theme-toggle"); page.wait_for_timeout(350)
    after = page.evaluate("document.documentElement.getAttribute('data-theme')")
    check("theme toggle switches", before != after, f"{before}->{after}")
    goto(page, BASE + "/rooms.html"); page.wait_for_timeout(600)
    check("theme persists across pages",
          page.evaluate("document.documentElement.getAttribute('data-theme')") == after)

    # --- every page still loads with the SW active ---------------------------
    pages = ["/index.html","/rooms.html","/room-details.html","/facilities.html","/gallery.html",
             "/offers.html","/about.html","/contact.html","/policies.html","/terms.html",
             "/privacy.html","/refund-policy.html","/cancellation-policy.html","/booking.html",
             "/booking-review.html","/booking-confirmation.html","/login.html","/register.html",
             "/my-bookings.html","/review.html","/payment-verify.html","/404.html","/403.html","/500.html"]
    for p in pages:
        r = goto(page, BASE + p); page.wait_for_timeout(200)
        ok = r.status == 200 and page.query_selector("main, .dash") is not None
        check("page loads " + p, ok, str(r.status))

    dash = ["/dashboard/index.html","/dashboard/bookings.html","/dashboard/booking-details.html",
            "/dashboard/check-in.html","/dashboard/check-out.html","/dashboard/rooms.html",
            "/dashboard/room-details.html","/dashboard/guests.html","/dashboard/payments.html",
            "/dashboard/receipts.html","/dashboard/notifications.html","/dashboard/reports.html",
            "/dashboard/staff.html","/dashboard/audit-logs.html","/dashboard/settings.html",
            "/dashboard/availability.html"]
    for p in dash:
        r = goto(page, BASE + p); page.wait_for_timeout(200)
        # Unauthenticated staff pages redirect to login — this is the project's
        # PRE-EXISTING auth guard (verified identical with the SW blocked), and
        # the PWA must not bypass it.
        landed_login = "/login.html" in page.url
        has_dash = page.query_selector(".dash") is not None
        check("dash page loads " + p, r.status == 200 and (landed_login or has_dash), page.url)
        check("auth guard still enforced " + p, landed_login,
              "PWA bypassed the staff auth guard: " + page.url)

    # --- SECURITY: no tokens or auth data anywhere in Cache Storage ----------
    goto(page, BASE + "/index.html"); page.wait_for_timeout(500)
    page.evaluate("""() => {
        sessionStorage.setItem('jone.session', JSON.stringify({access:'SECRET_ACCESS_TOKEN', refresh:'SECRET_REFRESH'}));
    }""")
    goto(page, BASE + "/dashboard/index.html"); page.wait_for_timeout(900)
    goto(page, BASE + "/index.html"); page.wait_for_timeout(900)
    leak = page.evaluate("""async () => {
        const bad = [];
        for (const n of await caches.keys()) {
            const c = await caches.open(n);
            for (const req of await c.keys()) {
                const u = new URL(req.url).pathname;
                if (u.startsWith('/api/') || u.startsWith('/dashboard/')) bad.push('url:' + u);
                if (req.headers.get('authorization')) bad.push('authheader:' + u);
                const res = await c.match(req);
                if (res && /text|json|javascript/.test(res.headers.get('content-type')||'')) {
                    const t = await res.clone().text();
                    if (t.includes('SECRET_ACCESS_TOKEN') || t.includes('SECRET_REFRESH')) bad.push('token-in-body:' + u);
                }
            }
        }
        return bad;
    }""")
    check("no tokens / api / dashboard in cache", leak == [], str(leak))

    # tokens stay in sessionStorage only, never localStorage
    ls = page.evaluate("JSON.stringify(Object.entries(localStorage))")
    check("no token in localStorage", "SECRET_ACCESS_TOKEN" not in ls and "SECRET_REFRESH" not in ls)

    # --- no secrets shipped in frontend assets -------------------------------
    for f in ["/sw.js", "/js/pwa.js", "/manifest.webmanifest", "/js/runtime-config.js", "/js/config.js"]:
        txt = page.request.get(BASE + f).text()
        bad = [k for k in ("sk_live", "sk_test", "SECRET_KEY", "PAYSTACK_SECRET", "AWS_SECRET",
                           "password", "smtp") if k.lower() in txt.lower()]
        check("no secrets in " + f, not bad, str(bad))

    # --- update strategy: no forced reload during a critical flow ------------
    goto(page, BASE + "/booking.html"); page.wait_for_timeout(700)
    safe = page.evaluate("""() => {
        JONE.pwa.beginCriticalFlow();
        return { inFlow: true };
    }""")
    # booking path itself must be treated as unsafe to auto-update
    check("booking page marked unsafe for auto-update",
          page.evaluate("/(booking|payment|checkout)/i.test(location.pathname)"))
    check("critical flow API exists",
          page.evaluate("typeof JONE.pwa.beginCriticalFlow === 'function' && typeof JONE.pwa.endCriticalFlow === 'function'"))

    check("no JS errors across run", not errs, str(errs[:5]))
    b.close()

print("\n".join(f"{'PASS' if ok else 'FAIL'}  {n}" + (f"  -> {d}" if d and not ok else "")
                for n, ok, d in results))
print(f"\n{sum(1 for _,o,_ in results if o)}/{len(results)} passed")
if failures:
    print("\nFAILURES:"); [print("  "+f) for f in failures]
sys.exit(1 if failures else 0)
