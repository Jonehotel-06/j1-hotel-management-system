# tests-pwa/test_installability.py
"""Ask Chrome itself whether the app is installable (CDP Manifest domain)."""
import json
from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    b=pw.chromium.launch(); ctx=b.new_context(service_workers="allow"); p=ctx.new_page()
    cdp = ctx.new_cdp_session(p)
    p.goto("http://127.0.0.1:8080/index.html", wait_until="load"); p.wait_for_timeout(1500)
    m = cdp.send("Page.getAppManifest")
    print("manifest url:", m.get("url"))
    print("parse errors:", json.dumps(m.get("errors", []), indent=1))
    pj = m.get("parsed") or {}
    print("parsed name:", pj.get("name"), "| startUrl:", pj.get("startUrl"), "| scope:", pj.get("scope"))
    errs = [e for e in m.get("errors", []) if e.get("critical")]
    print("\nCRITICAL manifest errors:", errs if errs else "NONE")
    b.close()
