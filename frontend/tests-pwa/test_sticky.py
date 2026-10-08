# tests-pwa/test_sticky.py
"""Sticky, blurred topbar — dashboard (scrolls in .dash-main) + public header."""
import sys
from playwright.sync_api import sync_playwright
out=[]
def check(n,ok,d=""): out.append((n,ok,d))

STUB = """
localStorage.setItem('jone.auth', JSON.stringify({
  email:'a@b.c', first_name:'Ada', last_name:'Staff', role:'ADMIN', permissions:[]}));
sessionStorage.setItem('jone.session', JSON.stringify({access:'x.y.z', refresh:'r'}));
"""

with sync_playwright() as pw:
    b=pw.chromium.launch(); ctx=b.new_context(viewport={"width":1280,"height":800})
    p=ctx.new_page(); p.add_init_script(STUB)
    p.goto("http://127.0.0.1:8080/dashboard/index.html", wait_until="load")
    p.wait_for_timeout(1500)
    check("stayed on dashboard (auth stub ok)", "/dashboard/" in p.url, p.url)

    tb = p.query_selector(".dash-topbar")
    check("topbar exists", tb is not None)

    st = p.evaluate("""() => {
      const t=document.querySelector('.dash-topbar'), s=getComputedStyle(t);
      return {pos:s.position, top:s.top, z:s.zIndex,
              bf:s.backdropFilter||s.webkitBackdropFilter, bg:s.backgroundColor};
    }""")
    check("position sticky", st["pos"]=="sticky", str(st))
    check("top:0", st["top"]=="0px", str(st))
    check("backdrop blur applied", "blur" in (st["bf"] or ""), str(st["bf"]))
    # color-mix() computes to `color(srgb r g b / a)` in Chromium, not rgba().
    # Assert on the resolved ALPHA rather than the serialization format.
    alpha = p.evaluate("""() => {
        const bg = getComputedStyle(document.querySelector('.dash-topbar')).backgroundColor;
        const m = bg.match(/\\/\\s*([0-9.]+)\\s*\\)/) || bg.match(/rgba?\\([^)]*,\\s*([0-9.]+)\\s*\\)/);
        return m ? parseFloat(m[1]) : 1;
    }""")
    check("background is translucent (alpha < 1)", 0 < alpha < 1, f"alpha={alpha} raw={st['bg']}")

    # The real scroll container must be .dash-main, and content must overflow.
    sc = p.evaluate("""() => {
      const m=document.querySelector('.dash-main');
      return {h:m.clientHeight, sh:m.scrollHeight, over:m.scrollHeight>m.clientHeight+40};
    }""")
    check("dash-main is scrollable", sc["over"], str(sc))

    check("not scrolled initially",
          p.eval_on_selector(".dash-topbar","e=>!e.classList.contains('scrolled')"))

    # Scroll the CONTAINER (window scroll would do nothing here).
    p.evaluate("() => document.querySelector('.dash-main').scrollTo(0, 400)")
    p.wait_for_timeout(400)
    check("scrolled class added on container scroll",
          p.eval_on_selector(".dash-topbar","e=>e.classList.contains('scrolled')"))
    check("shadow + border appear when scrolled", p.evaluate("""() => {
        const s=getComputedStyle(document.querySelector('.dash-topbar'));
        return s.boxShadow!=='none' && s.borderBottomColor!=='rgba(0, 0, 0, 0)';
    }"""))

    # Topbar must stay pinned at the viewport top while content scrolls under it.
    box = p.evaluate("() => { const r=document.querySelector('.dash-topbar').getBoundingClientRect(); return {top:r.top, h:r.height}; }")
    mtop = p.evaluate("() => document.querySelector('.dash-main').getBoundingClientRect().top")
    check("topbar pinned to top of scroller", abs(box["top"]-mtop) < 2, f"{box} main={mtop}")
    check("topbar has height (visible)", box["h"] > 40, str(box))

    p.evaluate("() => document.querySelector('.dash-main').scrollTo(0, 0)")
    p.wait_for_timeout(400)
    check("scrolled class removed at top",
          p.eval_on_selector(".dash-topbar","e=>!e.classList.contains('scrolled')"))

    # Sidebar/drawer still work (we touched the same module)
    p.set_viewport_size({"width":390,"height":844}); p.wait_for_timeout(400)
    p.click(".dash-menu-toggle"); p.wait_for_timeout(450)
    check("mobile sidebar still opens",
          p.eval_on_selector(".dash-sidebar","e=>e.classList.contains('open')"))
    p.keyboard.press("Escape"); p.wait_for_timeout(450)
    check("mobile sidebar still closes",
          p.eval_on_selector(".dash-sidebar","e=>!e.classList.contains('open')"))
    p.set_viewport_size({"width":1280,"height":800})

    # Dark theme
    p.evaluate("()=>document.documentElement.setAttribute('data-theme','dark')")
    p.evaluate("() => document.querySelector('.dash-main').scrollTo(0, 300)")
    p.wait_for_timeout(400)
    dk = p.evaluate("""() => { const s=getComputedStyle(document.querySelector('.dash-topbar'));
        return {bf:s.backdropFilter||s.webkitBackdropFilter, bg:s.backgroundColor}; }""")
    check("dark theme keeps blur", "blur" in (dk["bf"] or ""), str(dk))

    # Other dashboard pages get it too
    for pg in ["bookings","payments","rooms","settings"]:
        p.goto(f"http://127.0.0.1:8080/dashboard/{pg}.html", wait_until="load"); p.wait_for_timeout(900)
        p.evaluate("() => { const m=document.querySelector('.dash-main'); m && m.scrollTo(0,300); }")
        p.wait_for_timeout(350)
        ok = p.evaluate("""() => { const t=document.querySelector('.dash-topbar');
            if(!t) return false; const m=document.querySelector('.dash-main');
            return m.scrollHeight>m.clientHeight+40 ? t.classList.contains('scrolled') : true; }""")
        check(f"sticky works on {pg}.html", ok)

    # Public header untouched
    p.goto("http://127.0.0.1:8080/index.html", wait_until="load"); p.wait_for_timeout(1000)
    p.evaluate("() => window.scrollTo(0, 500)"); p.wait_for_timeout(400)
    check("public header still sticky+blurred", p.evaluate("""() => {
        const h=document.querySelector('.site-header'), s=getComputedStyle(h);
        return s.position==='sticky' && /blur/.test(s.backdropFilter||s.webkitBackdropFilter||'')
               && h.classList.contains('scrolled');
    }"""))
    b.close()

print("\n".join(f"{'PASS' if o else 'FAIL'}  {n}"+(f"  -> {d}" if d and not o else "") for n,o,d in out))
f=[n for n,o,_ in out if not o]; print(f"\n{len(out)-len(f)}/{len(out)} passed")
sys.exit(1 if f else 0)
