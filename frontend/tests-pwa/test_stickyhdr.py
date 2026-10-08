# tests-pwa/test_stickyhdr.py
"""The header/topbar must stay pinned at the top through any amount of scrolling."""
import sys
from playwright.sync_api import sync_playwright
out=[]
def check(n,ok,d=""): out.append((n,ok,d))

PUBLIC=["index.html","rooms.html","room-details.html","facilities.html","gallery.html",
        "offers.html","about.html","contact.html","policies.html","terms.html",
        "booking.html","my-bookings.html","login.html","404.html"]
STUB="""localStorage.setItem('jone.auth',JSON.stringify({email:'a@b.c',first_name:'Ada',last_name:'S',role:'ADMIN',permissions:[]}));
sessionStorage.setItem('jone.session',JSON.stringify({access:'x',refresh:'r'}));"""

with sync_playwright() as pw:
  b=pw.chromium.launch()
  for vw,h,tag in ((1280,800,"desktop"),(390,844,"mobile")):
    ctx=b.new_context(viewport={"width":vw,"height":h}); p=ctx.new_page()
    for page in PUBLIC:
      p.goto(f"http://127.0.0.1:8080/{page}",wait_until="load"); p.wait_for_timeout(700)
      if not p.query_selector(".site-header"): continue
      # make sure the page is long enough to scroll
      p.evaluate("""() => { if(document.body.scrollHeight <= innerHeight+200){
          const d=document.createElement('div'); d.style.height='2000px'; document.body.appendChild(d);} }""")
      p.wait_for_timeout(150)
      tops=[]
      for y in (0,300,900,2000):
        p.evaluate("y=>window.scrollTo(0,y)", y); p.wait_for_timeout(280)
        tops.append(round(p.evaluate("document.querySelector('.site-header').getBoundingClientRect().top"),1))
      check(f"[{tag}] header pinned {page}", all(abs(t)<2 for t in tops), f"tops={tops}")
      if page=="index.html":
        check(f"[{tag}] header visible while scrolled",
              p.evaluate("""() => { const h=document.querySelector('.site-header');
                  const r=h.getBoundingClientRect(); const s=getComputedStyle(h);
                  return r.height>30 && s.visibility!=='hidden' && parseFloat(s.opacity)>0.5 &&
                         document.elementFromPoint(r.left+r.width/2, r.height/2) !== null; }"""))
        check(f"[{tag}] blur still active", p.evaluate("""() => {
            const s=getComputedStyle(document.querySelector('.site-header'));
            return /blur/.test(s.backdropFilter||s.webkitBackdropFilter||''); }"""))
      # no horizontal overflow reintroduced by the clip change
      check(f"[{tag}] no h-scroll {page}",
            p.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1"),
            str(p.evaluate("document.documentElement.scrollWidth"))+" vs "+str(vw))
    ctx.close()

    # dashboard
    ctx=b.new_context(viewport={"width":vw,"height":h}); p=ctx.new_page(); p.add_init_script(STUB)
    for dp in ["index","bookings","payments","rooms"]:
      p.goto(f"http://127.0.0.1:8080/dashboard/{dp}.html",wait_until="load"); p.wait_for_timeout(1200)
      p.evaluate("""() => { const c=document.querySelector('.dash-content')||document.querySelector('.dash-main');
        for(let i=0;i<25;i++){const d=document.createElement('div');
        d.style.cssText='margin:10px 0;padding:22px;background:#456';c.appendChild(d);} }""")
      p.wait_for_timeout(250)
      tops=[]
      for y in (0,400,1200):
        p.evaluate("""y=>{document.querySelectorAll('*').forEach(e=>{if(e.scrollHeight>e.clientHeight+5)e.scrollTop=y;});window.scrollTo(0,y);}""", y)
        p.wait_for_timeout(280)
        tops.append(round(p.evaluate("document.querySelector('.dash-topbar').getBoundingClientRect().top"),1))
      check(f"[{tag}] topbar pinned dashboard/{dp}", all(abs(t)<2 for t in tops), f"tops={tops}")
    ctx.close()
  b.close()

print("\n".join(f"{'PASS' if o else 'FAIL'}  {n}"+(f"  -> {d}" if d and not o else "") for n,o,d in out))
f=[n for n,o,_ in out if not o]; print(f"\n{len(out)-len(f)}/{len(out)} passed")
sys.exit(1 if f else 0)
