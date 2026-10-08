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

with sync_playwright() as pw:
    b = pw.chromium.launch()

    # ---- iPhone Safari ----
    ctx = b.new_context(user_agent=IOS_UA, viewport={"width":390,"height":844},
                        is_mobile=True, has_touch=True, service_workers="allow")
    p = ctx.new_page()
    p.goto("http://127.0.0.1:8080/index.html", wait_until="load"); p.wait_for_timeout(1200)
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
    ctx.close()

    # ---- Android Chrome ----
    ctx = b.new_context(user_agent=ANDROID_UA, viewport={"width":412,"height":915},
                        is_mobile=True, has_touch=True, service_workers="allow")
    p = ctx.new_page()
    p.goto("http://127.0.0.1:8080/index.html", wait_until="load"); p.wait_for_timeout(1200)
    check("Android: not treated as iOS", p.evaluate("JONE.pwa.isIOS()") is False)
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
    p.add_init_script("""Object.defineProperty(window,'matchMedia',{value:(q)=>({
        matches: q.includes('display-mode: standalone'), media:q,
        addEventListener(){}, removeEventListener(){}, addListener(){}, removeListener(){}})});""")
    p.goto("http://127.0.0.1:8080/index.html", wait_until="load"); p.wait_for_timeout(1000)
    check("standalone: reported installed", p.evaluate("JONE.pwa.isStandalone()"))
    check("standalone: no install controls",
          p.evaluate("document.querySelectorAll('[data-pwa-install]').length === 0 || [...document.querySelectorAll('[data-pwa-install]')].every(e=>e.hidden)"))
    check("standalone: no install banner", p.query_selector(".pwa-banner") is None)
    b.close()

print("\n".join(f"{'PASS' if o else 'FAIL'}  {n}"+(f"  -> {d}" if d and not o else "") for n,o,d in out))
f=[n for n,o,_ in out if not o]
print(f"\n{len(out)-len(f)}/{len(out)} passed")
sys.exit(1 if f else 0)
