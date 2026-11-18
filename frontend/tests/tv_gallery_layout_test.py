"""Local browser viewport/keyboard smoke test against seeded staging data."""
import os
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

base = os.environ.get('TV_GALLERY_TEST_BASE', 'http://localhost:8765').rstrip('/')
if urlparse(base).hostname not in {'localhost', '127.0.0.1'}:
    raise SystemExit('Local staging only.')
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
    for width, height in [(1920,1080), (3840,2160), (1280,720), (390,844), (844,390)]:
        context = browser.new_context(viewport={'width':width, 'height':height})
        page = context.new_page(); page.goto(base + '/rooms-gallery/'); page.wait_for_timeout(500)
        assert page.locator('.choice').count() == 2
        for button in page.locator('.choice').all():
            rect = button.bounding_box()
            assert rect and rect['x'] >= 0 and rect['y'] >= 0
            assert rect['x'] + rect['width'] <= width + 1 and rect['y'] + rect['height'] <= height + 1
        page.keyboard.press('ArrowDown' if width < 700 else 'ArrowRight')
        assert page.evaluate('document.activeElement.id') == 'videos'
        page.keyboard.press('ArrowUp' if width < 700 else 'ArrowLeft')
        assert page.evaluate('document.activeElement.id') == 'images'
        page.keyboard.press('Enter'); expect(page.locator('.room-card').first).to_be_visible()
        for card in page.locator('.room-card').all():
            rect = card.bounding_box()
            assert rect['x'] >= 0 and rect['y'] >= 0
            assert rect['x'] + rect['width'] <= width + 1 and rect['y'] + rect['height'] <= height + 1
        page.keyboard.press('Enter'); expect(page.locator('#overlay')).to_be_visible()
        page.keyboard.press('Escape'); expect(page.locator('#overlay')).to_be_hidden()
        print('PASS', width, height); context.close()
    browser.close()
