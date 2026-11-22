(function(){
  'use strict';
  var $=function(id){return document.getElementById(id);};
  var ownKey='jone.tv.admin.token', token='', rooms=[], media=[], settings={}, currentCatalog=null;
  /* Section status line. `kind` drives the colour and, for 'working', a real
     spinner. Nothing below ever sets 'working' without an open request: the
     busy state is created when the XHR/fetch starts and cleared in `finally`,
     so the indicator cannot outlive the operation it describes. */
  function message(text,kind){
    var box=$('message');if(!box)return;
    box.replaceChildren();
    box.setAttribute('data-kind',kind||'');
    box.setAttribute('role',kind==='error'?'alert':'status');
    if(!text)return;
    if(kind==='working'){var spin=document.createElement('span');spin.className='spinner';spin.setAttribute('aria-hidden','true');box.appendChild(spin);}
    box.appendChild(document.createTextNode(text));
  }
  function fail(text){message(text,'error');}
  function ok(text){message(text,'ok');}

  /* ---- Busy states -------------------------------------------------------
     `busy(button, true)` disables the control, marks it aria-busy (CSS draws
     the spinner and appends it to the label) and remembers the original label
     so a refused or failed request always restores a usable button. A second
     click on a control that is already busy is ignored, which is what stops a
     double tap on a phone from sending the same upload/patch twice. */
  function busy(node,isBusy,label){
    if(!node)return;
    if(isBusy){
      if(node.getAttribute('aria-busy')==='true')return;
      node.setAttribute('data-idle-label',node.textContent);
      if(label)node.textContent=label;
      node.setAttribute('aria-busy','true');
      node.disabled=true;
      return;
    }
    if(node.getAttribute('aria-busy')!=='true')return;
    node.removeAttribute('aria-busy');
    if(node.hasAttribute('data-idle-label')){node.textContent=node.getAttribute('data-idle-label');node.removeAttribute('data-idle-label');}
    node.disabled=false;
  }
  function isBusy(node){return !!(node&&node.getAttribute('aria-busy')==='true');}

  /* Wrap one user action so every button on the page behaves the same way:
     disabled + spinner while it runs, "Working…" in the status line, success or
     the real error afterwards, and the button always restored. */
  async function runAction(node,working,succeeded,action){
    if(isBusy(node))return null;
    busy(node,true);
    if(working)message(working,'working');
    try{
      var result=await action();
      if(succeeded)message(typeof succeeded==='function'?succeeded(result):succeeded,'ok');
      return result;
    }catch(err){
      message((err&&err.message)||'The action failed. Please try again.','error');
      return null;
    }finally{
      busy(node,false);
    }
  }

  function skeletons(host,count){
    if(!host)return;
    host.replaceChildren();
    for(var i=0;i<(count||3);i++){var row=document.createElement('div');row.className='skeleton-row';host.appendChild(row);}
  }

  /* ---- Media renditions -------------------------------------------------
     Thumbnails are fetched through the authenticated admin rendition route
     (/manage/media/<id>/thumb/) as a Blob and shown from an object URL. The
     download route requires the JWT in an Authorization header, so a plain
     <img src> would answer 401 and every card would look broken. When a
     thumbnail cannot be produced, the card says so instead of rendering a
     broken image. */
  var blobUrls={};
  function releaseBlobs(prefix){
    Object.keys(blobUrls).forEach(function(id){
      if(!prefix||String(id).indexOf(prefix)===0){try{URL.revokeObjectURL(blobUrls[id]);}catch(_){}delete blobUrls[id];}
    });
  }
  async function renditionBlob(item,rendition){
    var key='blob:'+item.id+':'+rendition;
    if(blobUrls[key])return blobUrls[key];
    var res=await fetch(JoneOffline.apiBase()+'/api/rooms-gallery/manage/media/'+item.id+'/'+rendition+'/',headers());
    if(!res.ok)throw new Error('Rendition unavailable ('+res.status+').');
    var url=URL.createObjectURL(await res.blob());
    blobUrls[key]=url;
    return url;
  }
  function headers(extra){
    return Object.assign({},extra||{},token?{'Authorization':'Bearer '+token}:{});
  }
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
    options=options||{};options.headers=headers(options.headers);
    /* Offline administration: never pretend a mutating request reached the
       server while the device has no connection. This is shown immediately
       instead of after a 30 s timeout, nothing the operator selected is
       discarded, and the same button works once the connection returns. */
    if(options.method&&options.method!=='GET'&&!navigator.onLine){
      throw new Error('You are offline. This change cannot be sent to the server yet; your selection is preserved. Reconnect and try again.');
    }
    /* A mobile connection can stall a request indefinitely; without a cap the
       caller waits forever and the page looks frozen. 30 s is far beyond any
       healthy response and short enough to tell the manager something broke. */
    var controller=typeof AbortController==='function'?new AbortController():null;
    var timer=controller?setTimeout(function(){controller.abort();},30000):null;
    var res;
    try{
      res=await fetch(JoneOffline.apiBase()+'/api/rooms-gallery/'+path,controller?Object.assign({},options,{signal:controller.signal}):options);
    }catch(err){
      throw new Error(err&&err.name==='AbortError'?'The server did not answer within 30 seconds. Check the connection and try again.':(err&&err.message)||'Network error.');
    }finally{if(timer)clearTimeout(timer);}
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
  /* Fetch the first stored object that exists for this media item.
       image -> the re-encoded 'display' rendition, falling back to the
                original file when processing has not produced one;
       video -> the 'file' rendition (the playable H.264 the TV uses),
                falling back to the original upload while it is still
                processing.
     Each candidate is a real request and the LAST failure is what gets
     reported, so the operator is never told "could not be produced" without
     the HTTP status that explains why. */
  function fetchRendition(item,candidates,index){
    var rendition=candidates[index];
    return fetch(JoneOffline.apiBase()+'/api/rooms-gallery/manage/media/'+item.id+'/'+rendition+'/',{headers:headers()})
      .then(function(res){
        if(!res.ok){
          if(index+1<candidates.length)return fetchRendition(item,candidates,index+1);
          throw new Error('The preview could not be produced ('+res.status+'). '+
            (item.kind==='video'?'The video may still be processing.':'Ask an administrator to check the gallery storage.'));
        }
        return res.blob();
      },function(err){
        if(index+1<candidates.length)return fetchRendition(item,candidates,index+1);
        throw err;
      });
  }
  function preview(item){
    var candidates=item.kind==='image'?['display','file']:['file'];
    var area=$('preview');if(!area)return;
    area.replaceChildren();
    var heading=document.createElement('p');heading.className='caption';
    heading.textContent='Loading '+item.name+'…';
    area.appendChild(heading);
    message('Loading “'+item.name+'”…','working');
    return fetchRendition(item,candidates,0)
      .then(function(blob){
        var url=URL.createObjectURL(blob);
        area.replaceChildren();
        if(item.kind==='image'||item.kind==='video'||item.kind==='audio'){
          var el=document.createElement(item.kind==='image'?'img':item.kind);
          el.src=url;if(item.kind!=='image')el.controls=true;
          el.setAttribute('aria-label',item.name);
          area.appendChild(el);
        }else{
          var a=document.createElement('a');a.href=url;a.download=item.name;a.textContent='Download '+item.name;area.appendChild(a);
        }
        var caption=document.createElement('p');caption.className='caption';caption.textContent=item.name;
        var close=button('Close preview',function(){try{URL.revokeObjectURL(url);}catch(_){}area.replaceChildren();});
        area.appendChild(caption);area.appendChild(close);
        message('Showing “'+item.name+'”.','ok');
      })
      .catch(function(e){area.replaceChildren();fail((e&&e.message)||'The preview could not be produced.');});
  }
  /* ---- Media gallery -----------------------------------------------------
     One card per uploaded file: a real thumbnail (or a labelled fallback), the
     file name/type/order, and the actions that apply to it. The previous
     renderer emitted table ROWS with a file NAME and a "Set cover" button, so
     an administrator could not see which picture they were choosing as the
     cover — exactly the report "there's no way I can see the images I uploaded
     in order to know the one I can use as cover image".

     Cover selection is a server write (PATCH is_cover=true) and the card is
     only marked as the cover after the server confirms; then the collections
     are re-read, so a refresh shows the same cover. It is never a local-only
     flag. */
  function orderBadge(item){
    var span=document.createElement('span');span.className='badge-state';
    span.textContent=item.kind==='video'?'Video order '+item.order:(item.kind==='audio'?'Audio':'Position '+item.order);
    return span;
  }
  function stateBadge(text,tone){
    var span=document.createElement('span');span.className='badge-state';
    if(tone)span.setAttribute('data-tone',tone);
    span.textContent=text;return span;
  }
  async function fillThumb(card,item){
    /* The rendition is drawn inside .media-frame; the kind badge and the card
       chrome live outside it, so a failed rendition replaces ONLY the picture
       and the file stays identifiable ("VIDEO", "IMAGE", …). */
    var thumb=card.querySelector('.media-frame')||card.querySelector('.media-thumb');
    if(item.kind==='video'&&item.processing_status==='ready'){
      /* A video's poster frame IS its processed display rendition (H.264), so
         the same object can be used as a muted, non-autoplaying preview. The
         control stays a real <video> so "identifiable as video" survives even
         if the poster fails to decode. */
      var video=document.createElement('video');
      video.muted=true;video.playsInline=true;video.preload='metadata';video.controls=false;
      video.setAttribute('aria-label','Preview of '+item.name);
      thumb.appendChild(video);
      try{video.src=await renditionBlob(item,'file');}catch(_){thumbFallback(thumb,item);}
      return;
    }
    if(item.kind==='image'||(item.kind==='video'&&item.processing_status==='ready')){
      var img=document.createElement('img');
      img.alt='Thumbnail of '+item.name;img.loading='lazy';
      thumb.appendChild(img);
      try{img.src=await renditionBlob(item,'thumb');}
      catch(_){thumbFallback(thumb,item);}
      return;
    }
    thumbFallback(thumb,item);
  }
  function thumbFallback(thumb,item){
    if(!thumb)return;
    var box=document.createElement('div');box.className='media-fallback';
    box.textContent=item.kind==='image'?'Preview unavailable — open the file'
      :item.kind==='video'?(item.processing_status==='failed'?'Video processing failed':'Video still processing — no preview yet')
      :item.kind==='audio'?'Audio file':'No inline preview for this file';
    thumb.replaceChildren(box);
  }
  function renderMedia(){
    var target=$('media-list');if(!target)return;
    releaseBlobs();
    var empty=$('media-empty'),count=$('media-count');
    var rows=media.filter(function(m){return selectedRoom()?String(m.room_id)===selectedRoom():m.room_id===null;});
    /* Stable, meaningful order: the cover first, then the presentation order. */
    rows.sort(function(a,b){
      if(!!b.is_cover!==!!a.is_cover)return (b.is_cover?1:0)-(a.is_cover?1:0);
      if(a.order!==b.order)return a.order-b.order;
      return a.id-b.id;
    });
    target.replaceChildren();
    if(count)count.textContent=rows.length?(rows.length+' file'+(rows.length===1?'':'s')+' in this room'):'';
    if(empty)empty.textContent=rows.length?'':'No files in this room yet. Choose files above and press “Upload selected files”.';
    rows.forEach(function(item){
      var card=document.createElement('article');
      card.className='media-card';
      card.setAttribute('data-cover',item.is_cover?'true':'false');
      card.setAttribute('data-inactive',item.active?'false':'true');
      card.setAttribute('data-media-id',String(item.id));

      var thumb=document.createElement('button');
      thumb.type='button';thumb.className='media-thumb';
      thumb.setAttribute('aria-label','Preview '+item.name);
      var frame=document.createElement('div');frame.className='media-frame';
      thumb.appendChild(frame);
      var kindTag=document.createElement('span');kindTag.className='media-kind';kindTag.textContent=item.kind;
      thumb.appendChild(kindTag);
      thumb.addEventListener('click',function(){preview(item);});
      card.appendChild(thumb);
      fillThumb(card,item);

      var badges=document.createElement('div');badges.className='media-badges';
      if(item.is_cover){var c=document.createElement('span');c.className='badge-cover';c.textContent='COVER';badges.appendChild(c);}
      badges.appendChild(orderBadge(item));
      if(!item.active)badges.appendChild(stateBadge('Hidden from TV','warn'));
      if(item.kind==='video'){
        badges.appendChild(stateBadge(item.processing_status+(item.processing_error?' — '+item.processing_error:''),
          item.processing_status==='failed'?'bad':(item.processing_status==='ready'?'':'warn')));
      }
      card.appendChild(badges);

      var name=document.createElement('div');name.className='media-name';name.textContent=item.name;
      var meta=document.createElement('div');meta.className='media-meta';
      meta.textContent=Math.round((item.size||0)/1024)+' KB · '+(item.is_cover?'shown first for this room':'position '+item.order);
      card.appendChild(name);card.appendChild(meta);

      var actions=document.createElement('div');actions.className='media-actions';

      /* --- cover ------------------------------------------------------ */
      if(item.kind==='image'&&item.room_id!==null){
        if(item.is_cover){
          var current=button('Cover image ✓',function(){ok('This picture is already the cover for this room. It is what guests see first.');});
          current.disabled=true;current.classList.add('primary');
          actions.appendChild(current);
        }else{
          var setCover=button('Set as cover',function(){
            return runAction(setCover,'Saving the cover image…','Cover image saved. Guests will see “'+item.name+'” first for this room.',async function(){
              await api('manage/media/'+item.id+'/',json('PATCH',{is_cover:true}));
              await load();
              var confirmed=media.filter(function(m){return String(m.room_id)===String(item.room_id)&&m.is_cover;})[0];
              if(!confirmed||String(confirmed.id)!==String(item.id)){
                throw new Error('The server did not record this picture as the cover. Nothing was hidden — press “Set as cover” again, and report it if it repeats.');
              }
            });
          });
          setCover.classList.add('primary');
          actions.appendChild(setCover);
        }
      }

      /* --- presentation order / visibility ---------------------------- */
      if(item.room_id!==null){
        var move=function(delta,node){
          return runAction(node,'Saving the order…','Order saved.',async function(){
            var next=Math.max(0,Number(item.order)+delta);
            await api('manage/media/'+item.id+'/',json('PATCH',{order:next}));
            await load();
          });
        };
        var earlier=button('Move earlier',function(){return move(-1,earlier);});earlier.disabled=item.order<=0;
        var later=button('Move later',function(){return move(1,later);});
        actions.appendChild(earlier);actions.appendChild(later);
        var toggle=button(item.active?'Hide from TV':'Show on TV',function(){
          return runAction(toggle,item.active?'Hiding this file…':'Showing this file…',item.active?'File hidden from the TV presentation.':'File shown on the TV presentation.',async function(){
            await api('manage/media/'+item.id+'/',json('PATCH',{active:!item.active}));
            await load();
          });
        });
        actions.appendChild(toggle);
      }else{
        var orderInput=document.createElement('input');
        orderInput.type='number';orderInput.min='0';orderInput.max='1000000';orderInput.value=item.order;
        orderInput.setAttribute('aria-label','Playback order for '+item.name);
        orderInput.style.width='90px';
        var saveOrder=button('Save order',function(){
          return runAction(saveOrder,'Saving the order…','Order saved.',async function(){
            await api('manage/media/'+item.id+'/',json('PATCH',{order:Number(orderInput.value)}));
            await load();
          });
        });
        actions.appendChild(orderInput);actions.appendChild(saveOrder);
      }

      /* --- video processing ------------------------------------------ */
      if(item.kind==='video'&&item.processing_status!=='ready'){
        var retryProc=button('Retry processing',function(){
          return runAction(retryProc,'Asking the server to process this video again…',function(retried){
            return retried.processing_status==='ready'?'Video processing complete.'
              :retried.processing_status==='failed'?'Retry failed: '+(retried.processing_error||'please try again later.'):'Video queued for processing.';
          },async function(){
            var retried=await api('manage/media/'+item.id+'/retry-processing/',{method:'POST'});
            await load();
            return retried;
          });
        });
        actions.appendChild(retryProc);
      }

      /* --- preview / replace / delete -------------------------------- */
      actions.appendChild(button('Open file',function(){preview(item);}));
      actions.appendChild(button('Replace',function(){
        var picker=document.createElement('input');picker.type='file';picker.hidden=true;document.body.appendChild(picker);
        picker.onchange=async function(){
          var file=picker.files[0];picker.remove();if(!file)return;
          var row=document.createElement('div');row.className='upload-row';
          var progress=document.createElement('progress');progress.max=100;
          var note=document.createElement('span');note.textContent=file.name;
          row.append(note,progress);$('uploads').appendChild(row);
          message('Replacing “'+item.name+'”…','working');
          try{
            var created=await upload(file,item.room_id?String(item.room_id):'',row);
            if(created.kind!==item.kind){await load();message('The replacement has a different media type, so the original was kept and the new file is listed separately.','warn');return;}
            await api('manage/media/'+created.id+'/',json('PATCH',{order:item.order,active:item.active,is_cover:item.is_cover}));
            if(created.kind==='video'&&created.processing_status!=='ready'){await load();message('The new video is still processing. The original was kept — delete it only after checking the replacement.','warn');return;}
            if(settings.music_id===item.id)await api('manage/settings/',json('PATCH',{music_id:created.id}));
            var result=await api('manage/media/'+item.id+'/',{method:'DELETE'});
            await load();message(result&&result.warning?result.warning:'File replaced.','ok');
          }catch(e){
            await load().catch(function(){});
            message('Replacement incomplete; review both files: '+((e&&e.message)||e),'error');
          }
        };
        picker.click();
      }));
      var del=button('Delete',function(){
        if(!confirm('Delete '+item.name+'? This removes it from the gallery on the server.'))return;
        return runAction(del,'Deleting “'+item.name+'”…',function(result){
          return (result&&result.warning)?result.warning:'File deleted.';
        },async function(){return api('manage/media/'+item.id+'/',{method:'DELETE'});}).then(function(result){
          if(result!==null)return load();
        });
      });
      del.style.background='#8a1c10';
      actions.appendChild(del);

      card.appendChild(actions);
      target.appendChild(card);
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
    /* The three collections are independent, so they are loaded independently.
       Promise.all() made the whole page hostage to its slowest member: on a
       mobile connection one dropped request left the manager staring at the
       sign-in form with no panel, no spinner and no reason, which reads as a
       frozen page. The panel is now opened as soon as the session is accepted,
       whatever the outcome, and every failure is named in the status line. */
    $('login-panel').hidden=true;$('admin').hidden=false;
    /* A visible, honest "working" state while the three reads are open. It is
       cleared by whichever branch below reports the outcome, so it can never
       stay on screen after the requests have finished. */
    message('Loading rooms, media and settings…','working');
    if(!media.length)skeletons($('media-list'),3);
    var results=await Promise.allSettled([api('manage/rooms/'),api('manage/media/'),api('manage/settings/')]);
    var failures=[];
    if(results[0].status==='fulfilled')rooms=results[0].value;else failures.push('rooms: '+reason(results[0]));
    if(results[1].status==='fulfilled')media=results[1].value;else failures.push('media: '+reason(results[1]));
    if(results[2].status==='fulfilled')settings=results[2].value;else failures.push('settings: '+reason(results[2]));
    /* Report BEFORE rendering: a missing collection can make a renderer throw,
       and an exception here must not swallow the explanation of why. */
    var renderErrors=[];
    try{fillRooms();}catch(err){renderErrors.push('rooms could not be shown: '+((err&&err.message)||err));}
    try{renderSettings();}catch(err){renderErrors.push('settings could not be shown: '+((err&&err.message)||err));}
    /* Report before rendering threw anywhere: a renderer exception must not
       swallow the explanation of why a collection is missing. */
    if(failures.length||renderErrors.length){
      message('Some sections did not load — '+failures.concat(renderErrors).join('; ')+'. Press "Refresh status" to retry.','error');
    }else if($('message').getAttribute('data-kind')==='working'){
      message('','');
    }
    return failures.concat(renderErrors);
  }
  function reason(result){var e=result&&result.reason;return (e&&e.message)||'failed';}
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
    var row=document.createElement('div');row.className='upload-row';row.setAttribute('data-state','queued');
    var label=document.createElement('span');label.textContent=file.name;
    var progress=document.createElement('progress');progress.max=100;progress.value=0;
    progress.setAttribute('aria-label','Upload progress for '+file.name);
    var text=document.createElement('span');row.append(label,progress,text);$('uploads').appendChild(row);
    var running=false, completed=false;
    async function run(){
      if(running||completed)return;running=true;progress.value=0;row.setAttribute('data-state','working');retry.hidden=true;
      if(!navigator.onLine){text.textContent='Cannot upload — this device is offline. Reconnect, then press Retry.';row.setAttribute('data-state','failed');retry.hidden=false;running=false;return;}
      var blocked=preflight(file);
      if(blocked){text.textContent='Failed: '+blocked;row.setAttribute('data-state','failed');retry.hidden=true;running=false;return;}
      text.textContent='Uploading…';
      message('Uploading “'+file.name+'”…','working');
      var result;
      try{result=await upload(file,roomId,row);}catch(e){
        text.textContent='Failed: '+e.message;row.setAttribute('data-state','failed');retry.hidden=!!e.uncertain;running=false;
        /* The row already carries the server's own explanation next to the
           file name; the section banner summarises the outcome instead of
           repeating the same sentence a second time (which also kept the
           message ambiguous to anything matching on visible text). */
        fail(file.name+' could not be added — see the reason beside the file.');
        /* Uncertain = the server may already hold this file. Refresh the
           media list so the operator can SEE whether it landed before
           deciding anything — and the Retry button stays hidden until
           they have had that chance, so a blind re-upload cannot create
           a duplicate (the server also dedupes identical bytes). */
        if(e.uncertain){try{await load();text.textContent+=' The media list was refreshed — check whether this file is already listed.';}catch(_){}}
        return;
      }
      completed=true;running=false;progress.value=100;row.setAttribute('data-state','done');
      text.textContent='Uploaded'+(result.notice?' — '+result.notice:'');
      var refreshFailures=[];
      try{refreshFailures=await load()||[];}catch(e){refreshFailures=[(e&&e.message)||'refresh failed'];}
      /* The FILE was stored (the server answered 201) — that is what this row
         reports. But if the list could not be re-read, saying "uploaded" alone
         would be misleading: the operator would look for a file that is not on
         screen. Say both, with the phrase the operator can act on. */
      if(refreshFailures.length){
        text.textContent+='; list refresh failed. Press "Refresh status" to retry.';
      }else{
        ok(file.name+' was saved'+(result.notice?' — '+result.notice:'')+'. It is listed in this room’s media below.');
      }
    }
    var retry=button('Retry',run);retry.hidden=true;row.appendChild(retry);return run;
  }
  $('upload').onclick=function(){
    var node=this;
    if(isBusy(node))return;
    var files=Array.from($('files').files),roomId=selectedRoom();
    if(!files.length){fail('Choose one or more files first, then press “Upload selected files”.');$('files').focus();return;}
    busy(node,true);
    var chain=Promise.resolve();
    files.forEach(function(f){chain=chain.then(function(){return uploadRow(f,roomId)();});});
    return chain.then(function(){
      $('files').value='';
      var failed=Array.from($('uploads').querySelectorAll('[data-state=failed]')).length;
      if(!failed&&$('message').getAttribute('data-kind')!=='error'){
        ok(files.length+(files.length===1?' file':' files')+' saved.');
      }
    }).finally(function(){busy(node,false);});
  };
  $('save-room').onclick=function(){
    var node=this;
    return runAction(node,'Saving this room’s presentation state…','Room presentation state saved. Guests will '+( $('room-active').checked?'see':'no longer see')+' this room.',async function(){
      await api('manage/rooms/',json('PATCH',{room_id:Number(selectedRoom()),active:$('room-active').checked}));
      await load();
    });
  };
  $('save-settings').onclick=function(){
    var node=this;
    return runAction(node,'Saving presentation settings…','Settings saved.',async function(){
      await api('manage/settings/',json('PATCH',{slideshow_seconds:Number($('interval').value),max_upload_mb:Number($('max-upload').value),music_volume:Number($('volume').value),music_id:$('music-list').value?Number($('music-list').value):null,music_enabled:$('music-enabled').checked,music_autoplay:$('music-auto').checked}));
      await load();
    });
  };
  $('room').onchange=selectRoom;
  $('refresh-media').onclick=function(){
    var node=this;
    return runAction(node,'Refreshing rooms, media and processing status…','Status refreshed.',async function(){await load();});
  };
  /* Multi-file selection is the default; unticking the box makes the picker
     single-file (phones with a small screen), and the choice is remembered for
     this session so a manager does not have to re-tick it for every upload. */
  (function(){
    var multi=$('upload-multi'),picker=$('files');
    if(!multi||!picker)return;
    var stored='';
    try{stored=sessionStorage.getItem('jone.tv.admin.multi')||'';}catch(_){}
    if(stored==='0')multi.checked=false;
    function apply(){picker.multiple=multi.checked;
      try{sessionStorage.setItem('jone.tv.admin.multi',multi.checked?'1':'0');}catch(_){}}
    multi.onchange=apply;apply();
  })();
  $('login').onsubmit=async function(e){e.preventDefault();
      var submit=this.querySelector('[type=submit]');busy(submit,true,'Signing in…');
      $('login-message').textContent='Signing in…';try{
      var lc=typeof AbortController==='function'?new AbortController():null,lt=lc?setTimeout(function(){lc.abort();},30000):null,lres;
      try{lres=await fetch(JoneOffline.apiBase()+'/api/auth/login/',lc?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:$('email').value,password:$('password').value}),signal:lc.signal}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:$('email').value,password:$('password').value})});}
      catch(le){throw new Error(le&&le.name==='AbortError'?'Sign-in timed out after 30 seconds. Check the connection and try again.':'Could not reach the server. Check the connection and try again.');}
      finally{if(lt)clearTimeout(lt);}
      var res=lres;var obj=await res.json();if(!res.ok||!obj.data||!obj.data.tokens)throw new Error(obj.message||'Sign-in failed.');token=obj.data.tokens.access;sessionStorage.setItem(ownKey,token);$('password').value='';await load();$('login-message').textContent='';}catch(err){token='';sessionStorage.removeItem(ownKey);$('login-message').textContent=(err&&err.message)||'Sign-in failed.';}finally{busy(submit,false);}};
  $('signout').onclick=function(){sessionStorage.removeItem(ownKey);token='';$('admin').hidden=true;$('login-panel').hidden=false;$('login-message').textContent='Gallery session closed. Other hotel sessions are unchanged.';};
  async function offlineStatus(){var status=await JoneOffline.status();$('offline-status').textContent=status.ready?'Ready for offline: '+status.count+' selected files saved on this device.':status.reason;if(navigator.storage&&navigator.storage.estimate){var q=await navigator.storage.estimate();$('quota').textContent='Storage used: '+Math.round((q.usage||0)/1048576)+' MB; estimated quota: '+Math.round((q.quota||0)/1048576)+' MB.';}}
  $('refresh-offline').onclick=async function(){try{currentCatalog=await JoneOffline.freshCatalog();var box=$('offline-choices');box.replaceChildren();currentCatalog.data.rooms.forEach(function(room){var group=document.createElement('fieldset'),legend=document.createElement('legend');legend.textContent='ROOM '+room.number;group.appendChild(legend);var label=document.createElement('label'),check=document.createElement('input');check.type='checkbox';check.className='offline-room';check.value=room.id;check.checked=true;label.append(check,document.createTextNode(' Room images'));group.appendChild(label);room.media.filter(function(m){return m.kind==='video';}).forEach(function(m,i){var l=document.createElement('label'),v=document.createElement('input');v.type='checkbox';v.className='offline-video';v.value=m.id;l.append(v,document.createTextNode(' Video '+(i+1)));group.appendChild(l);});box.appendChild(group);});await offlineStatus();}catch(e){$('offline-status').textContent=e.message;}};
  $('sync').onclick=async function(){if(!currentCatalog){$('offline-status').textContent='Refresh selections first.';return;}this.disabled=true;try{try{if(navigator.storage&&navigator.storage.persist)navigator.storage.persist().catch(function(){});}catch(_){}var rooms=Array.from(document.querySelectorAll('.offline-room:checked')).map(function(x){return x.value;}),videos=Array.from(document.querySelectorAll('.offline-video:checked')).map(function(x){return x.value;});await JoneOffline.sync(currentCatalog,rooms,videos,function(done,total,failures){$('offline-status').textContent='Synchronizing '+done+' / '+total+' files; failures: '+failures.length;});await offlineStatus();}catch(e){$('offline-status').textContent='Not ready: '+e.message;}finally{this.disabled=false;}};
  $('remove-offline').onclick=async function(){if(!confirm('Remove downloaded gallery media from this device?'))return;await JoneOffline.remove();await offlineStatus();};
  window.addEventListener('tv-gallery-offline-change',offlineStatus);
  if('serviceWorker' in navigator)navigator.serviceWorker.register('/rooms-gallery/sw.js',{scope:'/rooms-gallery/'}).then(offlineStatus).catch(offlineStatus);else offlineStatus();
  token=getToken();if(token)load().catch(function(e){$('login-message').textContent=e.message;});
}());
