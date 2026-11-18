"""Destructive STAGING-ONLY browser integration test; requires seeded test rooms.

Run with Python Playwright, local fixture server, and environment variables:
TV_GALLERY_TEST_BASE=http://localhost:8765, TV_GALLERY_TEST_EMAIL,
TV_GALLERY_TEST_PASSWORD, TV_GALLERY_TEST_VIDEO, TV_GALLERY_TEST_AUDIO,
TV_GALLERY_TEST_ALLOW_MUTATIONS=1. Seed four active gallery rooms TEST-01 through
TEST-04 of type 'Test type'; give each two images, and no videos or music. This
script uploads media and changes gallery settings. Never point it at real data.
"""
import os
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

base = os.environ.get('TV_GALLERY_TEST_BASE', 'http://localhost:8765').rstrip('/')
if urlparse(base).hostname not in {'localhost', '127.0.0.1'} or os.environ.get('TV_GALLERY_TEST_ALLOW_MUTATIONS') != '1':
    raise SystemExit('Refusing to modify a non-local or unapproved environment.')
email = os.environ['TV_GALLERY_TEST_EMAIL']
password = os.environ['TV_GALLERY_TEST_PASSWORD']
video = os.environ['TV_GALLERY_TEST_VIDEO']
audio = os.environ['TV_GALLERY_TEST_AUDIO']
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--autoplay-policy=no-user-gesture-required'])
    context = browser.new_context(viewport={'width': 1920, 'height': 1080})
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(base + '/rooms-gallery/admin.html')
    page.locator('#email').fill(email); page.locator('#password').fill(password); page.locator('#login button').click()
    expect(page.locator('#admin')).to_be_visible(timeout=15000)
    page.locator('#room').select_option(label='ROOM TEST-01 — Test type')
    page.locator('#files').set_input_files(video); page.locator('#upload').click()
    expect(page.locator('#uploads')).to_contain_text('Uploaded', timeout=15000)
    expect(page.locator('#media-list')).to_contain_text(os.path.basename(video))
    # No background polling in the UI: refresh until the dedicated worker finishes.
    import time
    for _ in range(30):
        page.locator('#refresh-media').click()
        if 'ready' in page.locator('#media-list tr').filter(has_text=os.path.basename(video)).inner_text(): break
        time.sleep(1)
    else: raise AssertionError('Video worker did not complete')
    page.locator('#room').select_option(''); page.locator('#files').set_input_files(audio); page.locator('#upload').click()
    expect(page.locator('#media-list')).to_contain_text(os.path.basename(audio), timeout=15000)
    page.locator('#music-list').select_option(label=os.path.basename(audio)); page.locator('#music-enabled').check()
    page.locator('#interval').fill('2'); page.locator('#save-settings').click()
    expect(page.locator('#message')).to_have_text('Settings saved.')
    page.locator('#refresh-offline').click(); expect(page.locator('.offline-room')).to_have_count(4)
    page.locator('.offline-video').first.check(); page.locator('#sync').click()
    expect(page.locator('#offline-status')).to_contain_text('Ready for offline', timeout=30000)
    page.goto(base + '/rooms-gallery/'); expect(page.locator('.choice')).to_have_count(2)
    page.locator('#images').click(); expect(page.locator('.room-card')).to_have_count(4)
    page.locator('.room-card').first.click(); expect(page.locator('#overlay')).to_be_visible()
    src = page.locator('#photo').get_attribute('src'); page.wait_for_timeout(2300)
    assert page.locator('#photo').get_attribute('src') != src
    page.wait_for_timeout(2200); assert page.locator('#photo').get_attribute('src') == src
    page.keyboard.press('Escape'); expect(page.locator('#overlay')).to_be_hidden()
    assert page.evaluate('document.activeElement.classList.contains("room-card")')
    page.locator('#back-home').click(); page.locator('#videos').click()
    expect(page.locator('.room-card')).to_have_count(1); page.locator('.room-card').click(); page.wait_for_timeout(1200)
    assert page.locator('#video').evaluate('(v)=>v.muted && v.loop && !v.paused')
    assert not page.locator('#music').evaluate('(a)=>a.paused')
    page.locator('#close').click(); assert page.locator('#video').evaluate('(v)=>v.paused')
    assert not page.locator('#music').evaluate('(a)=>a.paused')
    page.locator('#back-home').click(); assert page.locator('#music').evaluate('(a)=>a.paused')
    context.set_offline(True); page.reload(); expect(page.locator('#connection')).to_contain_text('Offline', timeout=15000)
    page.locator('#images').click(); expect(page.locator('.room-card')).to_have_count(4)
    page.locator('.room-card').first.click(); page.wait_for_timeout(500)
    assert page.locator('#photo').evaluate('(i)=>i.complete && i.naturalWidth>0')
    page.locator('#close').click(); page.locator('#back-home').click(); page.locator('#videos').click()
    page.locator('.room-card').first.click(); page.wait_for_timeout(1200)
    assert page.locator('#video').evaluate('(v)=>v.readyState>=2 && !v.paused')
    assert not errors, errors
    print('PASS: login, uploads, settings, sync, slides, video, music, offline playback, focus')
    context.close(); browser.close()
