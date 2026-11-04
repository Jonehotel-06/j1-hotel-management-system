"""Opt-in browser checks against a LOCAL isolated deployment only.

Usage:
 DATABASE_URL=sqlite:////absolute/path/sandbox.sqlite3 python tools/verification/latest_repair_browser.py seed /private/login.json
 python tools/verification/latest_repair_browser.py test /private/login.json http://127.0.0.1:5500 results.json

Install Playwright and Chromium separately for verification. Never use production credentials.
The seed mode refuses non-SQLite databases; it must not be run on a production copy.
"""
import json
import os
from pathlib import Path
import secrets
import sys

ROOT=Path(__file__).resolve().parents[2]


def seed(path):
    sys.path.insert(0,str(ROOT/'backend'))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings.development')
    import django
    django.setup()
    from django.db import connection
    if connection.vendor != 'sqlite' or 'browser' not in connection.settings_dict['NAME']:
        raise RuntimeError('Refusing to seed: use a dedicated SQLite database with browser in its filename.')
    from django.core.management import call_command
    call_command('migrate',verbosity=0)
    from apps.accounts.models import User
    from apps.pos.models import MenuCategory,MenuItem,RestaurantTable
    from apps.rooms.models import RoomType,Room
    from apps.staff_operations.models import AttendanceRecord
    from django.utils import timezone
    from apps.core.utils import hotel_today
    from datetime import timedelta
    password=secrets.token_urlsafe(24)
    admin,_=User.objects.get_or_create(email='local-e2e-admin@example.test',defaults={'role':'ADMIN','first_name':'Local','last_name':'Verifier'})
    admin.set_password(password);admin.save()
    for area in ['BAR','RESTAURANT']:
        category,_=MenuCategory.objects.get_or_create(slug='e2e-'+area.lower(),defaults={'name':'E2E '+area,'service_area':area,'is_active':True})
        MenuItem.objects.get_or_create(sku='E2E-'+area,defaults={'category':category,'name':'E2E '+area+' item','base_price':'2000.00','is_active':True,'is_available':True})
    RestaurantTable.objects.get_or_create(code='E2E-T1',defaults={'seats':4,'is_active':True})
    kind,_=RoomType.objects.get_or_create(slug='e2e-standard',defaults={'name':'E2E Standard','base_price':'15000.00','max_guests':2})
    for number in ['1','6','20']:Room.objects.get_or_create(room_number=number,defaults={'room_type':kind,'is_active':True,'status':'AVAILABLE'})
    now=timezone.now()-timedelta(days=3)
    AttendanceRecord.objects.get_or_create(reference='ATT-E2E-HISTORY',defaults={'staff':admin,'business_date':hotel_today()-timedelta(days=3),'clock_in_at':now,'last_event_at':now})
    from tests.factories import make_booking,make_guest
    from tests.portal_stay_helpers import make_in_house_stay
    from apps.guest_services.models import ServiceRequest
    from apps.finance.services.folio_service import get_or_create_main_folio_for_stay
    from apps.portal.models import PortalSession
    from apps.portal.services import hash_secret
    from apps.pos.services.order_service import create_order,submit_order,transition_order
    guest=make_guest('local-browser-guest-'+secrets.token_hex(3)+'@example.test')
    room=Room.objects.get(room_number='20')
    booking=make_booking(guest,kind,[room],check_in=hotel_today(),check_out=hotel_today()+timedelta(days=2),status='CHECKED_IN')
    stay=make_in_house_stay(booking);folio,_=get_or_create_main_folio_for_stay(stay=stay,actor=admin)
    req=ServiceRequest.objects.create(reference='SRQ-BROWSER-'+secrets.token_hex(4),guest=guest,stay=stay,room=room,portal_email=guest.email,category='ROOM_SERVICE',channel='PORTAL',summary='Local browser receipt test',status='ACKNOWLEDGED')
    item=MenuItem.objects.get(sku='E2E-BAR')
    order,_=create_order(mode='ROOM_SERVICE',stay=stay,folio=folio,actor=admin,lines=[{'menu_item_id':item.pk,'quantity':1}],idempotency_key='browser-guest-'+secrets.token_hex(5))
    order.service_request=req;order.save(update_fields=['service_request'])
    submit_order(order=order,actor=admin);transition_order(order=order,target_status='DELIVERED',actor=admin)
    portal_token=secrets.token_urlsafe(48)
    PortalSession.objects.create(email=guest.email,session_hash=hash_secret(portal_token),expires_at=timezone.now()+timedelta(hours=2))
    Path(path).write_text(json.dumps({'email':admin.email,'password':password,'portal_token':portal_token,'request_reference':req.reference}));os.chmod(path,0o600)
    print('Local isolated fixtures prepared. No production connection used.')


