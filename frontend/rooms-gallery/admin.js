(function(){
  'use strict';
  var $=function(id){return document.getElementById(id);};
  var ownKey='jone.tv.admin.token', token='', rooms=[], media=[], settings={}, currentCatalog=null;
  function message(text){$('message').textContent=text;}
  function getToken(){try{var own=sessionStorage.getItem(ownKey);if(own)return own;var existing=JSON.parse(sessionStorage.getItem('jone.session')||'null');return existing&&existing.access||'';}catch(_){return '';}}
  /* Read the API's own explanation out of an error response.

     The backend envelope for a rejected request is
         { success:false, code:'VALIDATION_ERROR', message:'Validation failed.',
           errors:{ file:['Image could not be decoded safely.'] } }
     and the page previously looked only at `data.file`, which never exists, so
     every rejection — including "storage unavailable" and "image too large" —
     was displayed as the uninformative "Failed: Validation failed.".
     `errors` is authoritative; `data` is still read as a fallback so a cached
     older build of this page keeps working. */
  function firstMessage(node){
    if(node===undefined||node===null)return '';
    if(typeof node==='string')return node.trim();
    if(typeof node==='number'||typeof node==='boolean')return String(node);
    if(Array.isArray(node)){for(var i=0;i<node.length;i++){var m=firstMessage(node[i]);if(m)return m;}return '';}
    if(typeof node==='object'){for(var k in node){if(!Object.prototype.hasOwnProperty.call(node,k))continue;var v=firstMessage(node[k]);if(v)return v;}}
    return '';
  }
  function errorDetail(obj){
    if(!obj||typeof obj!=='object')return {message:'',field:'',code:''};
    var buckets=[obj.errors, obj.data];
    for(var i=0;i<buckets.length;i++){
      var bucket=buckets[i];
      if(!bucket||typeof bucket!=='object')continue;
      var keys=Object.keys(bucket);
      for(var j=0;j<keys.length;j++){
        var text=firstMessage(bucket[keys[j]]);
        if(text)return {message:text,field:keys[j],code:obj.code||''};
      }
    }
    return {message:typeof obj.message==='string'?obj.message:'',field:'',code:obj.code||''};
  }
  async function api(path,options){
    options=options||{};options.headers=Object.assign({},options.headers||{},token?{'Authorization':'Bearer '+token}:{});
    var res=await fetch(JoneOffline.apiBase()+'/api/rooms-gallery/'+path,options);
    if(res.status===204)return null;
    var data;try{data=await res.json();}catch(_){throw new Error('Unexpected server response ('+res.status+').');}
    if(!res.ok||data.success===false){
      var detail=errorDetail(data);
      throw new Error(detail.message||data.message||('Request failed ('+res.status+').'));
    }
    return data.data;
  }
  function json(method,data){return {method:method,headers:{'Content-Type':'application/json'},body:JSON.stringify(data)};}
  function selectedRoom(){return $('room').value;}
  function makeOption(value,label){var x=document.createElement('option');x.value=value;x.textContent=label;return x;}
  function fillRooms(){
    var previous=selectedRoom();$('room').replaceChildren(makeOption('', 'Background music'));
    rooms.forEach(function(r){$('room').appendChild(makeOption(String(r.id),'ROOM '+r.number+' — '+r.type+(r.eligible?'':' (inactive in hotel system)')));});
    if(Array.from($('room').options).some(function(x){return x.value===previous;}))$('room').value=previous;
    selectRoom();
  }
  function selectRoom(){var room=rooms.find(function(r){return String(r.id)===selectedRoom();});$('room-active').disabled=!room;$('save-room').disabled=!room;$('room-active').checked=!!(room&&room.active);renderMedia();}
  function button(text,fn){var b=document.createElement('button');b.textContent=text;b.type='button';b.addEventListener('click',fn);return b;}
  async function preview(item){
    var rendition=item.kind==='image'?'display':'file';
    try{
      var res=await fetch(JoneOffline.apiBase()+'/api/rooms-gallery/manage/media/'+item.id+'/'+rendition+'/',{headers:{'Authorization':'Bearer '+token}});
      if(!res.ok)throw new Error('Preview unavailable.');
      var blob=await res.blob(),url=URL.createObjectURL(blob), area=$('preview');area.replaceChildren();
      if(item.kind==='image'||item.kind==='video'||item.kind==='audio'){
        var el=document.createElement(item.kind==='image'?'img':item.kind);el.src=url;el.style.maxWidth='100%';el.style.maxHeight='300px';if(item.kind!=='image')el.controls=true;area.appendChild(el);
      }else{var a=document.createElement('a');a.href=url;a.download=item.name;a.textContent='Download '+item.name;area.appendChild(a);}
      var close=button('Close preview',function(){URL.revokeObjectURL(url);area.replaceChildren();});area.appendChild(close);
    }catch(e){message(e.message);}
  }
  function renderMedia(){
    var target=$('media-list');target.replaceChildren();
    media.filter(function(m){return selectedRoom()?String(m.room_id)===selectedRoom():m.room_id===null;}).forEach(function(item){
      var row=document.createElement('tr'),name=document.createElement('td'),kind=document.createElement('td'),order=document.createElement('td'),actions=document.createElement('td');
      name.setAttribute('data-label','File');kind.setAttribute('data-label','Type');order.setAttribute('data-label','Order');actions.setAttribute('data-label','Actions');
      name.textContent=item.name;kind.textContent=item.kind+(item.kind==='video'?' · '+item.processing_status+(item.processing_error?' — '+item.processing_error:''):'')+(item.is_cover?' · cover':'')+(item.active?'':' · inactive');
      var input=document.createElement('input');input.type='number';input.min='0';input.max='1000000';input.value=item.order;input.style.width='85px';order.appendChild(input);
      actions.appendChild(button('Save order',async function(){try{await api('manage/media/'+item.id+'/',json('PATCH',{order:Number(input.value)}));await load();message('Order saved.');}catch(e){message(e.message);}}));
      actions.appendChild(button(item.active?'Deactivate':'Activate',async function(){try{await api('manage/media/'+item.id+'/',json('PATCH',{active:!item.active}));await load();}catch(e){message(e.message);}}));
      if(item.kind==='image')actions.appendChild(button('Set cover',async function(){try{await api('manage/media/'+item.id+'/',json('PATCH',{is_cover:true}));await load();}catch(e){message(e.message);}}));
      actions.appendChild(button('Replace',function(){
        var picker=document.createElement('input');picker.type='file';picker.hidden=true;document.body.appendChild(picker);
        picker.onchange=async function(){
          var file=picker.files[0];picker.remove();if(!file)return;
          var row=document.createElement('div'),progress=document.createElement('progress');progress.max=100;row.appendChild(document.createTextNode('Replacing '+item.name+' '));row.appendChild(progress);$('uploads').appendChild(row);
          try{
            var created=await upload(file,item.room_id?String(item.room_id):'',row);
            if(created.kind!==item.kind){await load();message('Replacement has a different media type. Original retained; the new file is listed separately.');return;}
            await api('manage/media/'+created.id+'/',json('PATCH',{order:item.order,active:item.active,is_cover:item.is_cover}));
            if(created.kind==='video' && created.processing_status!=='ready'){await load();message('New video is processing. Original retained; delete it only after checking the replacement.');return;}
            if(settings.music_id===item.id)await api('manage/settings/',json('PATCH',{music_id:created.id}));
            var result=await api('manage/media/'+item.id+'/',{method:'DELETE'});
            await load();message(result&&result.warning?result.warning:'File replaced.');
          }catch(e){await load().catch(function(){});message('Replacement incomplete; review both files: '+e.message);}
        };picker.click();
      }));
      if(item.kind==='video' && item.processing_status!=='ready') actions.appendChild(button('Retry processing',async function(){try{var retried=await api('manage/media/'+item.id+'/retry-processing/',{method:'POST'});await load();message(retried.processing_status==='failed'?'Retry failed: '+(retried.processing_error||'Please try again later.'):retried.processing_status==='ready'?'Video processing complete.':'Video queued for processing.');}catch(e){message(e.message);}}));
      if(item.kind!=='video' || item.processing_status==='ready')actions.appendChild(button('Preview / download',function(){preview(item);}));
      actions.appendChild(button('Delete',async function(){if(!confirm('Delete '+item.name+'?'))return;try{var result=await api('manage/media/'+item.id+'/',{method:'DELETE'});await load();message(result&&result.warning?result.warning:'File deleted.');}catch(e){message(e.message);}}));
      row.append(name,kind,order,actions);target.appendChild(row);
    });
  }
  function renderSettings(){
    var mb=maxUploadMb();
    $('upload-limit').textContent='Maximum file size: '+mb+' MB (JPG, PNG, WebP, GIF images; MP4, MOV, WebM videos; MP3, WAV audio).'
      +(settings.storage_ready===false?' Warning: gallery storage on the server is not configured — uploads will fail until an administrator fixes it ('+(settings.storage_detail||'')+').':'');
    $('interval').value=settings.slideshow_seconds;$('max-upload').value=settings.max_upload_mb;$('volume').value=settings.music_volume;
    $('music-enabled').checked=settings.music_enabled;$('music-auto').checked=settings.music_autoplay;
    $('music-list').replaceChildren(makeOption('','None'));media.filter(function(m){return m.kind==='audio'&&m.room_id===null;}).forEach(function(m){$('music-list').appendChild(makeOption(String(m.id),m.name));});
    $('music-list').value=settings.music_id?String(settings.music_id):'';
  }
  async function load(){
    var data=await Promise.all([api('manage/rooms/'),api('manage/media/'),api('manage/settings/')]);rooms=data[0];media=data[1];settings=data[2];
    $('login-panel').hidden=true;$('admin').hidden=false;fillRooms();renderSettings();
  }
  /* Formats the API accepts (server-side validation is authoritative; this is
     only so the page can refuse an obviously-wrong file BEFORE spending minutes
     uploading it — the two layers must not disagree). */
  var IMAGE_EXTENSIONS=['jpg','jpeg','png','webp','gif'];
  var VIDEO_EXTENSIONS=['mp4','m4v','mov','webm'];
  var AUDIO_EXTENSIONS=['mp3','wav','ogg'];
  function extensionOf(name){var parts=String(name||'').toLowerCase().split('.');return parts.length>1?parts.pop():'';}
  function maxUploadMb(){
    var value=Number(settings.effective_max_upload_mb||settings.max_upload_mb);
    return isFinite(value)&&value>0?value:128;
  }
  /* Pre-flight: an empty file, an obviously unsupported extension or a file over
     the limit is reported immediately with the real reason. */
  function preflight(file){
    if(!file||!file.size)return 'The selected file is empty (0 bytes).';
    var ext=extensionOf(file.name),known=IMAGE_EXTENSIONS.indexOf(ext)>=0||VIDEO_EXTENSIONS.indexOf(ext)>=0||AUDIO_EXTENSIONS.indexOf(ext)>=0;
    if(!known)return 'Unsupported file format ("'+(ext?'.'+ext:'no extension')+'"). Use JPG, PNG, WebP or GIF images, MP4/MOV/WebM videos, or MP3/WAV audio.';
    var limit=maxUploadMb()*1024*1024;
    if(file.size>limit)return 'This file is '+Math.round(file.size/1048576)+' MB; the maximum allowed size is '+maxUploadMb()+' MB.';
    return '';
  }
  function upload(file,roomId,row){
    return new Promise(function(resolve,reject){
      var blocked=preflight(file);
      if(blocked){reject(new Error(blocked));return;}
      var xhr=new XMLHttpRequest(),form=new FormData();form.append('file',file);if(roomId)form.append('room_id',roomId);
      xhr.open('POST',JoneOffline.apiBase()+'/api/rooms-gallery/manage/media/');xhr.setRequestHeader('Authorization','Bearer '+token);
      xhr.upload.onprogress=function(e){if(e.lengthComputable)row.querySelector('progress').value=Math.round(e.loaded/e.total*100);};
      function uncertain(message){var error=new Error(message+' The server may have saved this file. Refresh the media list and check before uploading again.');error.uncertain=true;reject(error);}
      xhr.onerror=function(){uncertain('Network connection interrupted. Please check your connection and retry.');};
      xhr.onabort=function(){uncertain('Upload interrupted. The upload was cancelled before completion.');};
      xhr.ontimeout=function(){uncertain('Upload timed out. The file may be too large or the connection is slow. Try a smaller file or retry.');};
      xhr.timeout=300000;
      xhr.onload=function(){
        var obj;try{obj=JSON.parse(xhr.responseText);}catch(_){uncertain('Unexpected server response ('+xhr.status+').');return;}
        if(xhr.status<200||xhr.status>=300||obj.success===false){
          var detail=errorDetail(obj);
          var reason=detail.message||obj.message||('Upload failed ('+xhr.status+').');
          /* Storage/configuration faults are the SERVER's problem and nothing was
             stored, so the same file can safely be retried once an administrator
             fixes it — this must not read as "your file is invalid". */
          if(obj.code==='GALLERY_STORAGE_UNAVAILABLE'){
            var storageError=new Error(reason);
            storageError.retryable=true;
            reject(storageError);return;
          }
          if(xhr.status===413){reject(new Error('The server refused the upload as too large (HTTP 413). The maximum configured size is '+maxUploadMb()+' MB; if the file is smaller, the hosting proxy has a lower limit — ask an administrator.'));return;}
          if(xhr.status>=500){uncertain(reason);return;}
          if(/exceeds|maximum|limit|size/i.test(reason)){reject(new Error(reason));return;}
          if(/unsupported|format|type/i.test(reason)){reject(new Error(reason));return;}
          reject(new Error(reason));return;
        }
        if(!obj.data||!obj.data.id){uncertain('The server returned an incomplete upload response.');return;}
        resolve(obj.data);
      };xhr.send(form);
    });
  }
  function uploadRow(file,roomId){
    var row=document.createElement('div');row.className='upload-row';var label=document.createElement('span');label.textContent=file.name+' ';var progress=document.createElement('progress');progress.max=100;progress.value=0;var text=document.createElement('span');row.append(label,progress,text);$('uploads').appendChild(row);
    var running=false, completed=false;
    async function run(){
      if(running||completed)return;running=true;progress.value=0;retry.hidden=true;
      var blocked=preflight(file);
      if(blocked){text.textContent=' Failed: '+blocked;retry.hidden=true;running=false;return;}
      text.textContent=' Uploading…';
      var result;
      try{result=await upload(file,roomId,row);}catch(e){
        text.textContent=' Failed: '+e.message;retry.hidden=!!e.uncertain;running=false;
        /* Uncertain = the server may already hold this file. Refresh the
           media list so the operator can SEE whether it landed before
           deciding anything — and the Retry button stays hidden until
           they have had that chance, so a blind re-upload cannot create
           a duplicate (the server also dedupes identical bytes). */
        if(e.uncertain){try{await load();text.textContent+=' The media list was refreshed — check whether this file is already listed.';}catch(_){}}
        return;
      }
      completed=true;running=false;progress.value=100;text.textContent=' Uploaded'+(result.notice?' — '+result.notice:'');
      try{await load();}catch(e){text.textContent+='; list refresh failed. Use Refresh processing status: '+e.message;}
    }
    var retry=button('Retry',run);retry.hidden=true;row.appendChild(retry);return run;
  }
  $('upload').onclick=async function(){var files=Array.from($('files').files),roomId=selectedRoom();if(!files.length){message('Select files first.');return;}this.disabled=true;try{for(var f of files)await uploadRow(f,roomId)();$('files').value='';}finally{this.disabled=false;}};
  $('save-room').onclick=async function(){try{await api('manage/rooms/',json('PATCH',{room_id:Number(selectedRoom()),active:$('room-active').checked}));await load();message('Room presentation state saved.');}catch(e){message(e.message);}};
  $('save-settings').onclick=async function(){try{await api('manage/settings/',json('PATCH',{slideshow_seconds:Number($('interval').value),max_upload_mb:Number($('max-upload').value),music_volume:Number($('volume').value),music_id:$('music-list').value?Number($('music-list').value):null,music_enabled:$('music-enabled').checked,music_autoplay:$('music-auto').checked}));await load();message('Settings saved.');}catch(e){message(e.message);}};
  $('room').onchange=selectRoom;
  $('refresh-media').onclick=function(){load().then(function(){message('Status refreshed.');}).catch(function(e){message(e.message);});};
  $('login').onsubmit=async function(e){e.preventDefault();$('login-message').textContent='Signing in…';try{var res=await fetch(JoneOffline.apiBase()+'/api/auth/login/',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:$('email').value,password:$('password').value})});var obj=await res.json();if(!res.ok||!obj.data||!obj.data.tokens)throw new Error(obj.message||'Sign-in failed.');token=obj.data.tokens.access;sessionStorage.setItem(ownKey,token);$('password').value='';await load();$('login-message').textContent='';}catch(err){token='';sessionStorage.removeItem(ownKey);$('login-message').textContent=err.message;}};
  $('signout').onclick=function(){sessionStorage.removeItem(ownKey);token='';$('admin').hidden=true;$('login-panel').hidden=false;$('login-message').textContent='Gallery session closed. Other hotel sessions are unchanged.';};
  async function offlineStatus(){var status=await JoneOffline.status();$('offline-status').textContent=status.ready?'Ready for offline: '+status.count+' selected files saved on this device.':status.reason;if(navigator.storage&&navigator.storage.estimate){var q=await navigator.storage.estimate();$('quota').textContent='Storage used: '+Math.round((q.usage||0)/1048576)+' MB; estimated quota: '+Math.round((q.quota||0)/1048576)+' MB.';}}
  $('refresh-offline').onclick=async function(){try{currentCatalog=await JoneOffline.freshCatalog();var box=$('offline-choices');box.replaceChildren();currentCatalog.data.rooms.forEach(function(room){var group=document.createElement('fieldset'),legend=document.createElement('legend');legend.textContent='ROOM '+room.number;group.appendChild(legend);var label=document.createElement('label'),check=document.createElement('input');check.type='checkbox';check.className='offline-room';check.value=room.id;check.checked=true;label.append(check,document.createTextNode(' Room images'));group.appendChild(label);room.media.filter(function(m){return m.kind==='video';}).forEach(function(m,i){var l=document.createElement('label'),v=document.createElement('input');v.type='checkbox';v.className='offline-video';v.value=m.id;l.append(v,document.createTextNode(' Video '+(i+1)));group.appendChild(l);});box.appendChild(group);});await offlineStatus();}catch(e){$('offline-status').textContent=e.message;}};
  $('sync').onclick=async function(){if(!currentCatalog){$('offline-status').textContent='Refresh selections first.';return;}this.disabled=true;try{try{if(navigator.storage&&navigator.storage.persist)navigator.storage.persist().catch(function(){});}catch(_){}var rooms=Array.from(document.querySelectorAll('.offline-room:checked')).map(function(x){return x.value;}),videos=Array.from(document.querySelectorAll('.offline-video:checked')).map(function(x){return x.value;});await JoneOffline.sync(currentCatalog,rooms,videos,function(done,total,failures){$('offline-status').textContent='Synchronizing '+done+' / '+total+' files; failures: '+failures.length;});await offlineStatus();}catch(e){$('offline-status').textContent='Not ready: '+e.message;}finally{this.disabled=false;}};
  $('remove-offline').onclick=async function(){if(!confirm('Remove downloaded gallery media from this device?'))return;await JoneOffline.remove();await offlineStatus();};
  window.addEventListener('tv-gallery-offline-change',offlineStatus);
  if('serviceWorker' in navigator)navigator.serviceWorker.register('/rooms-gallery/sw.js',{scope:'/rooms-gallery/'}).then(offlineStatus).catch(offlineStatus);else offlineStatus();
  token=getToken();if(token)load().catch(function(e){$('login-message').textContent=e.message;});
}());
