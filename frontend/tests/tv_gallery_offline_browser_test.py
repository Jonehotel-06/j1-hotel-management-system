"""Local staging offline smoke in Chromium and Firefox; no production writes."""
import os
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect

base = os.environ['TV_GALLERY_TEST_BASE'].rstrip('/')
assert urlsplit(base).hostname in ('localhost', '127.0.0.1'), 'Local staging only'
email = os.environ['TV_GALLERY_TEST_EMAIL']
password = os.environ['TV_GALLERY_TEST_PASSWORD']
with sync_playwright() as p:
    for name in ('chromium', 'firefox'):
        browser = getattr(p, name).launch(headless=True)
        context = browser.new_context(viewport={'width': 1280, 'height': 720})
        page = context.new_page()
        page.goto(base + '/rooms-gallery/admin.html')
        page.fill('#email', email); page.fill('#password', password)
        page.click('#login button'); expect(page.locator('#admin')).to_be_visible()
        page.click('#refresh-offline')
        expect(page.locator('.offline-room').first).to_be_visible()
        expect(page.locator('.offline-video').first).to_be_visible()
        page.locator('.offline-video').first.check(); page.click('#sync')
        expect(page.locator('#offline-status')).to_contain_text('Ready for offline', timeout=30000)
        assert page.evaluate('JoneOffline.status()')['ready']
        context.set_offline(True)
        page.goto(base + '/rooms-gallery/')
        expect(page.locator('#images')).to_be_visible(); page.click('#images')
        page.locator('.room-card').first.click(); expect(page.locator('#photo')).to_be_visible()
        assert page.locator('#photo').evaluate('(i) => i.complete && i.naturalWidth > 0')
        page.keyboard.press('Escape'); page.keyboard.press('Escape')
        page.click('#videos'); page.locator('.room-card').first.click()
        page.wait_for_function("document.querySelector('#video').readyState >= 2", timeout=10000)
        assert page.locator('#video').evaluate('(v) => v.muted && v.loop')
        print('PASS:', name, 'selected image and muted looping video available offline')
        browser.close()