def test(path,base,output):
    from urllib.parse import urlparse
    if urlparse(base).hostname not in {'127.0.0.1','localhost'}:raise RuntimeError('Local test origin required.')
    from playwright.sync_api import sync_playwright,expect
    credentials=json.loads(Path(path).read_text());results=[]
    def passed(name):results.append({'name':name,'status':'PASSED'})
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
        context=browser.new_context(viewport={'width':1440,'height':1000},service_workers='block')
        context.add_init_script('window.__APP_CONFIG__={API_BASE_URL:window.location.origin};')
        page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        try:
            page.goto(base+'/login.html');page.locator('#email').fill(credentials['email']);page.locator('#password').fill(credentials['password']);page.locator('#login-form button[type=submit]').click();page.wait_for_url('**/dashboard/**',timeout=20000);passed('Actual staff login UI against isolated Django API')
            page.goto(base+'/dashboard/pos.html');page.wait_for_function('document.querySelector("#pos-unfinished").options.length > 0');passed('POS starts with a staff-scoped unfinished cart')
            page.locator('#pos-mode').select_option('BAR');page.wait_for_function('Array.from(document.querySelector("#pos-item").options).some(o=>o.text.includes("E2E BAR item"))')
            bar_item=page.locator('#pos-item option').filter(has_text='E2E BAR item').get_attribute('value');page.locator('#pos-item').select_option(bar_item);page.locator('#pos-add-line').click();expect(page.locator('[data-pos-cart]')).to_contain_text('E2E BAR item');first=page.locator('#pos-unfinished').input_value()
            page.locator('#pos-mode').select_option('TAKEAWAY');page.wait_for_function('document.querySelector("#pos-category").options.length===2');passed('Walk-in/Takeaway displays restaurant and bar categories')
            expect(page.locator('[data-pos-cart]')).not_to_contain_text('E2E BAR item');page.locator('#pos-new-unfinished').click();second=page.locator('#pos-unfinished').input_value();assert first!=second
            page.locator('#pos-unfinished').select_option(first);expect(page.locator('[data-pos-cart]')).to_contain_text('E2E BAR item');expect(page.locator('#pos-mode')).to_have_value('BAR');passed('Switching independent unfinished orders preserves mode and items without merging')
            page.reload();page.wait_for_function('document.querySelector("#pos-unfinished").options.length >= 3');expect(page.locator('[data-pos-cart]')).to_contain_text('E2E BAR item');passed('Unfinished cart survives page reload under same staff identity')
            page.locator('#pos-guest').fill('Local browser sale');page.locator('#pos-create').click();expect(page.locator('[data-tbody=pos-orders]')).to_contain_text('Local browser sale',timeout=15000);passed('Independent draft submission reaches Django and appears only after backend confirmation')
            expect(page.locator('#pos-mode')).to_be_enabled();passed('Submission guard releases mode selection after successful draft')
            page.goto(base+'/dashboard/workforce.html');expect(page.locator('[data-wf-attendance-rows]')).to_contain_text('Local Verifier',timeout=15000);passed('Historical attendance is visible on initial Workforce load')
            page.locator('#wf-attendance-search').fill('no-such-employee');expect(page.locator('[data-wf-attendance-rows]')).to_contain_text('No attendance records found');passed('Attendance employee search returns truthful empty state')
            page.goto(base+'/dashboard/service-requests.html');expect(page.locator('#sr-status option[value=IN_PROGRESS]')).to_have_count(0);expect(page.locator('#sr-status option[value=CANCELLED]')).to_have_count(0);passed('Staff Guest Requests no longer offers retired status options')
            page.set_viewport_size({'width':390,'height':844});page.goto(base+'/dashboard/pos.html');page.wait_for_function('document.querySelector("#pos-unfinished").options.length > 0');page.locator('#pos-mode').select_option('TAKEAWAY');page.wait_for_function('document.querySelector("#pos-category").options.length===2');passed('Mobile POS category/session controls operate at 390px width')
            # Exercise the guest-facing receipt action with a real isolated portal cookie.
            guest_context=browser.new_context(service_workers='block')
            guest_context.add_init_script('window.__APP_CONFIG__={API_BASE_URL:window.location.origin};')
            guest_context.add_cookies([{'name':'jone_portal_session','value':credentials['portal_token'],'domain':'127.0.0.1','path':'/api/portal/','httpOnly':True,'secure':False,'sameSite':'Lax'}])
            guest_page=guest_context.new_page();guest_page.goto(base+'/portal/index.html');guest_page.locator('[data-portal-tab=requests]').click()
            delivery=guest_page.locator('[data-portal-request-delivered="'+credentials['request_reference']+'"]')
            expect(delivery).to_be_visible(timeout=15000);delivery.click()
            guest_page.locator('.modal.open').get_by_role('button',name='Delivered',exact=True).click()
            expect(guest_page.locator('[data-portal-request-delivered="'+credentials['request_reference']+'"]')).to_have_count(0,timeout=15000)
            passed('Verified guest confirms own receipt through Delivered UI and the confirmation action disappears')
            guest_context.close()
            if errors:raise AssertionError('Browser JavaScript errors: '+ '; '.join(errors))
            passed('No uncaught JavaScript page errors across tested workflows')
        except Exception as exc:
            results.append({'name':'Browser workflow execution','status':'FAILED','reason':str(exc)[:1200]})
        finally:
            browser.close();Path(output).write_text(json.dumps(results,indent=2));print(json.dumps(results,indent=2))
    if any(row['status']=='FAILED' for row in results):raise SystemExit(1)

if __name__=='__main__':
    if sys.argv[1]=='seed':seed(sys.argv[2])
    elif sys.argv[1]=='test':test(*sys.argv[2:5])
    else:raise SystemExit('Choose seed or test')
