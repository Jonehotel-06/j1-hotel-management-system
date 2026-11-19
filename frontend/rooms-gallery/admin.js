(function(){
  'use strict';
  var $=function(id){return document.getElementById(id);};
  var ownKey='jone.tv.admin.token', token='', rooms=[], media=[], settings={}, currentCatalog=null;
  function message(text){$('message').textContent=text;}
  function getToken(){try{var own=sessionStorage.getItem(ownKey);if(own)return own;var existing=JSON.parse(sessionStorage.getItem('jone.session')||'null');return existing&&existing.access||'';}catch(_){return '';}}
  async function api(path,options){
    options=options||{};options.headers=Object.assign({},options.headers||{},token?{'Authorization':'Bearer '+token}:{});
    var res=await fetch(JoneOffline.apiBase()+'/api/rooms-gallery/'+path,options);
    if(res.status===204)return null;
    var data;try{data=await res.json();}catch(_){throw new Error('Unexpected server response ('+res.status+').');}
    if(!res.ok||data.success===false)throw new Error(data.message||('Request failed ('+res.status+').'));
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
    $('upload-limit').textContent='Maximum file size: '+settings.max_upload_mb+' MB. Images have an additional 16 MB processing limit.';
    $('interval').value=settings.slideshow_seconds;$('max-upload').value=settings.max_upload_mb;$('volume').value=settings.music_volume;
    $('music-enabled').checked=settings.music_enabled;$('music-auto').checked=settings.music_autoplay;
    $('music-list').replaceChildren(makeOption('','None'));media.filter(function(m){return m.kind==='audio'&&m.room_id===null;}).forEach(function(m){$('music-list').appendChild(makeOption(String(m.id),m.name));});
    $('music-list').value=settings.music_id?String(settings.music_id):'';
  }
  async function load(){
    var data=await Promise.all([api('manage/rooms/'),api('manage/media/'),api('manage/settings/')]);rooms=data[0];media=data[1];settings=data[2];
    $('login-panel').hidden=true;$('admin').hidden=false;fillRooms();renderSettings();
  }
  function upload(file,roomId,row){
    return new Promise(function(resolve,reject){
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
          var detail=obj.data&&obj.data.file;
          var reason=detail?(Array.isArray(detail)?detail.join(' '):String(detail)):(obj.message||'Upload failed');
          if(xhr.status>=500){uncertain(reason);return;}
          if(xhr.status===413||/exceeds|maximum|limit|size/i.test(reason)){reject(new Error('File exceeds the maximum allowed size ('+settings.max_upload_mb+' MB).'));return;}
          if(/unsupported|format|type/i.test(reason)){reject(new Error('Unsupported file format. Please use JPG, PNG, WebP images or MP4/WebM videos.'));return;}
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
      if(running||completed)return;running=true;progress.value=0;text.textContent=' Uploading…';retry.hidden=true;
      var result;
      try{result=await upload(file,roomId,row);}catch(e){text.textContent=' Failed: '+e.message;retry.hidden=!!e.uncertain;running=false;return;}
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
