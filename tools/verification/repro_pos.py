import asyncio, json, sys
from jone_verify import ADMIN, API_BASE as API, Session, WEB_BASE as BASE, artifact
from playwright.async_api import async_playwright

async def main():
    s = Session(ADMIN)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width":1440,"height":1000})
        await ctx.add_init_script(s.init_script())
        page = await ctx.new_page()
        calls = []
        async def on_response(resp):
            if "/api/admin/pos/" in resp.url:
                calls.append((resp.request.method, resp.url.replace(WEB_BASE,""), resp.status))
        page.on("response", lambda r: asyncio.ensure_future(on_response(r)))
        errs = []
        page.on("console", lambda m: errs.append((m.type, m.text)) if m.type == "error" else None)
        page.on("pageerror", lambda e: errs.append(("pageerror", str(e))))

        await page.goto(BASE + "/dashboard/pos.html", wait_until="networkidle")
        await page.wait_for_timeout(2500)

        # INVALID CASE first: pick RESTAURANT without a table session, add an item, submit
        await page.select_option("#pos-mode", "RESTAURANT")
        await page.wait_for_timeout(600)
        cats = await page.eval_on_selector_all("#pos-category option", "els => els.map(e=>e.value).filter(Boolean)")
        print("categories:", cats[:5])
        if cats:
            await page.select_option("#pos-category", cats[0]); await page.wait_for_timeout(500)
        items = await page.eval_on_selector_all("#pos-item option", "els => els.map(e=>({v:e.value,t:e.textContent}))")
        print("items:", items[:6])
        await page.select_option("#pos-item", items[0]["v"]); await page.wait_for_timeout(400)
        await page.click("#pos-add-line"); await page.wait_for_timeout(400)
        await page.click("#pos-create")
        await page.wait_for_timeout(1200)
        box = await page.evaluate("""() => {
            const e = document.querySelector('[data-pos-create-error]');
            const cs = getComputedStyle(e);
            const r = e.getBoundingClientRect();
            return {hiddenAttr: e.hidden, text: e.textContent, display: cs.display, visibility: cs.visibility,
                    height: r.height, width: r.width, padding: cs.padding};
        }""")
        print("ERROR BOX (after invalid submit):", json.dumps(box))
        await page.screenshot(path=artifact("pos-invalid-submit.png"))
        print("api calls:", calls[-6:])
        print("console errors:", errs[:10])

        # VALID CASE: TAKEAWAY with 3 items
        calls.clear()
        await page.select_option("#pos-mode", "TAKEAWAY"); await page.wait_for_timeout(500)
        for i in range(3):
            cat = cats[i % len(cats)]
            await page.select_option("#pos-category", cat); await page.wait_for_timeout(300)
            its = await page.eval_on_selector_all("#pos-item option", "els => els.map(e=>e.value).filter(Boolean)")
            if not its: continue
            await page.select_option("#pos-item", its[i % len(its)]); await page.wait_for_timeout(250)
            await page.click("#pos-add-line"); await page.wait_for_timeout(300)
        cart = await page.evaluate("() => document.querySelector('[data-pos-cart]').innerText.slice(0,200)")
        print("CART:", cart.replace("\n"," | "))
        await page.click("#pos-create")
        await page.wait_for_timeout(2500)
        after = await page.evaluate("""() => {
            const e = document.querySelector('[data-pos-create-error]');
            return {errHidden: e.hidden, errText: e.textContent,
                    cart: document.querySelector('[data-pos-cart]').innerText.slice(0,120),
                    rows: document.querySelector('[data-tbody=pos-orders]').innerText.slice(0,300),
                    toast: (document.querySelector('.toast, .jone-toast, [data-toast]')||{}).innerText};
        }""")
        print("AFTER VALID SUBMIT:", json.dumps(after))
        print("api calls:", calls)
        print("console errors:", errs[:10])
        await page.screenshot(path=artifact("pos-valid-submit.png"))
        await browser.close()
asyncio.run(main())
