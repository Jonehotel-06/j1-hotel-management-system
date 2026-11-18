"""Staging-only CDP-forced Background Sync test; never run against production.

Requires TV_GALLERY_TEST_BASE=http://localhost:8765, TV_GALLERY_TEST_EMAIL,
TV_GALLERY_TEST_PASSWORD, and a staged active room with a processed video.
This does not establish that the browser will schedule sync automatically.
"""
import os
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect

base = os.environ['TV_GALLERY_TEST_BASE'].rstrip('/')
parsed = urlsplit(base)
assert parsed.hostname in ('localhost', '127.0.0.1'), 'Local staging only'
email, password = os.environ['TV_GALLERY_TEST_EMAIL'], os.environ['TV_GALLERY_TEST_PASSWORD']
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
    context = browser.new_context()
    page = context.new_page()
    cdp = context.new_cdp_session(page)
    registrations = []
    cdp.on('ServiceWorker.workerRegistrationUpdated', lambda event: registrations.extend(event['registrations']))
    cdp.send('ServiceWorker.enable')
    page.goto(base + '/rooms-gallery/admin.html')
    page.fill('#email', email); page.fill('#password', password)
    page.click('#login button'); expect(page.locator('#admin')).to_be_visible()
    page.click('#refresh-offline'); expect(page.locator('.offline-room').first).to_be_visible()
    expect(page.locator('.offline-video').first).to_be_visible(); page.locator('.offline-video').first.check()
    context.set_offline(True); page.click('#sync')
    expect(page.locator('#offline-status')).to_contain_text('pending retry', timeout=10000)
    state = page.evaluate('JoneSyncDB.read()')
    assert state['phase'] == 'pending'
    assert not page.evaluate('JoneOffline.status()')['ready']
    context.set_offline(False)
    regs = [r for r in registrations if r['scopeURL'] == base + '/rooms-gallery/' and not r['isDeleted']]
    assert regs, 'service worker registration not found via CDP'
    cdp.send('ServiceWorker.dispatchSyncEvent', {'origin': base, 'registrationId': regs[-1]['registrationId'],
                                                'tag': 'jone-tv-download-v2', 'lastChance': False})
    # An async predicate is not awaited by all Playwright versions.
    for _ in range(40):
        page.wait_for_timeout(250)
        if page.evaluate('JoneOffline.status()')['ready']:
            break
    else:
        raise AssertionError('Forced worker retry did not complete')
    context.set_offline(True)
    page.goto(base + '/rooms-gallery/')
    page.locator('#videos').click(); page.locator('.room-card').first.click()
    page.wait_for_function("document.querySelector('#video').readyState >= 2", timeout=10000)
    print('PASS: failed sync stayed pending; CDP-forced retry completed; selected video playable offline')
    browser.close()
