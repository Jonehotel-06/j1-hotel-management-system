/* Public-only offline download orchestration; credentials are never cached. */
(function(){
  'use strict';
  var SHELL='jone-tv-shell-v2',PREFIX='jone-tv-download-v2-';
  var shell=['/rooms-gallery/','/rooms-gallery/gallery.css','/rooms-gallery/gallery.js','/rooms-gallery/offline.js','/rooms-gallery/sync-db.js','/rooms-gallery/grid.js','/rooms-gallery/brand.png','/rooms-gallery/icon-192.png','/rooms-gallery/icon-512.png','/rooms-gallery/icon.svg','/rooms-gallery/manifest.webmanifest'];
  function apiBase(){return /^(jonehotels\.com|www\.jonehotels\.com)$/i.test(location.hostname)?'https://api.jonehotels.com':'';}
  function catalogUrl(){return new URL(apiBase()+'/api/rooms-gallery/catalog/',location.href).href;}
  function safeUrl(value){var url=new URL(value,location.href),expected=new URL(catalogUrl());if(url.origin!==expected.origin||!/^\/api\/rooms-gallery\/media\/\d+\/(thumb|display|file)\/$/.test(url.pathname)||url.search||url.hash)throw new Error('Unexpected media URL.');return url.href;}
  async function status(){
    if(!('serviceWorker' in navigator)||!('caches' in window)||!window.indexedDB)return {ready:false,reason:'Offline storage is not supported.'};
    try{
      var record=await JoneSyncDB.read();if(!record)return {ready:false,reason:'Not synchronized.'};
      if(record.phase!=='ready')return {ready:false,reason:'Synchronizing or pending retry'+(record.error?' · '+record.error:'')};
      if(record.outdated)return {ready:false,reason:'Gallery changed; synchronize again.'};
      var sw=await navigator.serviceWorker.getRegistration('/rooms-gallery/');if(!sw||!sw.active)return {ready:false,reason:'Service worker is not active.'};
      var shellCache=await caches.open(SHELL),mediaCache=await caches.open(record.activeCache);
      for(var p of shell)if(!(await shellCache.match(p)))return {ready:false,reason:'Application shell is incomplete.'};
      var cached=await mediaCache.match(record.catalogUrl);if(!cached||(await cached.text())!==record.catalogText)return {ready:false,reason:'Catalog is missing; synchronize again.'};
      for(var u of record.urls)if(!(await mediaCache.match(u)))return {ready:false,reason:'A selected file is missing; synchronize again.'};
      return {ready:true,count:record.urls.length,savedAt:record.savedAt};
    }catch(_){return {ready:false,reason:'Unable to verify offline storage.'};}
  }
  async function noteCatalog(text){try{var state=await JoneSyncDB.read();if(state&&state.phase==='ready')await JoneSyncDB.compare(state.job,function(s){return Object.assign({},s,{outdated:s.outdated||text!==s.catalogText});});}catch(_){} }
  async function freshCatalog(){
    var res=await fetch(catalogUrl()+'?fresh=1',{cache:'no-store',credentials:'omit',redirect:'error'});
    if(!res.ok||res.type==='opaque')throw new Error('Could not load the current gallery catalog.');
    var text=await res.text(),data=JSON.parse(text);if(!data.success||!data.data||!Array.isArray(data.data.rooms))throw new Error('Invalid gallery catalog.');
    await noteCatalog(text);return {text:text,data:data.data};
  }
  function newId(){var bytes=new Uint8Array(16);crypto.getRandomValues(bytes);return Array.from(bytes).map(function(b){return b.toString(16).padStart(2,'0');}).join('');}
  async function send(message,onProgress){
    var registration=await navigator.serviceWorker.ready;if(!registration.active)throw new Error('Service worker is not active.');
    await new Promise(function(resolve,reject){var ping=new MessageChannel(),timer=setTimeout(function(){ping.port1.close();reject(new Error('The gallery worker is updating. Close other gallery tabs and reopen this page.'));},5000);ping.port1.onmessage=function(e){clearTimeout(timer);ping.port1.close();if(e.data&&e.data.version===2)resolve();else reject(new Error('Gallery worker version mismatch.'));};registration.active.postMessage({type:'TV_GALLERY_PING'},[ping.port2]);});
    return new Promise(function(resolve,reject){
      var channel=new MessageChannel();channel.port1.onmessage=function(event){var d=event.data||{};
        if(d.type==='progress'){if(onProgress)onProgress(d.done,d.total,[]);}
        else if(d.type==='done'||d.type==='removed'){channel.port1.close();resolve(d);}
        else if(d.type==='error'){channel.port1.close();reject(new Error(d.message+(d.queued?' Background retry is scheduled.':' Retry manually when online.')));}
      };registration.active.postMessage(message,[channel.port2]);
    });
  }
  async function sync(catalog,selectedRooms,videoIds,progress){
    if(!('serviceWorker' in navigator)||!('caches' in window)||!window.indexedDB)throw new Error('Offline storage is not supported by this browser.');
    var selected=new Set(selectedRooms.map(String)),vids=new Set(videoIds.map(String)),urls=[];
    catalog.data.rooms.forEach(function(room){room.media.forEach(function(item){if(item.thumbnail)urls.push(safeUrl(item.thumbnail));if(item.kind==='image'&&selected.has(String(room.id)))urls.push(safeUrl(item.url));if(item.kind==='video'&&vids.has(String(item.id)))urls.push(safeUrl(item.url));});});
    if(catalog.data.music_url)urls.push(safeUrl(catalog.data.music_url));urls=Array.from(new Set(urls));
    if(urls.length>10000||catalog.text.length>5000000)throw new Error('The selection is too large to synchronize.');
    var previous=await JoneSyncDB.read(),id=newId(),pending=PREFIX+id;
    var job={job:id,phase:'pending',activeCache:previous&&previous.activeCache||null,pendingCache:pending,urls:urls,catalogText:catalog.text,catalogUrl:catalogUrl(),error:''};
    await JoneSyncDB.write(job);
    if(previous&&previous.pendingCache&&previous.pendingCache!==pending)await caches.delete(previous.pendingCache);
    await send({type:'TV_GALLERY_SYNC',job:id},progress);
    var result=await status();if(!result.ready)throw new Error(result.reason);return result;
  }
  async function remove(){await send({type:'TV_GALLERY_REMOVE'});try{localStorage.removeItem('jone.tv.offline.manifest.v1');}catch(_){}}
  if('serviceWorker' in navigator)navigator.serviceWorker.addEventListener('message',function(e){if(e.data&&e.data.type==='TV_GALLERY_OFFLINE_CHANGED')window.dispatchEvent(new Event('tv-gallery-offline-change'));});
  window.JoneOffline={status:status,sync:sync,remove:remove,freshCatalog:freshCatalog,apiBase:apiBase,noteCatalog:noteCatalog};
}());
