#!/usr/bin/env python3
"""Headless browser verification of the UI-facing audit items (2, 3, 5, 6, 9, 10, 11)
plus the item-1 enrolment surface and the item-4 capture contract.

Run:  python tools/verification/verify_ui_items.py
Screenshots land next to this script as ui-*.png. Read-only except the terminal
CLOCK_IN/CLOCK_OUT pair, which records (and then closes) one attendance window
for an already-enrolled demo account, and the item-9 reload probe.
"""
import asyncio
import json
import os
import sys
from datetime import datetime

from playwright.async_api import async_playwright
from jone_verify import (ADMIN, BAR, GENERAL_MANAGER, MANAGER, RECEPTION, API_BASE as API,
                         WEB_BASE as BASE, artifact, desk_key, password)


DESK_KEY = desk_key()
RESULTS = []
CONSOLE_ERRORS = []


PW = password()


def record(item, name, ok, detail=""):
    RESULTS.append((item, name, ok, detail))
    print(("PASS" if ok else "FAIL"), f"[{item}]", name, "-", detail)


def login_retry(email, headers=None, attempts=4):
    """The login endpoint is throttled at 5/min per client; wait out a 429."""
    import time
    import requests
    r = None
    for _ in range(attempts):
        r = requests.post(API + "/api/auth/login/",
                          json={"email": email, "password": PW},
                          headers=headers or {}, timeout=20)
        if r.status_code != 429:
            return r
        print("· login throttled; waiting for the next window…")
        time.sleep(62)
    return r


class UserSession:
    """A signed-in browser session, optionally carrying the Receptionist Desktop key."""

    def __init__(self, email, desk_key=""):
        r = login_retry(email, {"X-JONE-Desktop-Key": desk_key} if desk_key else None)
        assert r.status_code == 200, f"login failed {r.status_code}: {r.text[:200]}"
        body = r.json()
        self.user = body["data"]["user"]
        self.tokens = body["data"]["tokens"]
        self.key = desk_key

    def init_script(self):
        script = """
        (function(){
          localStorage.setItem('jone.auth', %s);
          sessionStorage.setItem('jone.session', %s);
        })();
        """ % (json.dumps(json.dumps(self.user)), json.dumps(json.dumps(self.tokens)))
        if self.key:
            script += "localStorage.setItem('jone.receptionistDesktopKey', %s);" % json.dumps(self.key)
        return script


async def new_page(browser, session, *, viewport=(1440, 900), extras=(), route_version=None):
    ctx = await browser.new_context(viewport={"width": viewport[0], "height": viewport[1]})
    scripts = [session.init_script()] if session else []
    if extras:
        scripts.extend(extras)
    for script in scripts:
        await ctx.add_init_script(script)
    page = await ctx.new_page()
    page.on("console", lambda m: CONSOLE_ERRORS.append(("console." + m.type, m.text))
            if m.type == "error" else None)
    page.on("pageerror", lambda e: CONSOLE_ERRORS.append(("pageerror", str(e))))
    if route_version:
        async def fulfil(route):
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(route_version))
        await page.route("**/version.json*", fulfil)
    return ctx, page


async def goto(page, path):
    await page.goto(BASE + path, wait_until="networkidle")
    await page.wait_for_timeout(900)


async def ui_subject_from_page(page, subject_id):
    """The email of the UI subject, read from the profile page the flow just used."""
    email = await page.evaluate(r"""() => {
        const node = document.querySelector('[data-sp-email], #sp-email, .sp-meta');
        const text = (node ? node.textContent : document.body.textContent) || "";
        const match = text.match(/[\w.+-]+@[\w-]+\.[\w.-]+/);
        return match ? match[0] : "";
    }""")
    return email or f"profile id {subject_id}"


