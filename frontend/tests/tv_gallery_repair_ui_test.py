import json
import os
from playwright.sync_api import sync_playwright
base=os.environ.get('TV_GALLERY_TEST_BASE','http://127.0.0.1:8765').rstrip('/')
assert base.startswith(('http://127.0.0.1:', 'http://localhost:')), 'Local staging only'
with sync_playwright() as p:
 b=p.chromium.launch(headless=True,args=['--no-sandbox'])
 for width in [320,360,375,390,414,768,1280]:
  page=b.new_page(viewport={'width':width,'height':900},service_workers='block')
  page.add_init_script("sessionStorage.setItem('jone.tv.admin.token','test-token')")
  state={'post':0,'fail_refresh':False}
  def route(r):
   path=r.request.url.split('?')[0]; method=r.request.method
   if path.endswith('/manage/rooms/'): data=[{'id':1,'number':'202','type':'Kings and Queens Court','active':True,'eligible':True}]
   elif path.endswith('/manage/settings/'): data={'slideshow_seconds':5,'max_upload_mb':128,'music_volume':50,'music_enabled':False,'music_autoplay':False,'music_id':None}
   elif path.endswith('/manage/media/') and method=='GET':
    if state['fail_refresh']: r.fulfill(status=503,json={'success':False,'message':'refresh failed'});return
    data=[{'id':1,'room_id':1,'kind':'image','name':'longfilename'*14+'.jpg','order':1,'active':True,'is_cover':False}]
   elif path.endswith('/manage/media/') and method=='POST':
    state['post']+=1;state['fail_refresh']=True; r.fulfill(status=201,json={'success':True,'data':{'id':2,'kind':'image'}});return
   else: data={'rooms':[]}
   r.fulfill(status=200,json={'success':True,'data':data})
  page.route('**/api/rooms-gallery/**',route);page.goto(base+'/rooms-gallery/admin.html');page.locator('#admin').wait_for();page.select_option('#room','1')
  overflow=page.evaluate('document.documentElement.scrollWidth > innerWidth');assert not overflow,(width,page.evaluate('document.documentElement.scrollWidth'))
  for sel in ['#room','#files','#upload','#save-room','#interval','#max-upload','#music-list','#volume','#save-settings','#refresh-offline','#sync','#remove-offline','#media-list']:
   box=page.locator(sel).bounding_box();assert box and box['x']>=-1 and box['x']+box['width']<=width+1,(width,sel,box)
  if width==320:
   page.locator('#files').set_input_files({'name':'test.jpg','mimeType':'image/jpeg','buffer':b'test'})
   page.locator('#upload').click();page.get_by_text('list refresh failed',exact=False).wait_for();assert page.locator('.upload-row button').is_hidden();assert state['post']==1
  print('PASS',width,'no horizontal overflow, controls inside viewport'+(', successful upload + failed refresh cannot retry' if width==320 else ''))
  page.close()
 page=b.new_page(service_workers='block')
 page.add_init_script("sessionStorage.setItem('jone.tv.admin.token','test-token')")
 state={'posts':0,'mode':'validation'}
 def retry_route(r):
  path=r.request.url.split('?')[0]
  if path.endswith('/manage/rooms/'): data=[{'id':1,'number':'202','type':'Court','active':True,'eligible':True}]
  elif path.endswith('/manage/settings/'): data={'slideshow_seconds':5,'max_upload_mb':128,'music_volume':50,'music_enabled':False,'music_autoplay':False,'music_id':None}
  elif path.endswith('/manage/media/') and r.request.method=='POST':
   state['posts']+=1
   if state['mode']=='validation':
    state['mode']='success';r.fulfill(status=400,json={'success':False,'message':'Validation failed.','data':{'file':['Image could not be decoded safely.']}});return
   if state['mode']=='uncertain':
    r.fulfill(status=500,json={'success':False,'message':'Server error','data':None});return
   data={'id':3,'kind':'image'}
  else: data=[]
  r.fulfill(status=201 if r.request.method=='POST' else 200,json={'success':True,'data':data})
 page.route('**/api/rooms-gallery/**',retry_route);page.goto(base+'/rooms-gallery/admin.html');page.locator('#admin').wait_for()
 page.locator('#files').set_input_files({'name':'bad.jpg','mimeType':'image/jpeg','buffer':b'bad'});page.locator('#upload').click()
 page.get_by_text('Image could not be decoded safely.',exact=False).wait_for();page.locator('.upload-row button').click();page.get_by_text('Uploaded',exact=False).wait_for();assert state['posts']==2
 state['mode']='uncertain';page.locator('#files').set_input_files({'name':'maybe.jpg','mimeType':'image/jpeg','buffer':b'maybe'});page.locator('#upload').click();page.get_by_text('server may have saved',exact=False).wait_for();assert page.locator('.upload-row').last.locator('button').is_hidden();assert state['posts']==3
 print('PASS mocked 400 exposes reason and retry; ambiguous 500 blocks unsafe retry')
 page.close()
 b.close()
