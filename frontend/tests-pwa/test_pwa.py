# tests-pwa/test_pwa.py
"""Browser verification of the J-ONE PWA (Chromium, real service worker)."""
import json, sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8080"
PAGES = ["/index.html", "/rooms.html", "/room-details.html", "/booking.html",
         "/booking-review.html", "/booking-confirmation.html", "/contact.html",
         "/login.html", "/offers.html", "/gallery.html", "/my-bookings.html",
         "/payment-verify.html", "/404.html", "/dashboard/index.html",
         "/dashboard/bookings.html", "/dashboard/payments.html",
         "/dashboard/availability.html", "/dashboard/settings.html"]

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
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))

    # --- manifest reachable + parsed from every page depth -------------------
    for path in PAGES:
        goto(page, BASE + path)
        settle(page)
        href = page.eval_on_selector('link[rel=manifest]', "e => e.href") if page.query_selector('link[rel=manifest]') else None
        check("manifest link present " + path, href is not None, str(href))
        if href:
            r = page.request.get(href)
            check("manifest 200 " + path, r.status == 200, str(r.status))
            check("manifest MIME " + path,
                  "manifest" in (r.headers.get("content-type") or ""), r.headers.get("content-type", ""))
        tc = page.query_selector('meta[name=theme-color]')
        check("theme-color " + path, tc is not None)
        vp = page.eval_on_selector('meta[name=viewport]', "e=>e.content")
        check("viewport " + path, "width=device-width" in vp, vp)
        check("pwa.js loaded " + path, page.evaluate("!!(window.JONE && window.JONE.pwa)"))

    # --- manifest content ----------------------------------------------------
    m = json.loads(page.request.get(BASE + "/manifest.webmanifest").text())
    check("manifest name", m["name"] == "J-ONE HOTEL & LODGE", m["name"])
    check("start_url", m["start_url"] == "/index.html", m["start_url"])
    check("scope", m["scope"] == "/", m["scope"])
    check("display standalone", m["display"] == "standalone")
    sizes = {i["sizes"] for i in m["icons"]}
    check("icon 192", "192x192" in sizes)
    check("icon 512", "512x512" in sizes)
    check("maskable icon", any(i.get("purpose") == "maskable" for i in m["icons"]))
    for i in m["icons"]:
        r = page.request.get(BASE + i["src"])
        check("icon reachable " + i["src"], r.status == 200, str(r.status))

    # --- service worker registration + scope ---------------------------------
    goto(page, BASE + "/index.html", "load")
    reg = page.evaluate("""async () => {
        const r = await navigator.serviceWorker.ready;
        return { scope: r.scope, active: !!r.active, state: r.active && r.active.state };
    }""")
    check("sw registered", reg["active"], json.dumps(reg))
    check("sw scope is root", reg["scope"].endswith("/") and reg["scope"] == BASE + "/", reg["scope"])

    # --- sw controls pages at every depth ------------------------------------
    for path in ["/index.html", "/rooms.html", "/booking.html", "/dashboard/index.html"]:
        goto(page, BASE + path, "load")
        page.wait_for_timeout(300)
        controlled = page.evaluate("!!navigator.serviceWorker.controller")
        check("sw controls " + path, controlled)

    # --- caches: what got stored ---------------------------------------------
    goto(page, BASE + "/index.html", "load")
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
    check("NO /dashboard/ cached", not any(u.startswith("/dashboard/") for u in allurls),
          str([u for u in allurls if u.startswith("/dashboard/")]))

    # --- dashboard navigation must NOT enter the pages cache -----------------
    goto(page, BASE + "/dashboard/bookings.html", "load")
    page.wait_for_timeout(800)
    goto(page, BASE + "/index.html", "load")
    dash_cached = page.evaluate("""async () => {
        const names = await caches.keys(); const out = [];
        for (const n of names) for (const r of await (await caches.open(n)).keys())
            if (new URL(r.url).pathname.startsWith('/dashboard/')) out.push(r.url);
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
    goto(page, BASE + "/index.html", "load")
    page.wait_for_timeout(600)
    ctx.set_offline(True)

    # previously visited page still opens
    goto(page, BASE + "/index.html", "domcontentloaded")
    check("offline: visited page served", "J-ONE" in page.content())

    # never-visited page -> offline.html (NOT index.html)
    goto(page, BASE + "/facilities.html", "domcontentloaded")
    body = page.content()
    check("offline: fallback page shown", "You're offline" in body or "offline" in page.title().lower(), page.title())
    check("offline: NOT redirected to index", "Quiet comfort, close to everything" not in body)

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
    # never return a cached response that could look like success.
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
    goto(page, BASE + "/index.html", "load")
    page.wait_for_timeout(500)
    ctrl = page.evaluate("""() => {
        const els = [...document.querySelectorAll('[data-pwa-install]')];
        return { count: els.length, hiddenAll: els.every(e => e.hidden) };
    }""")
    check("install controls injected", ctrl["count"] >= 2, json.dumps(ctrl))
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
