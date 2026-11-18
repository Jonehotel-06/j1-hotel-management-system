"""Local-only admin UI check: broker failure is not reported as queued."""
import json
import os
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect

base = os.environ['TV_GALLERY_TEST_BASE'].rstrip('/')
assert urlsplit(base).hostname in ('localhost', '127.0.0.1'), 'Local staging only'
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    def media(route):
        response = route.fetch()
        data = response.json()
        data['data'].append({'id': 999999, 'room_id': None, 'kind': 'video', 'name': 'staging-failed.mp4',
                             'size': 123, 'active': True, 'is_cover': False, 'order': 0,
                             'processing_status': 'failed', 'processing_error': 'Queue unavailable.'})
        route.fulfill(response=response, body=json.dumps(data))
    def retry(route):
        route.fulfill(status=200, content_type='application/json', body=json.dumps({'success': True,
          'data': {'id': 999999, 'processing_status': 'failed', 'processing_error': 'Queue unavailable.'}}))
    page.route('**/api/rooms-gallery/manage/media/', media)
    page.route('**/api/rooms-gallery/manage/media/999999/retry-processing/', retry)
    page.goto(base + '/rooms-gallery/admin.html')
    page.fill('#email', os.environ['TV_GALLERY_TEST_EMAIL'])
    page.fill('#password', os.environ['TV_GALLERY_TEST_PASSWORD'])
    page.click('#login button'); expect(page.locator('#admin')).to_be_visible()
    row = page.locator('#media-list tr').filter(has_text='staging-failed.mp4')
    expect(row).to_be_visible(); row.get_by_role('button', name='Retry processing').click()
    expect(page.locator('#message')).to_have_text('Retry failed: Queue unavailable.')
    print('PASS: failed broker retry is not presented as queued')
    browser.close()
