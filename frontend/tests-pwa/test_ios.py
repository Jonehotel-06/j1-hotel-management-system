# tests-pwa/test_ios.py
"""iOS Safari: no beforeinstallprompt, so verify the Add-to-Home-Screen path."""
import sys
from playwright.sync_api import sync_playwright
out=[]
def check(n,ok,d=""): out.append((n,ok,d))

IOS_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
ANDROID_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Mobile Safari/537.36")
STUB = """
localStorage.setItem('jone.auth', JSON.stringify({
  email:'a@b.c', first_name:'Ada', last_name:'Staff', role:'ADMIN', permissions:[]}));
sessionStorage.setItem('jone.session', JSON.stringify({access:'x.y.z', refresh:'r'}));
"""

def stub_api(page):
    import json as _json, re as _re
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
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(body))
    page.route(_re.compile(r".*/api/.*"), handler)

with sync_playwright() as pw:
    b = pw.chromium.launch()

    # ---- iPhone Safari ----
    ctx = b.new_context(user_agent=IOS_UA, viewport={"width":390,"height":844},
                        is_mobile=True, has_touch=True, service_workers="allow")
    p = ctx.new_page()
    stub_api(p); p.add_init_script(STUB)
    p.goto("http://127.0.0.1:8080/dashboard/index.html", wait_until="load"); p.wait_for_timeout(1200)
    check("iOS detected", p.evaluate("JONE.pwa.isIOS()"))
    # iOS never fires beforeinstallprompt -> controls must still be offered
    shown = p.evaluate("[...document.querySelectorAll('[data-pwa-install]')].some(e=>!e.hidden)")
    check("iOS: install control visible without beforeinstallprompt", shown)
    # tapping it opens the Share -> Add to Home Screen instructions, NOT Android copy
    p.evaluate("() => document.querySelector('[data-pwa-install]:not([hidden])').click()")
    p.wait_for_timeout(600)
    txt = p.evaluate("() => document.querySelector('.modal-panel, .modal')?.textContent || ''")
    check("iOS: shows Share -> Add to Home Screen", "Add to Home Screen" in txt and "Share" in txt, txt[:120])
    check("iOS: no Android-specific copy", "Install app" not in txt and "Chrome" not in txt, txt[:120])
    # apple meta present
    for m in ["apple-mobile-web-app-capable","apple-mobile-web-app-status-bar-style","apple-mobile-web-app-title"]:
        check("iOS meta " + m, p.query_selector(f'meta[name="{m}"]') is not None)
    check("apple-touch-icon present", p.query_selector('link[rel="apple-touch-icon"]') is not None)
    ati = p.eval_on_selector('link[rel="apple-touch-icon"]', "e=>e.href")
    check("apple-touch-icon 200", p.request.get(ati).status == 200, ati)
    # The public website offers guests no install path at all.
    p.goto("http://127.0.0.1:8080/index.html", wait_until="load"); p.wait_for_timeout(500)
    check("iOS public site: no install control", p.evaluate("document.querySelectorAll('[data-pwa-install]').length === 0"))
    check("iOS public site: no manifest", p.query_selector('link[rel="manifest"]') is None)
    ctx.close()

    # ---- Android Chrome ----
    ctx = b.new_context(user_agent=ANDROID_UA, viewport={"width":412,"height":915},
                        is_mobile=True, has_touch=True, service_workers="allow")
    p = ctx.new_page()
    p.goto("http://127.0.0.1:8080/index.html", wait_until="load"); p.wait_for_timeout(500)
    check("Android public site: no install control", p.evaluate("document.querySelectorAll('[data-pwa-install]').length === 0"))
    check("Android public site: install is not enabled", p.evaluate("JONE.pwa.installEnabled === false"))
    stub_api(p); p.add_init_script(STUB)
    p.goto("http://127.0.0.1:8080/dashboard/index.html", wait_until="load"); p.wait_for_timeout(1200)
    check("Android: not treated as iOS", p.evaluate("JONE.pwa.isIOS()") is False)
    check("Android dashboard: install is enabled", p.evaluate("JONE.pwa.installEnabled === true"))
    hidden = p.evaluate("[...document.querySelectorAll('[data-pwa-install]')].every(e=>e.hidden)")
    check("Android: control hidden until prompt fires", hidden)
    p.evaluate("""() => { const e=new Event('beforeinstallprompt');
        e.prompt=()=>{window.__p=1}; e.userChoice=Promise.resolve({outcome:'accepted'});
        window.dispatchEvent(e); }""")
    p.wait_for_timeout(300)
    check("Android: control revealed after prompt",
          p.evaluate("[...document.querySelectorAll('[data-pwa-install]')].some(e=>!e.hidden)"))
    p.evaluate("() => document.querySelector('[data-pwa-install]:not([hidden])').click()")
    p.wait_for_timeout(400)
    check("Android: native prompt() invoked (no iOS modal)", p.evaluate("window.__p === 1"))
    ctx.close()

    # ---- already installed (standalone): no install UI at all ----
    ctx = b.new_context(user_agent=ANDROID_UA, viewport={"width":412,"height":915}, service_workers="allow")
    p = ctx.new_page()
    stub_api(p); p.add_init_script(STUB)
    p.add_init_script("""Object.defineProperty(window,'matchMedia',{value:(q)=>({
        matches: q.includes('display-mode: standalone'), media:q,
        addEventListener(){}, removeEventListener(){}, addListener(){}, removeListener(){}})});""")
    p.goto("http://127.0.0.1:8080/dashboard/index.html", wait_until="load"); p.wait_for_timeout(1000)
    check("standalone: reported installed", p.evaluate("JONE.pwa.isStandalone()"))
    check("standalone: no install controls",
          p.evaluate("document.querySelectorAll('[data-pwa-install]').length === 0 || [...document.querySelectorAll('[data-pwa-install]')].every(e=>e.hidden)"))
    check("standalone: no install banner", p.query_selector(".pwa-banner") is None)
    b.close()

print("\n".join(f"{'PASS' if o else 'FAIL'}  {n}"+(f"  -> {d}" if d and not o else "") for n,o,d in out))
f=[n for n,o,_ in out if not o]
print(f"\n{len(out)-len(f)}/{len(out)} passed")
sys.exit(1 if f else 0)
