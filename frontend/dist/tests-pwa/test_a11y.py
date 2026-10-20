# tests-pwa/test_a11y.py
"""Accessibility of the PWA-specific UI (install controls, banner, offline page)."""
import sys, urllib.request
from playwright.sync_api import sync_playwright
AXE = urllib.request.urlopen("https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js").read().decode()
out=[]
def check(n,ok,d=""): out.append((n,ok,d))
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
    b=pw.chromium.launch(); ctx=b.new_context(service_workers="allow"); p=ctx.new_page()

    # --- offline page ---
    p.goto("http://127.0.0.1:8080/offline.html", wait_until="load"); p.wait_for_timeout(400)
    p.add_script_tag(content=AXE)
    r = p.evaluate("async()=>{const r=await axe.run(document,{runOnly:['wcag2a','wcag2aa']});return r.violations.map(v=>({id:v.id,impact:v.impact,n:v.nodes.length}))}")
    serious=[v for v in r if v["impact"] in ("serious","critical")]
    check("offline.html: no serious/critical a11y violations", not serious, str(serious))
    check("offline.html: has h1", p.query_selector("h1") is not None)
    check("offline.html: live region for status", p.query_selector("[aria-live]") is not None)
    # keyboard: Try again reachable & activatable
    p.keyboard.press("Tab"); p.wait_for_timeout(150)
    focused = p.evaluate("document.activeElement.id || document.activeElement.tagName")
    check("offline.html: first tab reaches Try again", focused == "retry-btn", str(focused))
    fv = p.evaluate("""() => { const e=document.getElementById('retry-btn'); e.focus();
        const s=getComputedStyle(e); return s.outlineStyle !== 'none' || s.boxShadow !== 'none'; }""")
    check("offline.html: visible focus state", fv)

    # --- install controls on the dashboard (the only installable area) ---
    stub_api(p); p.add_init_script(STUB)
    p.goto("http://127.0.0.1:8080/dashboard/index.html", wait_until="load"); p.wait_for_timeout(1200)
    p.evaluate("""() => { const e=new Event('beforeinstallprompt');
        e.prompt=()=>{}; e.userChoice=Promise.resolve({outcome:'dismissed'});
        window.dispatchEvent(e); }""")
    p.evaluate("() => { [...document.querySelectorAll('[data-pwa-install]')].forEach(e=>e.hidden=false); }")
    p.wait_for_timeout(300)
    # build the banner directly for auditing
    p.evaluate("() => { localStorage.removeItem('jone.pwa.installDismissedAt'); }")
    p.wait_for_timeout(6500)
    has_banner = p.query_selector(".pwa-banner") is not None
    check("install banner rendered", has_banner)
    if has_banner:
        check("banner has region role+label",
              p.eval_on_selector(".pwa-banner","e=>e.getAttribute('role')==='region' && !!e.getAttribute('aria-label')"))
        check("banner close has aria-label",
              p.eval_on_selector(".pwa-banner-close","e=>!!e.getAttribute('aria-label')"))
        check("banner decorative img has empty alt",
              p.eval_on_selector(".pwa-banner img","e=>e.getAttribute('alt')===''"))
    # Keyboard reachability of the dashboard sidebar install control.
    states = p.evaluate("""async () => {
        const control = document.querySelector('.dash-sidebar-footer [data-pwa-install]');
        const out = {};
        if (control) {
            control.hidden = false;
            control.focus();
            out.sidebarFocusable = document.activeElement === control;
            const s = getComputedStyle(control);
            out.visibleFocus = s.outlineStyle !== 'none' || s.boxShadow !== 'none';
        }
        return out;
    }""")
    if states.get("sidebarFocusable") is not None:
        check("sidebar install control is keyboard focusable", states.get("sidebarFocusable") is True, str(states))
    check("install controls are real <button>s",
          p.eval_on_selector_all("[data-pwa-install]","els=>els.every(e=>e.tagName==='BUTTON')"))
    check("install controls have accessible names",
          p.eval_on_selector_all("[data-pwa-install]","els=>els.every(e=>(e.textContent||'').trim().length>3)"))
    check("no emoji in PWA UI",
          p.eval_on_selector_all("[data-pwa-install], .pwa-banner",
            "els=>els.every(e=>!/\\p{Extended_Pictographic}/u.test(e.textContent))"))

    # axe on the dashboard with the PWA UI visible — compare to known baseline set
    p.add_script_tag(content=AXE)
    r2 = p.evaluate("""async()=>{const r=await axe.run(
        {include:[['.pwa-banner'],['[data-pwa-install]']]},{runOnly:['wcag2a','wcag2aa']});
        return r.violations.map(v=>({id:v.id,impact:v.impact,nodes:v.nodes.length}))}""")
    bad=[v for v in r2 if v["impact"] in ("serious","critical")]
    check("PWA UI: no serious/critical a11y violations", not bad, str(bad))
    b.close()

print("\n".join(f"{'PASS' if o else 'FAIL'}  {n}"+(f"  -> {d}" if d and not o else "") for n,o,d in out))
f=[n for n,o,_ in out if not o]; print(f"\n{len(out)-len(f)}/{len(out)} passed")
sys.exit(1 if f else 0)
