/* Single-record, transactional state for gallery-only background downloads. */
(function (root) {
  'use strict';
  var NAME='jone-tv-sync-v2', STORE='state', KEY='current';
  function open(){return new Promise(function(resolve,reject){
    if(!root.indexedDB){reject(new Error('IndexedDB is not available.'));return;}
    var req=root.indexedDB.open(NAME,1);
    req.onupgradeneeded=function(){req.result.createObjectStore(STORE);};
    req.onsuccess=function(){resolve(req.result);};req.onerror=function(){reject(req.error);};
  });}
  async function read(){var db=await open();return new Promise(function(resolve,reject){
    var tx=db.transaction(STORE,'readonly'),req=tx.objectStore(STORE).get(KEY);
    req.onsuccess=function(){resolve(req.result||null);};req.onerror=function(){reject(req.error);};tx.oncomplete=function(){db.close();};tx.onabort=function(){db.close();reject(tx.error);};
  });}
  async function write(value){var db=await open();return new Promise(function(resolve,reject){
    var tx=db.transaction(STORE,'readwrite'),store=tx.objectStore(STORE);if(value===null)store.delete(KEY);else store.put(value,KEY);
    tx.oncomplete=function(){db.close();resolve();};tx.onabort=tx.onerror=function(){db.close();reject(tx.error||new Error('Storage write failed.'));};
  });}
  async function compare(job,fn){var db=await open();return new Promise(function(resolve,reject){
    var tx=db.transaction(STORE,'readwrite'),store=tx.objectStore(STORE),req=store.get(KEY),changed=false;
    req.onsuccess=function(){var state=req.result;if(state&&state.job===job){store.put(fn(state),KEY);changed=true;}};
    tx.oncomplete=function(){db.close();resolve(changed);};tx.onabort=tx.onerror=function(){db.close();reject(tx.error||new Error('Storage write failed.'));};
  });}
  root.JoneSyncDB={read:read,write:write,compare:compare};
}(typeof self!=='undefined'?self:window));
