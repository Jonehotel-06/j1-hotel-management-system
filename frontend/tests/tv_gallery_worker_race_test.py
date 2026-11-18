"""Local-only service-worker duplicate/cancel/validation checks."""
import os
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright

base = os.environ['TV_GALLERY_TEST_BASE'].rstrip('/')
assert urlsplit(base).hostname in ('localhost', '127.0.0.1'), 'Local staging only'
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()
    page.goto(base + '/rooms-gallery/')
    result = page.evaluate('''async () => {
      await navigator.serviceWorker.ready;
      const reg = await navigator.serviceWorker.getRegistration('/rooms-gallery/');
      function message(job){return new Promise((resolve,reject)=>{
        const channel=new MessageChannel(),timer=setTimeout(()=>reject(new Error('No worker response')),10000);
        channel.port1.onmessage=e=>{if(e.data.type==='done'||e.data.type==='error'){
          clearTimeout(timer);channel.port1.close();resolve(e.data);
        }};
        reg.active.postMessage({type:'TV_GALLERY_SYNC',job},[channel.port2]);
      });}
      const bad=await message('0000000000000000');
      const catalog=await JoneOffline.freshCatalog();
      const item=catalog.data.rooms.flatMap(r=>r.media).find(m=>m.thumbnail);
      if(!item)throw Error('No thumbnail fixture');
      const id='1234567890abcdef1234567890abcdef';
      const job={job:id,phase:'pending',activeCache:null,pendingCache:'jone-tv-download-v2-'+id,
                 urls:[item.thumbnail],catalogText:catalog.text,catalogUrl:location.origin+'/api/rooms-gallery/catalog/',error:''};
      await JoneSyncDB.write(job);
      const both=await Promise.all([message(id),message(id)]);
      const ready=await JoneOffline.status();
      const staleId='abcdef1234567890abcdef1234567890';
      await JoneSyncDB.write(Object.assign({},job,{job:staleId,pendingCache:'jone-tv-download-v2-'+staleId,catalogText:'{}'}));
      const stale=await message(staleId);
      const pending=await JoneOffline.status();
      let unsafe='not rejected';
      try {await JoneOffline.sync({text:'{}',data:{rooms:[{id:1,media:[{kind:'image',url:'https://example.com/not-media'}]}]}},['1'],[]);}
      catch(e){unsafe=e.message;}
      await JoneOffline.remove();
      return {bad:bad,both:both,ready:ready,stale:stale,pending:pending,unsafe:unsafe,removed:await JoneOffline.status()};
    }''')
    assert result['bad']['type'] == 'error', result
    assert all(m['type'] == 'done' for m in result['both']), result
    assert result['ready']['ready'], result
    assert result['stale']['type'] == 'error' and 'changed' in result['stale']['message'].lower(), result
    assert not result['pending']['ready'] and not result['removed']['ready'], result
    assert 'Unexpected media URL' in result['unsafe'], result
    print('PASS: invalid job replied, duplicate listeners completed, stale catalog rejected, unsafe URL rejected, remove cleared readiness')
    browser.close()