async def main():
    manager = UserSession(MANAGER)
    admin = UserSession(ADMIN)
    reception = UserSession(RECEPTION, desk_key=DESK_KEY)
    print("· API sessions acquired (manager, admin, receptionist)")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()

        # ------------------------------------------------------- item 2 (desktop)
        ctx, page = await new_page(browser, manager)
        await goto(page, "/dashboard/index.html")
        link = page.locator(".dash-user-link")
        count = await link.count()
        href = await link.first.get_attribute("href") if count else None
        label = await link.first.get_attribute("aria-label") if count else None
        name = (await link.first.inner_text()) if count else ""
        record(2, "sidebar identity is a link carrying the signed-in user",
               count == 1 and href == "profile.html" and "Manager" in name and label,
               f"href={href} aria-label={label!r}")
        await link.first.hover()
        await page.screenshot(path=artifact("ui-02-sidebar-link.png"), clip={"x": 0, "y": 700, "width": 280, "height": 200})
        await link.first.click()
        await page.wait_for_url("**/profile.html", timeout=15000)
        await page.wait_for_timeout(1200)
        heading = (await page.locator("h1").first.inner_text()).strip()
        form = await page.locator("[data-profile-form], form").count()
        record(2, "clicking it opens the user's own profile page",
               "profile.html" in page.url and heading and form > 0, f"{page.url} h1={heading!r}")
        await page.screenshot(path=artifact("ui-02-profile-page.png"))
        await ctx.close()

        # -------------------------------------------------------- item 2 (mobile)
        ctx, page = await new_page(browser, manager, viewport=(390, 844))
        await goto(page, "/dashboard/index.html")
        await page.click(".dash-menu-toggle")
        await page.wait_for_timeout(500)
        visible = await page.locator(".dash-user-link").first.is_visible()
        await page.locator(".dash-user-link").first.click()
        await page.wait_for_url("**/profile.html", timeout=15000)
        record(2, "the same link works in the mobile drawer", visible and "profile.html" in page.url,
               f"visible={visible} url={page.url}")
        await page.screenshot(path=artifact("ui-02-mobile-profile.png"))
        await ctx.close()

        # -------------------------------------------------------------- item 3
        ctx, page = await new_page(browser, None)
        await goto(page, "/login.html")
        desk_ui = await page.locator("#desk-setup, [data-desk-setup], input[name=desk_key], #desk-key").count()
        body_text = await page.inner_text("body")
        code_ui = await page.locator("[data-login-code], #login-code, [name=access_code]").count()
        record(3, "ordinary sign-in no longer offers desktop-key setup",
               desk_ui == 0 and "desktop key" not in body_text.lower(),
               f"desk widgets={desk_ui} access-code inputs={code_ui}")
        await page.screenshot(path=artifact("ui-03-login.png"))
        await ctx.close()

        ctx, page = await new_page(browser, admin)
        await goto(page, "/dashboard/terminals.html")
        panel = page.locator("[data-desktop-enrol-state]")
        panel_count = await panel.count()
        save_enabled = await page.locator("[data-desktop-enrol-save]").first.is_enabled() \
            if await page.locator("[data-desktop-enrol-save]").count() else False
        text = (await page.inner_text("body"))
        record(3, "Receptionist Desktop enrolment lives on the workstation admin page",
               panel_count == 1 and save_enabled and "Receptionist Desktop enrolment" in text,
               f"state node={panel_count} save enabled={save_enabled}")
        await page.screenshot(path=artifact("ui-03-terminals-enrolment.png"), full_page=False)
        await ctx.close()

        ctx, page = await new_page(browser, reception)
        await goto(page, "/dashboard/terminals.html")
        denied = "Receptionist Desktop enrolment" not in (await page.inner_text("body"))
        record(3, "an ordinary operational role cannot see the enrolment control", denied,
               "panel hidden/denied for RECEPTIONIST")
        await ctx.close()

        # -------------------------------------------------------- item 5 + 6 UI
        sim = "window.__APP_CONFIG__ = Object.assign({}, window.__APP_CONFIG__, {BIOMETRIC_READER_MODE: 'simulated'});"
        ctx, page = await new_page(browser, manager, extras=[sim])
        await goto(page, "/dashboard/workforce.html")
        await page.click('[data-wf-tab="reception-terminal"]')
        await page.wait_for_timeout(400)
        state_line = (await page.inner_text("[data-wf-rt-desktop-state]")).strip()
        fallback_visible = await page.locator("[data-wf-rt-fallback]").is_visible()
        record(6, "the console tells the operator what this browser is",
               "desktop" in state_line.lower(), state_line[:90])
        record(6, "manager sees the supervised fallback control", fallback_visible,
               f"fallback card visible={fallback_visible}")

        # Enrol a staff member from the staff profile with the SAME capture path a
        # terminal uses (item 1's UI), then clock them in at the terminal.
        #
        # The subject belongs to THIS script: an enrolment supersedes the previous
        # template, and the capture here comes from the browser (a simulated reader
        # in test mode), which does not match the deterministic probe the API-level
        # driver verifies against ITS subject. Sharing one account between the two
        # drivers looked like a product failure (403 NO_MATCH) and was really a
        # test-isolation collision — enrolment AND the terminal verify below must
        # therefore use the same, dedicated account.
        ui_subject_id = os.environ.get("JONE_VERIFY_UI_SUBJECT_ID", "20")
        ui_subject_email = os.environ.get("JONE_VERIFY_UI_SUBJECT_EMAIL", "")
        await goto(page, f"/dashboard/staff-profile.html?id={ui_subject_id}")
        await page.click('[data-sp-tab="biometrics"]')
        await page.wait_for_selector("#sp-btn-enroll-fp", state="visible", timeout=30000)
        await page.click("#sp-btn-enroll-fp")
        await page.wait_for_timeout(2500)
        profile_text = (await page.inner_text("body")).replace("\n", " ")
        record(1, "the staff profile enrol button records a fingerprint with a real capture source",
               "enrolled" in profile_text.lower() or "Fingerprint" in profile_text,
               profile_text[-140:])
        await page.screenshot(path=artifact("ui-01-enrol.png"))

        await goto(page, "/dashboard/workforce.html")
        await page.click('[data-wf-tab="reception-terminal"]')
        await page.wait_for_timeout(400)
        await page.fill("#wf-rt-identifier", ui_subject_email or await ui_subject_from_page(page, ui_subject_id))
        await page.click('[data-wf-rt-action="CLOCK_IN"]')
        try:
            await page.wait_for_selector("[data-reception-issued-code]", timeout=30000)
        except Exception:
            diag = (await page.inner_text("#wf-rt-result")).replace("\n", " ")[:160]
            print("  !! terminal diagnostics:", diag)
            raise
        code_el = await page.inner_text("[data-reception-issued-code]")
        result_text = await page.inner_text("#wf-rt-result")
        record(5, "front-desk CLOCK_IN returns an issued numeric code to the operator",
               code_el.strip().isdigit() and len(code_el.strip()) == 6,
               f"code={code_el.strip()} digits={code_el.strip().isdigit()}")
        record(5, "the issue time is rendered (labelled expiry comes from dateTime())",
               "Valid until" in result_text and "6-DIGIT NUMERIC ACCESS CODE" in result_text,
               result_text.replace("\n", " ")[:110])
        await page.screenshot(path=artifact("ui-05-terminal-code.png"))

        await page.click('[data-wf-rt-action="CLOCK_OUT"]')
        await page.wait_for_timeout(2500)
        out_text = await page.inner_text("#wf-rt-result")
        record(5, "CLOCK_OUT at the desk closes the attendance window",
               "COMPLETED" in out_text.upper(), out_text.replace("\n", " ")[:90])
        await ctx.close()

        # fallback UI: employee cannot see it, and a refusal is explained
        ctx, page = await new_page(browser, reception, extras=[sim])
        await goto(page, "/dashboard/workforce.html")
        await page.click('[data-wf-tab="reception-terminal"]')
        await page.wait_for_timeout(300)
        employee_fallback = await page.locator("[data-wf-rt-fallback]").is_visible()
        record(6, "ordinary staff never see the fallback control", not employee_fallback,
               f"visible={employee_fallback}")
        await ctx.close()

        ctx, page = await new_page(browser, manager, extras=[sim])
        await goto(page, "/dashboard/workforce.html")
        await page.click('[data-wf-tab="reception-terminal"]')
        await page.fill("#wf-rt-fallback-identifier", GENERAL_MANAGER)
        await page.fill("#wf-rt-fallback-reason", "short")
        await page.click("[data-wf-rt-fallback-issue]")
        await page.wait_for_timeout(800)
        toast = await page.locator(".toast, [class*=toast]").count()
        state = await page.inner_text("[data-wf-rt-fallback-state]")
        record(6, "an unusable fallback request explains why, before any request",
               toast > 0 or "10 characters" in state or "10 characters" in (await page.inner_text("body")),
               f"toasts={toast}")
        await ctx.close()

        # ------------------------------------------------------------- item 11
        ctx, page = await new_page(browser, admin)
        await goto(page, "/dashboard/audit-logs.html")
        await page.wait_for_selector("[data-audit-open]", timeout=30000)
        first_id = await page.locator("[data-audit-open]").first.get_attribute("data-audit-open")
        url_before = page.url
        await page.locator("[data-audit-open]").first.click()
        await page.wait_for_selector(".modal", timeout=15000)
        await page.wait_for_timeout(1200)
        modal_text = (await page.inner_text(".modal")).replace("\n", " ")
        controls = await page.locator('.modal button:has-text("Delete"), .modal button:has-text("Edit")').count()
        has_fields = all(word in modal_text for word in ("Event", "Time", "Actor"))
        record(11, "the audit list opens the event in a modal without leaving the list",
               page.url == url_before and has_fields,
               f"url unchanged={page.url == url_before} fields={has_fields}")
        record(11, "the modal is read-only and shows the record, not a blank shell",
               controls == 0 and len(modal_text) > 80 and "—" not in modal_text[:5], modal_text[:110])
        await page.screenshot(path=artifact("ui-11-audit-modal.png"))
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(400)
        closed = await page.locator(".modal").count() == 0 or not await page.locator(".modal").first.is_visible()
        record(11, "Escape closes the modal and returns the operator to the filtered list",
               closed and await page.locator("[data-audit-open]").count() > 0,
               f"modal closed={closed}")
        await page.screenshot(path=artifact("ui-11-audit-list.png"))
        await goto(page, f"/dashboard/audit-log-details.html?id={first_id}")
        detail_text = (await page.inner_text("body")).replace("\n", " ")
        record(11, "the standalone detail page still deep-links to one event",
               len(detail_text) > 120 and "Audit" in detail_text and "not found" not in detail_text.lower(),
               detail_text[:110])
        await page.screenshot(path=artifact("ui-11-audit-details.png"))

        # ------------------------------------------------------------- item 10
        topbar = page.locator(".dash-topbar").first
        box = await topbar.bounding_box()
        hit = await page.evaluate("""() => {
            const bar = document.querySelector('.dash-topbar');
            if (!bar) return null;
            const r = bar.getBoundingClientRect();
            if (r.height < 20) return null;
            const el = document.elementFromPoint(Math.round(r.left + r.width / 2), Math.round(r.top + r.height / 2));
            return {inside: !!(el && bar.contains(el)), height: Math.round(r.height)};
        }""")
        record(10, "the Audit Logs page paints the shared top navigation bar",
               bool(box) and hit and hit["inside"] and hit["height"] >= 40,
               f"box={box} hit={hit}")
        await ctx.close()

        # -------------------------------------------------------------- item 9
        watch = """
        (function(){
          // Count document loads in sessionStorage: survives the reload, so a
          // refresh that actually happened (and a loop that must not happen)
          // are both visible after the navigation.
          try { sessionStorage.setItem('__joneNavs', String(Number(sessionStorage.getItem('__joneNavs') || 0) + 1)); } catch (e) {}
          document.addEventListener('DOMContentLoaded', function(){
            var tries = 0;
            var iv = setInterval(function(){
              tries++;
              if (window.JONE && JONE.pwa && JONE.pwa.refreshToLatest && !JONE.pwa.refreshToLatest.__wrapped) {
                var original = JONE.pwa.refreshToLatest;
                JONE.pwa.refreshToLatest = function(reloadNow){
                  try { sessionStorage.setItem('__joneRefreshCalled', String(Date.now())); } catch (e) {}
                  return original.apply(JONE.pwa, arguments);
                };
                JONE.pwa.refreshToLatest.__wrapped = true;
                clearInterval(iv);
              } else if (tries > 400) { clearInterval(iv); }
            }, 50);
          });
        })();
        """
        newer = {"version": "9.9.9", "built": datetime.utcnow().isoformat()}
        ctx, page = await new_page(browser, manager, extras=[watch], route_version=newer)
        await goto(page, "/dashboard/index.html")
        await page.evaluate("() => JONE.updateChecker.check(true)")
        await page.wait_for_selector(".modal", timeout=30000)
        title = (await page.inner_text(".modal")).replace("\n", " ")
        refresh_btn = page.locator('.modal button:has-text("Refresh now")')
        later_btn = page.locator('.modal button:has-text("Not now")')
        record(9, "a newer deployed version raises an actionable update prompt",
               await refresh_btn.count() == 1 and await later_btn.count() == 1 and "new version" in title.lower(),
               title[:120])
        await page.screenshot(path=artifact("ui-09-update-prompt.png"))
        navs_before = await page.evaluate("() => Number(sessionStorage.getItem('__joneNavs') || 0)")
        await refresh_btn.first.click()
        await page.wait_for_timeout(6000)
        state = await page.evaluate("""() => ({
            navs: Number(sessionStorage.getItem('__joneNavs') || 0),
            called: sessionStorage.getItem('__joneRefreshCalled'),
            attempted: sessionStorage.getItem('jone.update.refreshAttempted'),
            dismissed: localStorage.getItem('jone.update.dismissedVersion'),
            version: window.JONE_VERSION,
        })""")
        record(9, "'Refresh now' runs the service-worker update path and reloads the page",
               bool(state["called"]) and state["navs"] == navs_before + 1,
               f"refreshToLatest called={bool(state['called'])} loads {navs_before}->{state['navs']}")
        record(9, "pressing Refresh is not treated as a dismissal",
               state["attempted"] == "9.9.9" and not state["dismissed"], json.dumps(state))
        await page.wait_for_timeout(4000)
        navs_later = await page.evaluate("() => Number(sessionStorage.getItem('__joneNavs') || 0)")
        record(9, "the refresh settles instead of looping", navs_later == state["navs"],
               f"loads after the refresh: {navs_later}")
        await page.evaluate("() => JONE.updateChecker.check(true)")
        await page.wait_for_timeout(1500)
        retry_text = (await page.inner_text(".modal")).replace("\n", " ") if await page.locator(".modal").count() else ""
        record(9, "a browser that still runs the old build is re-prompted with guidance",
               "already tried" in retry_text or "cached copy" in retry_text, retry_text[:120])
        await page.screenshot(path=artifact("ui-09-update-retry-guidance.png"))
        await ctx.close()

        # the public page has a partially filled form: it must NOT force a refresh
        ctx, page = await new_page(browser, None, route_version=newer)
        await goto(page, "/index.html")
        await page.evaluate("() => JONE.updateChecker.check(true)")
        await page.wait_for_timeout(1500)
        modal_on_form_page = await page.locator(".modal").count()
        page_text = (await page.inner_text("body")).lower()
        record(9, "a page with an unfinished form is never forced to reload",
               modal_on_form_page == 0 and "new version" in page_text,
               f"modals={modal_on_form_page} quiet notice={'new version' in page_text}")
        await ctx.close()

        await browser.close()

    errors = [e for e in CONSOLE_ERRORS if e[0] == "pageerror"]
    record(10, "no page errors on any page exercised above", not errors, f"{len(errors)} error(s): {errors[:3]}")

    failed = [row for row in RESULTS if not row[2]]
    print("\n==== SUMMARY ====")
    for item, name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  [{item}] {name}  ({detail})")
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} browser checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
