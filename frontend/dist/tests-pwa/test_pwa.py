# tests-pwa/test_pwa.py
"""Browser verification of the J-ONE PWA (Chromium, real service worker)."""
import json, re, sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8080"
PUBLIC_PAGES = ["/index.html", "/rooms.html", "/room-details.html", "/booking.html",
                "/booking-review.html", "/booking-confirmation.html", "/contact.html",
                "/login.html", "/offers.html", "/gallery.html", "/my-bookings.html",
                "/payment-verify.html", "/404.html"]
DASHBOARD_PAGES = ["/dashboard/index.html", "/dashboard/bookings.html",
                   "/dashboard/payments.html", "/dashboard/availability.html",
                   "/dashboard/settings.html"]
STUB = """
if (location.pathname.startsWith('/dashboard/')) {
  localStorage.setItem('jone.auth', JSON.stringify({
    email:'a@b.c', first_name:'Ada', last_name:'Staff', role:'ADMIN', permissions:[]}));
  sessionStorage.setItem('jone.session', JSON.stringify({access:'x.y.z', refresh:'r'}));
}
"""


def stub_api(page):
    def handler(route):
        url = route.request.url
        if "/api/auth/capabilities/" in url:
            body = {"success": True, "data": []}
        elif "/api/auth/token/refresh/" in url:
            body = {"success": True, "data": {"access": "stub-access", "refresh": "stub-refresh"}}
        elif "/api/auth/me" in url or "/api/admin/profile" in url:
            body = {"success": True, "data": {"id": 1, "email": "a@b.c", "first_name": "Ada",
                                              "last_name": "Staff", "role": "ADMIN", "is_active": True}}
        else:
            body = {"success": True, "data": {"results": []}, "pagination": None}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
    page.route(re.compile(r".*/api/.*"), handler)

def goto(pg, url, wait="domcontentloaded"):
    """dev_server.py is a tiny threaded HTTP/1.0 server; occasional connection
    aborts are a server artefact, not a site bug. Retry a couple of times."""
    last = None
    for _ in range(4):
        try:
            return pg.goto(url, wait_until=wait)
        except Exception as e:
            last = e
            pg.wait_for_timeout(400)
    raise last


def settle(pg, ms=250):
    try:
        pg.wait_for_load_state("load")
    except Exception:
        pass
    pg.wait_for_timeout(ms)


results, failures = [], []
def check(name, ok, detail=""):
    results.append((name, ok, detail))
    if not ok: failures.append(name + " :: " + detail)

