# tests-pwa/test_installability.py
"""Ask Chrome itself whether the staff dashboard app is installable (CDP)."""
import json, re
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8080"
STUB = """
localStorage.setItem('jone.auth', JSON.stringify({
  email:'a@b.c', first_name:'Ada', last_name:'Staff', role:'ADMIN', permissions:[]}));
sessionStorage.setItem('jone.session', JSON.stringify({access:'x.y.z', refresh:'r'}));
"""
EMPTY = {"success": True, "data": {"results": []}, "pagination": None}
PROFILE = {"success": True, "data": {"id": 1, "email": "a@b.c", "first_name": "Ada",
                                     "last_name": "Staff", "role": "ADMIN", "is_active": True}}


def stub_api(page):
    def handler(route):
        url = route.request.url
        if "/api/auth/capabilities/" in url:
            body = {"success": True, "data": []}
        elif "/api/auth/token/refresh/" in url:
            body = {"success": True, "data": {"access": "stub-access", "refresh": "stub-refresh"}}
        elif "/api/auth/me" in url or "/api/admin/profile" in url:
            body = PROFILE
        else:
            body = EMPTY
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
    page.route(re.compile(r".*/api/.*"), handler)


with sync_playwright() as pw:
    b = pw.chromium.launch(); ctx = b.new_context(service_workers="allow"); p = ctx.new_page()
    stub_api(p); p.add_init_script(STUB)
    p.goto(BASE + "/dashboard/index.html", wait_until="load"); p.wait_for_timeout(1500)
    m = ctx.new_cdp_session(p).send("Page.getAppManifest")
    print("manifest url:", m.get("url"))
    print("parse errors:", json.dumps(m.get("errors", []), indent=1))
    pj = m.get("parsed") or {}
    print("parsed name:", pj.get("name"), "| startUrl:", pj.get("startUrl"), "| scope:", pj.get("scope"))
    errs = [e for e in m.get("errors", []) if e.get("critical")]
    print("\nCRITICAL manifest errors:", errs if errs else "NONE")
    assert m.get("url", "").endswith("/dashboard/manifest.webmanifest"), m.get("url")
    assert not errs, errs

    # The public homepage must expose no manifest at all.
    p.goto(BASE + "/index.html", wait_until="load"); p.wait_for_timeout(500)
    public = ctx.new_cdp_session(p).send("Page.getAppManifest")
    print("public manifest url:", public.get("url"))
    assert not public.get("url"), public.get("url")
    b.close()
