(function () {
  'use strict';
  var $ = function(id){return document.getElementById(id);};
  var catalog = {rooms:[], slideshow_seconds:5}, mode = null, page = 0, selection = null;
  var timer = null, playing = true, musicAttempted = false, lastFocus = null, photoIndex = 0;
  var photo = $('photo'), video = $('video'), music = $('music');
  var state = {mode:null, room:null};
  function replace(next){ state = next; history.replaceState(next, ''); render(); }
  function push(next){ state = next; history.pushState(next, ''); render(); }
  function stopModal(){ clearInterval(timer); timer=null; video.pause(); video.removeAttribute('src'); video.load(); photo.removeAttribute('src'); }
  function focus(el){ if(el && !el.disabled) el.focus(); }
  function mediaFor(room, type){return room.media.filter(function(m){return m.kind === type;});}
  function roomsFor(type){return catalog.rooms.filter(function(r){return type === 'image' || mediaFor(r,'video').length;});}
  function updateConnection(){
    var online = navigator.onLine;
    JoneOffline.status().then(function(s){ $('connection').textContent = (online ? 'Online' : 'Offline') + (s.ready ? ' · Offline files ready' : ' · ' + s.reason); });
  }
  function goHome(){
    stopModal(); music.pause(); musicAttempted=false; $('enable-music').hidden=true;
    mode=null; selection=null; $('home').hidden=false; $('gallery').hidden=true; $('overlay').hidden=true;
    focus($('images'));
  }
  function enterMode(kind){
    page=0;
    if(kind==='video' && catalog.music_url){
      music.src=catalog.music_url; music.volume=Math.max(0,Math.min(1,Number(catalog.music_volume)/100));
      if(catalog.music_autoplay){ musicAttempted=true; var result=music.play(); if(result && result.catch) result.catch(function(){ $('enable-music').hidden=false; }); }
      else $('enable-music').hidden=false;
    }
    push({mode:kind,room:null});
  }
  function calc(){
    var box=$('grid-area').getBoundingClientRect();
    var mobile=window.innerWidth<700;
    return JoneGalleryGrid.calculate(roomsFor(mode).length,Math.max(1,box.width),Math.max(1,box.height), {gap:mobile?8:16,minWidth:mobile?145:200,minHeight:mobile?125:150});
  }
  function showGrid(focusId){
    var list=roomsFor(mode), layout=calc();
    page=Math.min(page,Math.max(0,layout.pages-1));
    var grid=$('room-grid'); grid.replaceChildren();
    if(layout.pageSize){
      grid.style.gridTemplateColumns='repeat('+layout.columns+', '+layout.cardWidth+'px)';
      grid.style.gridTemplateRows='repeat('+layout.rows+', '+layout.cardHeight+'px)';
      list.slice(page*layout.pageSize,(page+1)*layout.pageSize).forEach(function(room){
        var button=document.createElement('button'); button.className='room-card'; button.dataset.room=room.id;
        var images=mediaFor(room,'image'); var vids=mediaFor(room,'video'); var thumb=(mode==='video' && vids.length && vids[0].thumbnail)?vids[0].thumbnail:(images.length?images[0].thumbnail:null);
        if(thumb){var img=document.createElement('img'); img.src=thumb; img.alt=''; img.loading='lazy'; img.onerror=function(){img.replaceWith(placeholder());}; button.appendChild(img);} else button.appendChild(placeholder());
        var title=document.createElement('strong'); title.textContent='ROOM '+room.number; button.appendChild(title);
        button.addEventListener('click',function(){lastFocus=button; selection=room; photoIndex=0; push({mode:mode,room:room.id});});
        grid.appendChild(button);
      });
    }
    $('empty').hidden=!!list.length; $('empty').textContent=mode==='image'?'No rooms are available for presentation yet.':'No room videos are available yet.';
    $('page-indicator').textContent=list.length+' room'+(list.length===1?'':'s');
    $('page-label').textContent=layout.pages?'Page '+(page+1)+' of '+layout.pages:'';
    $('prev-page').disabled=page===0; $('next-page').disabled=page>=layout.pages-1;
    if(focusId){var el=grid.querySelector('[data-room="'+String(focusId)+'"]'); focus(el || $('back-home'));}
    else focus(grid.querySelector('button') || $('back-home'));
  }
  function placeholder(){var span=document.createElement('span'); span.className='no-image';span.setAttribute('aria-hidden','true');span.textContent='▧';return span;}
  function schedule(){clearInterval(timer);timer=null;if(playing && selection && mode==='image' && mediaFor(selection,'image').length>1){timer=setInterval(function(){change(1);},Math.max(2,Number(catalog.slideshow_seconds)||5)*1000);}}
  function change(delta){
    if(!selection)return;
    var items=mediaFor(selection,mode); if(!items.length)return;
    photoIndex=(photoIndex+delta+items.length)%items.length;
    $('media-error').hidden=true;
    $('media-count').textContent=(photoIndex+1)+' / '+items.length;
    if(mode==='image'){
      photo.src=items[photoIndex].url; photo.alt='Room '+selection.number;
      if(items.length>1){var next=new Image();next.src=items[(photoIndex+1)%items.length].url;}
    }else{
      video.pause(); var posters=mediaFor(selection,'image');video.poster=items[photoIndex].thumbnail || (posters.length?(posters[0].thumbnail||''):''); video.src=items[photoIndex].url; video.muted=true;video.loop=true;video.playsInline=true;
      $('play-video').hidden=true; var promise=video.play(); if(promise && promise.catch) promise.catch(function(){$('play-video').hidden=false;});
    }
  }
  function openModal(room){
    stopModal(); selection=room; photoIndex=0; playing=true;
    $('overlay').hidden=false; $('modal-title').textContent='ROOM '+room.number; $('modal-info').textContent=room.type || '';
    var items=mediaFor(room,mode); photo.hidden=mode!=='image';video.hidden=mode!=='video';
    $('prev-media').hidden=items.length<2; $('next-media').hidden=items.length<2; $('pause').hidden=mode!=='image'||items.length<2;
    $('pause').textContent='Pause'; $('media-count').hidden=items.length<2; $('media-error').hidden=!!items.length;
    if(!items.length) $('media-error').textContent='No images have been added for this room.';
    else { $('media-error').textContent='Unable to display this media. Please try another item.'; change(0); schedule(); }
    focus($('close'));
  }
  function render(){
    if(!state || !state.mode){goHome();return;}
    mode=state.mode; $('home').hidden=true;$('gallery').hidden=false;
    $('gallery-title').textContent=mode==='image'?'Room images':'Room videos';
    if(state.room){var room=catalog.rooms.find(function(r){return r.id===state.room;}); if(room){openModal(room);return;}}
    var previous=selection && selection.id; stopModal();selection=null;$('overlay').hidden=true;showGrid(previous);
    if(mode!=='video'){music.pause();$('enable-music').hidden=true;}
  }
  function back(){ if(state && state.room){history.back();} else if(state && state.mode){history.back();} }
  function spatial(key){
    var current=document.activeElement, area=!$('overlay').hidden?$('overlay'):(!$('gallery').hidden?$('gallery'):$('home'));
    var items=Array.from(area.querySelectorAll('button')).filter(function(b){return !b.disabled && b.offsetParent!==null;});
    if(!$('enable-music').hidden)items.push($('enable-music'));
    if(!items.length)return;
    if(items.indexOf(current)<0){focus(items[0]);return;}
    var a=current.getBoundingClientRect(), ax=a.left+a.width/2, ay=a.top+a.height/2, best=null, score=Infinity;
    items.forEach(function(b){if(b===current)return;var r=b.getBoundingClientRect(),dx=r.left+r.width/2-ax,dy=r.top+r.height/2-ay;
      var primary=key==='ArrowRight'?dx:key==='ArrowLeft'?-dx:key==='ArrowDown'?dy:-dy;
      var cross=(key==='ArrowLeft'||key==='ArrowRight')?Math.abs(dy):Math.abs(dx);
      if(primary>1){var n=primary+cross*2.5;if(n<score){score=n;best=b;}}});
    if(best)focus(best);
  }
  $('images').onclick=function(){enterMode('image');}; $('videos').onclick=function(){enterMode('video');};
  $('back-home').onclick=back; $('close').onclick=back;
  $('prev-page').onclick=function(){page--;showGrid();}; $('next-page').onclick=function(){page++;showGrid();};
  $('prev-media').onclick=function(){change(-1);schedule();}; $('next-media').onclick=function(){change(1);schedule();};
  $('pause').onclick=function(){playing=!playing;$('pause').textContent=playing?'Pause':'Resume';schedule();};
  $('play-video').onclick=function(){video.play().then(function(){$('play-video').hidden=true;}).catch(function(){});};
  $('enable-music').onclick=function(){music.play().then(function(){$('enable-music').hidden=true;}).catch(function(){});};
  photo.onerror=function(){$('media-error').hidden=false;}; video.onerror=function(){$('media-error').hidden=false;};
  document.addEventListener('visibilitychange',function(){if(document.hidden){clearInterval(timer);timer=null;}else schedule();});
  window.addEventListener('resize',function(){if(mode && !state.room)showGrid();});
  window.addEventListener('popstate',function(e){state=e.state || {mode:null,room:null};render();});
  window.addEventListener('keydown',function(e){if(e.key==='Escape'||e.key==='Backspace'||e.keyCode===461){if(state && state.mode){e.preventDefault();back();}return;}if(/^Arrow/.test(e.key)){e.preventDefault();spatial(e.key);}});
  window.addEventListener('online',updateConnection);window.addEventListener('offline',updateConnection);window.addEventListener('tv-gallery-offline-change',updateConnection);
  if('serviceWorker' in navigator) navigator.serviceWorker.register('/rooms-gallery/sw.js',{scope:'/rooms-gallery/'}).then(updateConnection).catch(updateConnection);
  replace({mode:null,room:null}); updateConnection();
  var controller=typeof AbortController!=='undefined'?new AbortController():null;var timeout=setTimeout(function(){if(controller)controller.abort();},12000);
  fetch(JoneOffline.apiBase()+'/api/rooms-gallery/catalog/',controller?{signal:controller.signal}:{})
    .then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.text();})
    .then(function(text){var result=JSON.parse(text);if(!result.success||!result.data)throw new Error('Invalid gallery response');catalog=result.data;if(mode && !state.room)showGrid();return JoneOffline.noteCatalog(text).then(updateConnection);})
    .catch(function(){ $('connection').textContent=navigator.onLine?'Gallery unavailable · Try again later':'Offline · Gallery is not synchronized'; })
    .finally(function(){clearTimeout(timeout);});
}());