with sync_playwright() as pw:
    b = pw.chromium.launch(args=["--enable-features=ServiceWorker"])
    ctx = b.new_context(service_workers="allow")
    errors = []
    page = ctx.new_page()
    stub_api(page); page.add_init_script(STUB)
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))

    # --- public pages: no manifest, no install UI, no registration -----------
    for path in PUBLIC_PAGES:
        goto(page, BASE + path)
        settle(page)
        check("NO manifest link on public " + path, page.query_selector('link[rel=manifest]') is None)
        check("NO install controls on public " + path,
              page.evaluate("document.querySelectorAll('[data-pwa-install]').length === 0"))
        check("install disabled on public " + path, page.evaluate("JONE.pwa.installEnabled === false"))
        tc = page.query_selector('meta[name=theme-color]')
        check("theme-color " + path, tc is not None)
        vp = page.eval_on_selector('meta[name=viewport]', "e=>e.content")
        check("viewport " + path, "width=device-width" in vp, vp)
        check("pwa.js loaded " + path, page.evaluate("!!(window.JONE && window.JONE.pwa)"))
    public_reg = page.evaluate("""async () => {
        const root = await navigator.serviceWorker.getRegistration('/');
        const dash = await navigator.serviceWorker.getRegistration('/dashboard/');
        return { root: !!root, dashboard: !!dash, controlled: !!navigator.serviceWorker.controller };
    }""")
    check("public pages register no worker", not public_reg["root"] and not public_reg["dashboard"]
          and not public_reg["controlled"], json.dumps(public_reg))
    root_manifest = page.request.get(BASE + "/manifest.webmanifest")
    check("old root manifest is gone", root_manifest.status == 404, str(root_manifest.status))

    # Legacy migration: a browser still holding the old root registration
    # loses it on the next public page load (the dashboard registration is a
    # separate registration and is created afterwards, below).
    legacy = page.evaluate("""async () => {
        const r = await navigator.serviceWorker.register('/sw.js', {scope: '/'});
        await new Promise(res => setTimeout(res, 800));
        const root = await navigator.serviceWorker.getRegistration('/');
        return { scope: root && root.scope, active: !!(root && root.active) };
    }""")
    check("legacy root registration can be simulated", legacy["active"], json.dumps(legacy))
    goto(page, BASE + "/index.html", "load")
    page.wait_for_timeout(1200)
    legacy_after = page.evaluate("""async () => {
        const root = await navigator.serviceWorker.getRegistration('/');
        return { root: !!root };
    }""")
    check("legacy root registration is removed by public pages", legacy_after["root"] is False,
          json.dumps(legacy_after))

    # --- dashboard pages: the only manifest links -----------------------------
    for path in DASHBOARD_PAGES:
        goto(page, BASE + path)
        settle(page)
        href = page.eval_on_selector('link[rel=manifest]', "e => e.href") if page.query_selector('link[rel=manifest]') else None
        check("manifest link present " + path, href is not None and href.endswith("/dashboard/manifest.webmanifest"), str(href))
        if href:
            r = page.request.get(href)
            check("manifest 200 " + path, r.status == 200, str(r.status))
            check("manifest MIME " + path,
                  "manifest" in (r.headers.get("content-type") or ""), r.headers.get("content-type", ""))
        check("install enabled on dashboard " + path, page.evaluate("JONE.pwa.installEnabled === true"))
        check("pwa.js loaded " + path, page.evaluate("!!(window.JONE && window.JONE.pwa)"))

    # --- manifest content ----------------------------------------------------
    m = json.loads(page.request.get(BASE + "/dashboard/manifest.webmanifest").text())
    check("manifest name", m["name"] == "J-ONE Staff Dashboard", m["name"])
    check("start_url", m["start_url"] == "/dashboard/index.html", m["start_url"])
    check("scope", m["scope"] == "/dashboard/", m["scope"])
    check("display standalone", m["display"] == "standalone")
    sizes = {i["sizes"] for i in m["icons"]}
    check("icon 192", "192x192" in sizes)
    check("icon 512", "512x512" in sizes)
    check("maskable icon", any(i.get("purpose") == "maskable" for i in m["icons"]))
    for i in m["icons"]:
        r = page.request.get(BASE + i["src"])
        check("icon reachable " + i["src"], r.status == 200, str(r.status))

    # --- service worker registration + scope ---------------------------------
    goto(page, BASE + "/dashboard/index.html", "load")
    reg = page.evaluate("""async () => {
        const r = await navigator.serviceWorker.ready;
        return { scope: r.scope, active: !!r.active, state: r.active && r.active.state };
    }""")
    check("sw registered", reg["active"], json.dumps(reg))
    check("sw scope is dashboard only", reg["scope"] == BASE + "/dashboard/", reg["scope"])

    # --- sw controls dashboard pages only ------------------------------------
    for path, expected in [("/index.html", False), ("/rooms.html", False),
                           ("/booking.html", False), ("/dashboard/index.html", True)]:
        goto(page, BASE + path, "load")
        page.wait_for_timeout(300)
        controlled = page.evaluate("!!navigator.serviceWorker.controller")
        check("sw controls " + path + " is " + str(expected), controlled is expected)

    # --- caches: what got stored ---------------------------------------------
    goto(page, BASE + "/dashboard/index.html", "load")
    page.wait_for_timeout(1500)
    cached = page.evaluate("""async () => {
        const names = await caches.keys();
        const out = {};
        for (const n of names) out[n] = (await (await caches.open(n)).keys()).map(r => new URL(r.url).pathname);
        return out;
    }""")
    allurls = [u for v in cached.values() for u in v]
    check("cache versioned", all(n.startswith("jone-v") for n in cached), str(list(cached)))
    check("offline.html precached", "/offline.html" in allurls)
    check("main.css precached", "/css/main.css" in allurls)
    check("NO /api/ cached", not any(u.startswith("/api/") for u in allurls),
          str([u for u in allurls if u.startswith("/api/")]))
    check("dashboard manifest precached", "/dashboard/manifest.webmanifest" in allurls)
    check("NO dashboard HTML cached", not any(u.startswith("/dashboard/") and u.endswith(".html") for u in allurls),
          str([u for u in allurls if u.startswith("/dashboard/") and u.endswith(".html")]))

    # --- dashboard navigation must NOT enter the pages cache -----------------
    goto(page, BASE + "/dashboard/bookings.html", "load")
    page.wait_for_timeout(800)
    goto(page, BASE + "/index.html", "load")
    dash_cached = page.evaluate("""async () => {
        const names = await caches.keys(); const out = [];
        for (const n of names) for (const r of await (await caches.open(n)).keys())
            if (new URL(r.url).pathname.startsWith('/dashboard/') && new URL(r.url).pathname.endsWith('.html')) out.push(r.url);
        return out;
    }""")
    check("dashboard never cached after visit", dash_cached == [], str(dash_cached))

    # --- API requests are never served from cache ----------------------------
    api_from_sw = page.evaluate("""async () => {
        const names = await caches.keys();
        for (const n of names) {
            const m = await (await caches.open(n)).match('/api/rooms/availability/');
            if (m) return true;
        }
        return false;
    }""")
    check("availability not in cache", api_from_sw is False)

    # --- offline behaviour ---------------------------------------------------
    # Public pages are not worker-controlled; booking/payment still fail closed
    # through the connectivity guard and the never-cached API layer below.
    goto(page, BASE + "/index.html", "load")
    page.wait_for_timeout(600)
    ctx.set_offline(True)

    # offline booking / payment are blocked with a clear message.
    # NOTE: navigate first, THEN go offline, so navigator.onLine is current —
    # this mirrors a real guest who loses signal while on the page.
    ctx.set_offline(False)
    goto(page, BASE + "/index.html", "load")
    page.wait_for_timeout(500)
    ctx.set_offline(True)
    page.wait_for_timeout(300)
    blocked = page.evaluate("() => JONE.pwa.requireOnline('booking') === false")
    check("offline booking blocked", blocked)
    pay_blocked = page.evaluate("() => JONE.pwa.requireOnline('payment') === false")
    check("offline payment blocked", pay_blocked)
    msg = page.evaluate("() => document.querySelector('.toast .toast-message')?.textContent || ''")
    check("offline message is user-friendly (no stack trace)",
          "internet connection" in msg.lower() and "Error" not in msg, msg)

    # THE critical guarantee: a payment/booking API call offline must FAIL,
    # never return a cached response that could look like success. Remove the
    # dashboard auth stub first so these calls hit the real (offline) network.
    try:
        page.unroute_all(behavior="wait")
    except Exception:
        pass
    api_offline = page.evaluate("""async () => {
        const out = {};
        try { await window.API.initPayment('J1-TEST'); out.pay = 'RETURNED'; }
        catch (e) { out.pay = 'failed:' + e.status; out.payMsg = e.message; }
        try { await window.API.verifyPayment('ref-test'); out.verify = 'RETURNED'; }
        catch (e) { out.verify = 'failed:' + e.status; }
        try { await window.API.createBooking({}); out.book = 'RETURNED'; }
        catch (e) { out.book = 'failed:' + e.status; }
        return out;
    }""")
    check("offline initPayment fails (no false success)",
          api_offline["pay"].startswith("failed"), json.dumps(api_offline))
    check("offline verifyPayment fails (no false success)",
          api_offline["verify"].startswith("failed"), json.dumps(api_offline))
    check("offline createBooking fails (never queued)",
          api_offline["book"].startswith("failed"), json.dumps(api_offline))
    check("offline API error message is plain language",
          "connection" in api_offline.get("payMsg", "").lower(), api_offline.get("payMsg", ""))

    # offline dashboard shows a connection message, not blank
    goto(page, BASE + "/dashboard/reports.html", "domcontentloaded")
    check("offline dashboard -> branded offline page, not blank",
          "offline" in page.content().lower() and "J-ONE" in page.content())

    ctx.set_offline(False)

    # --- install UI ----------------------------------------------------------
    # Guests get no install control even if a prompt event is simulated.
    goto(page, BASE + "/index.html", "load")
    page.wait_for_timeout(500)
    guest_ctrl = page.evaluate("""async () => {
        const e = new Event('beforeinstallprompt');
        e.prompt = () => {}; e.userChoice = Promise.resolve({outcome:'accepted'});
        window.dispatchEvent(e);
        await new Promise(r => setTimeout(r, 100));
        return document.querySelectorAll('[data-pwa-install]').length;
    }""")
    check("public site never shows install controls", guest_ctrl == 0, str(guest_ctrl))

    goto(page, BASE + "/dashboard/index.html", "load")
    page.wait_for_timeout(500)
    ctrl = page.evaluate("""() => {
        const els = [...document.querySelectorAll('[data-pwa-install]')];
        return { count: els.length, hiddenAll: els.every(e => e.hidden) };
    }""")
    check("dashboard install control injected", ctrl["count"] >= 1, json.dumps(ctrl))
    check("install controls hidden w/o prompt", ctrl["hiddenAll"], json.dumps(ctrl))
    # simulate beforeinstallprompt (headless Chromium does not fire it)
    shown = page.evaluate("""async () => {
        const e = new Event('beforeinstallprompt');
        e.prompt = () => {}; e.userChoice = Promise.resolve({outcome:'accepted'});
        window.dispatchEvent(e);
        await new Promise(r => setTimeout(r, 100));
        const els = [...document.querySelectorAll('[data-pwa-install]')];
        return els.length && els.every(e => !e.hidden);
    }""")
    check("install controls reveal on prompt", shown)
    a11y = page.evaluate("""() => {
        const els = [...document.querySelectorAll('[data-pwa-install]')];
        return els.every(e => e.tagName === 'BUTTON' && e.textContent.trim().length > 0 && !e.querySelector('img'));
    }""")
    check("install controls are labelled buttons", a11y)
    no_emoji = page.evaluate("""() => {
        const els = [...document.querySelectorAll('[data-pwa-install]')];
        return els.every(e => e.querySelector('svg') && !/\\p{Extended_Pictographic}/u.test(e.textContent));
    }""")
    check("install controls use SVG icons, no emoji", no_emoji)

    # --- themes still work ---------------------------------------------------
    for t in ("light", "dark"):
        page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", t)
        bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        check("theme renders " + t, bg not in ("", "rgba(0, 0, 0, 0)"), bg)

    # --- no JS errors introduced --------------------------------------------
    real = [e for e in errors if "Failed to load resource" not in e and "net::" not in e]
    check("no page JS errors", not real, str(real[:5]))

    b.close()

print("\n".join(f"{'PASS' if ok else 'FAIL'}  {n}{('  -> ' + d) if (d and not ok) else ''}"
                for n, ok, d in results))
print(f"\n{sum(1 for _,ok,_ in results if ok)}/{len(results)} passed")
if failures:
    print("\nFAILURES:")
    for f in failures: print("  " + f)
sys.exit(1 if failures else 0)
