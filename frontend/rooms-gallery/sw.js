/* Dedicated Rooms Gallery worker: never cache authenticated/admin requests. */
'use strict';
importScripts('/rooms-gallery/sync-db.js');
const SHELL='jone-tv-shell-v2', PREFIX='jone-tv-download-v2-', TAG='jone-tv-download-v2';
const PATHS=['/rooms-gallery/','/rooms-gallery/gallery.css','/rooms-gallery/gallery.js','/rooms-gallery/offline.js','/rooms-gallery/sync-db.js','/rooms-gallery/grid.js','/rooms-gallery/brand.png','/rooms-gallery/icon-192.png','/rooms-gallery/icon-512.png','/rooms-gallery/icon.svg','/rooms-gallery/manifest.webmanifest'];
const running=new Map();
self.addEventListener('install',e=>e.waitUntil(caches.open(SHELL).then(c=>c.addAll(PATHS))));
self.addEventListener('activate',e=>e.waitUntil((async()=>{
  const keys=await caches.keys();await Promise.all(keys.filter(k=>k.startsWith('jone-tv-shell-')&&k!==SHELL).map(k=>caches.delete(k)));
  await self.clients.claim();
})()));
function apiOrigin(url){return url.origin===self.location.origin||url.origin==='https://api.jonehotels.com';}
function isCatalog(url){return apiOrigin(url)&&url.pathname==='/api/rooms-gallery/catalog/';}
function isMedia(url){return apiOrigin(url)&&/^\/api\/rooms-gallery\/media\/\d+\/(thumb|display|file)\/$/.test(url.pathname)&&!url.search&&!url.hash;}
function validJob(job){
  if(!job||!/^[-a-f0-9]{16,64}$/.test(job.job)||job.pendingCache!==PREFIX+job.job||!Array.isArray(job.urls)||job.urls.length>10000||typeof job.catalogText!=='string'||job.catalogText.length>5000000)return false;
  try{const catalog=new URL(job.catalogUrl);return isCatalog(catalog)&&!catalog.search&&job.urls.every(s=>{const u=new URL(s);return u.origin===catalog.origin&&isMedia(u);});}catch(_){return false;}
}
function notify(port,data){if(port){try{port.postMessage(data);}catch(_){}}}
async function announce(){const clients=await self.clients.matchAll({type:'window',includeUncontrolled:true});clients.forEach(client=>client.postMessage({type:'TV_GALLERY_OFFLINE_CHANGED'}));}
async function deleteOldCaches(keep){
  const names=await caches.keys();await Promise.all(names.filter(n=>(n.startsWith(PREFIX)||n==='jone-tv-media-v1'||n==='jone-tv-meta-v1')&&n!==keep).map(n=>caches.delete(n)));
}
async function startJob(jobId,port){
  // Joiners must get a terminal message too: a Background Sync event may
  // already be processing this job when the admin page starts listening.
  if(running.has(jobId))return running.get(jobId).promise.then(
    ()=>notify(port,{type:'done'}),
    error=>{notify(port,{type:'error',message:String(error.message||'Synchronization failed.')});throw error;});
  const controller=new AbortController();
  const promise=(async()=>{
    let state=await JoneSyncDB.read();
    if(!state||state.job!==jobId||state.phase!=='pending'||!validJob(state))throw new Error('Synchronization was cancelled or is invalid.');
    const cache=await caches.open(state.pendingCache);
    try{
      for(let i=0;i<state.urls.length;i++){
        const current=await JoneSyncDB.read();if(!current||current.job!==jobId||current.phase!=='pending')throw new Error('Synchronization cancelled.');
        if(!(await cache.match(state.urls[i]))){
          if(self.navigator.storage&&self.navigator.storage.estimate){const q=await self.navigator.storage.estimate();if(q.quota&&q.usage/q.quota>0.95)throw new Error('Storage quota is nearly full.');}
          let response;
          try{response=await fetch(state.urls[i],{mode:'cors',credentials:'omit',cache:'no-store',redirect:'error',signal:controller.signal});}catch(e){if(e.name==='AbortError')throw e;const err=new Error('Network unavailable; retry when online.');err.retryable=true;throw err;}
          if(!response.ok||response.status!==200||response.type==='opaque'){const err=new Error('Media download failed: HTTP '+response.status);err.retryable=response.status===429||response.status>=500;throw err;}
          await cache.put(state.urls[i],response);
        }
        notify(port,{type:'progress',done:i+1,total:state.urls.length});
      }
      // A selection cannot become ready against stale server metadata.
      let latest;
      try { latest=await fetch(state.catalogUrl+'?fresh=1',{mode:'cors',credentials:'omit',cache:'no-store',redirect:'error',signal:controller.signal}); }
      catch(e){if(e.name==='AbortError')throw e;const err=new Error('Network unavailable; retry when online.');err.retryable=true;throw err;}
      if(!latest.ok||latest.type==='opaque'){const err=new Error('Could not verify the current catalog.');err.retryable=latest.status===429||latest.status>=500;throw err;}
      if((await latest.text())!==state.catalogText)throw new Error('Gallery changed; refresh selections before synchronizing.');
      await cache.put(state.catalogUrl,new Response(state.catalogText,{headers:{'Content-Type':'application/json'}}));
      const changed=await JoneSyncDB.compare(jobId,s=>Object.assign({},s,{phase:'ready',activeCache:s.pendingCache,pendingCache:null,savedAt:new Date().toISOString(),error:'',outdated:false}));
      if(!changed){await caches.delete(state.pendingCache);throw new Error('Synchronization cancelled.');}
      // Remove only the previously active generation. A concurrent new job may
      // already have opened another pending cache, so do not sweep its prefix.
      if(state.activeCache && state.activeCache!==state.pendingCache) await caches.delete(state.activeCache);
      await caches.delete('jone-tv-media-v1'); await caches.delete('jone-tv-meta-v1');
      await announce();notify(port,{type:'done'});
    }catch(error){
      const current=await JoneSyncDB.read();
      if(current&&current.job===jobId&&current.phase==='pending'){
        await JoneSyncDB.compare(jobId,s=>Object.assign({},s,{error:String(error.message||'Synchronization failed.')}));
        let queued=false;
        if(error.retryable&&self.registration.sync){try{await self.registration.sync.register(TAG);queued=true;}catch(_){}}
        await announce();notify(port,{type:'error',message:String(error.message||'Synchronization failed.'),queued:queued});
      }else{await caches.delete(state.pendingCache);notify(port,{type:'error',message:'Synchronization cancelled.',queued:false});}
      throw error;
    }
  })().finally(()=>running.delete(jobId));
  running.set(jobId,{controller:controller,promise:promise});return promise;
}
self.addEventListener('message',event=>{
  const data=event.data||{},port=event.ports&&event.ports[0];
  if(data.type==='TV_GALLERY_PING'){notify(port,{type:'pong',version:2});return;}
  if(data.type==='TV_GALLERY_SYNC')event.waitUntil((async()=>{if(self.registration.sync){try{await self.registration.sync.register(TAG);}catch(_){}}await startJob(data.job,port).catch(e=>notify(port,{type:'error',message:String(e.message||'Synchronization failed.')}));})());
  if(data.type==='TV_GALLERY_REMOVE')event.waitUntil((async()=>{
    await JoneSyncDB.write(null);
    const jobs=Array.from(running.values());jobs.forEach(j=>j.controller.abort());
    await Promise.all(jobs.map(j=>j.promise.catch(()=>{})));
    await deleteOldCaches(null);
    await announce();notify(port,{type:'removed'});
  })().catch(e=>notify(port,{type:'error',message:String(e.message||e)})));
});
self.addEventListener('sync',event=>{
  if(event.tag!==TAG)return;
  event.waitUntil((async()=>{const state=await JoneSyncDB.read();if(state&&state.phase==='pending'){try{await startJob(state.job,null);}catch(e){if(e.retryable)throw e;}}})());
});
async function cachedMedia(request){
  const state=await JoneSyncDB.read().catch(()=>null);
  const cache=state&&state.activeCache?await caches.open(state.activeCache):null;
  const found=cache?await cache.match(request.url):null;
  if(!found)return fetch(request);
  const range=request.headers.get('Range');if(!range)return found;
  const match=/^bytes=(\d*)-(\d*)$/.exec(range);if(!match||(!match[1]&&!match[2]))return fetch(request);
  const blob=await found.blob();let start,end;
  if(match[1]){start=Number(match[1]);end=match[2]?Math.min(Number(match[2]),blob.size-1):blob.size-1;}
  else{start=Math.max(0,blob.size-Number(match[2]));end=blob.size-1;}
  if(start>=blob.size||start>end)return new Response(null,{status:416,headers:{'Content-Range':'bytes */'+blob.size}});
  const headers=new Headers(found.headers);headers.set('Content-Range','bytes '+start+'-'+end+'/'+blob.size);headers.set('Content-Length',String(end-start+1));headers.set('Accept-Ranges','bytes');
  return new Response(blob.slice(start,end+1,headers.get('Content-Type')||''),{status:206,headers:headers});
}
self.addEventListener('fetch',event=>{
  const req=event.request,url=new URL(req.url);if(req.method!=='GET'||req.headers.has('Authorization'))return;
  if(url.origin===self.location.origin&&PATHS.indexOf(url.pathname)!==-1){event.respondWith(caches.open(SHELL).then(async c=>(await c.match(url.pathname))||fetch(req)));}
  else if(isCatalog(url)&&!url.searchParams.has('fresh'))event.respondWith((async()=>{
    try{return await fetch(req);}catch(_){const state=await JoneSyncDB.read().catch(()=>null);if(state&&state.activeCache){const cache=await caches.open(state.activeCache),found=await cache.match(req.url);if(found)return found;}return new Response(JSON.stringify({success:false,message:'Gallery unavailable offline'}),{status:503,headers:{'Content-Type':'application/json'}});}
  })());
  else if(isMedia(url))event.respondWith(cachedMedia(req));
});
