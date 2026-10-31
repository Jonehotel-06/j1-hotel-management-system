"""Playwright component smoke test against the actual Batch 08 panel and script.
Run from repository root: python frontend/tests/batch08_mobile.py
Playwright and Chromium are test-only dependencies; API replies are fixtures.
"""
from pathlib import Path
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[2]
html = (root / 'frontend/dashboard/housekeeping.html').read_text()
css = html[html.index('<style>'):html.index('</style>') + 8]
start = html.index('<section class="panel" id="asset-panel">')
end = html.index('</section><section class="panel">', start) + len('</section>')
panel = html[start:end]
fixture = """
window.__housekeepingAllowed=true;
window.APP_CONFIG={API_ENDPOINTS:{housekeepingTasks:'/api/admin/housekeeping/'}};
window.JONE={esc:s=>String(s).replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]))};
window.calls=[]; window.API={
 get:async path=>{ if(path.endsWith('queue/')) return {data:[{assignment_id:3,room:'101',room_type:'Suite',booking:'BK1',phase:'PRE'}],pagination:{total_pages:1}};
 if(path.endsWith('configuration/')) throw {status:403}; return {data:[]}; },
 post:async (path,body)=>{calls.push({path,body}); if(path.endsWith('inspections/')) return {data:{id:7,phase:'PRE',room:'101',booking:'BK1',revision:0,draft:{},checklist:[{item_id:1,name:'Hangers',expected:5}]}};
 return {data:{id:7,phase:'PRE',room:'101',booking:'BK1',revision:1,draft:{},checklist:[{item_id:1,name:'Hangers',expected:5}],completed_at:body.complete?'2026-01-01T00:00:00Z':null}}; }
};
"""
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
    for width, height in [(360, 800), (1280, 800)]:
        page = browser.new_page(viewport={'width': width, 'height': height})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.set_content('<!doctype html><html><head>'+css+'</head><body>'+panel+'</body></html>')
        page.add_script_tag(content=fixture)
        page.add_script_tag(path=str(root / 'frontend/js/room-assets.js'))
        page.get_by_role('button', name='Open checklist').click()
        page.get_by_role('button', name='Complete verification').click()
        assert 'Check every item' in page.locator('#asset-message').inner_text()
        page.locator('[name=checked]').check()
        page.locator('[name=actual]').fill('3')
        assert '2 missing' in page.locator('.asset-result').inner_text()
        page.get_by_role('button', name='Complete verification').click()
        assert 'completed successfully' in page.locator('#asset-message').inner_text()
        assert page.evaluate('calls[calls.length-1].body.lines[0].actual') == 3
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        assert not errors, errors
        print('PASS viewport', width, 'checklist validation, discrepancy, submission and no horizontal overflow')
        page.close()
    browser.close()
