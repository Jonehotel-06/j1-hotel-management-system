# tests-pwa/test_paystack.py
"""The SW must never intercept cross-origin payment traffic or payment endpoints."""
import sys
from playwright.sync_api import sync_playwright
out=[]
def check(n,ok,d=""): out.append((n,ok,d))

with sync_playwright() as pw:
    b=pw.chromium.launch(); ctx=b.new_context(service_workers="allow"); p=ctx.new_page()

    # Record which requests the service worker actually handled.
    handled=[]
    p.on("response", lambda r: handled.append((r.url, r.from_service_worker)))

    p.goto("http://127.0.0.1:8080/index.html", wait_until="load"); p.wait_for_timeout(1500)
    handled.clear()

    # Cross-origin request (stands in for checkout.paystack.com) must NOT be SW-served.
    p.evaluate("""async () => {
        try { await fetch('https://checkout.paystack.com/', {mode:'no-cors'}); } catch(e){}
    }""")
    p.wait_for_timeout(1200)
    ps=[(u,sw) for u,sw in handled if "paystack" in u]
    check("Paystack request not served by SW", all(not sw for _,sw in ps), str(ps))

    # Same-origin /api/payments/* must never come from the SW, and must not be cached.
    handled.clear()
    p.evaluate("""async () => {
        for (const u of ['/api/payments/initialize/','/api/payments/verify/ref123/',
                         '/api/bookings/','/api/rooms/availability/?check_in=2026-10-01',
                         '/api/auth/login/','/api/admin/dashboard/']) {
            try { await fetch(u); } catch(e){}
        }
    }""")
    p.wait_for_timeout(1500)
    api=[(u,sw) for u,sw in handled if "/api/" in u]
    check("API responses never come from the SW", all(not sw for _,sw in api),
          str([u for u,sw in api if sw]))

    cached = p.evaluate("""async () => {
        const bad=[];
        for (const n of await caches.keys())
            for (const r of await (await caches.open(n)).keys()) {
                const u=new URL(r.url);
                if (u.pathname.startsWith('/api/') || u.host.includes('paystack')) bad.push(r.url);
            }
        return bad;
    }""")
    check("no API/Paystack entries in any cache", cached==[], str(cached))

    # Payment-verify page: the SW must not turn a failed verify into a "success".
    p.goto("http://127.0.0.1:8080/payment-verify.html?reference=TEST123", wait_until="load")
    p.wait_for_timeout(2000)
    # Assert on VISIBLE text: the page has a hidden success panel in its markup
    # that is only revealed once the backend confirms the transaction.
    visible = p.inner_text("main").lower()
    check("failed verification never shows success",
          "payment successful" not in visible, visible[:160])
    check("failed verification states it could not be confirmed",
          "couldn't confirm" in visible or "could not" in visible, visible[:160])
    b.close()

print("\n".join(f"{'PASS' if o else 'FAIL'}  {n}"+(f"  -> {d}" if d and not o else "") for n,o,d in out))
f=[n for n,o,_ in out if not o]; print(f"\n{len(out)-len(f)}/{len(out)} passed")
sys.exit(1 if f else 0)
