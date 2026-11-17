/* Inventory-backed retail UI. The server owns prices, availability and settlement. */
(function () {
  "use strict";
  const S=window.ReceptionServices, API=window.API, esc=JONE.esc, money=JONE.formatNaira;
  const path=S.base+'reception/minimart/', $=s=>document.querySelector(s);
  const pendingSlot='jone.minimart.pending';
  let products=[], cart={}, page=1, busy=false, pending=null, refreshTimer, searchTimer;
  function message(text, failed) {
    $('[data-mart-message]').innerHTML=text?'<div class="alert'+(failed?' alert-error':'')+'" role="status">'+esc(text)+'</div>':'';
  }
  function filteredProducts() {
    const term=$('#mart-search').value.trim().toLowerCase(), category=$('#mart-category').value;
    return products.filter(p=>{
      const matchesCategory=!category || (String(category).match(/^\d+$/) ? String(p.category_id || '')===category : p.category===category);
      return matchesCategory && [p.name,p.sku,p.barcode || '',p.description || ''].join(' ').toLowerCase().includes(term);
    });
  }
  function renderProducts() {
    const rows=filteredProducts();
    $('[data-mart-products]').innerHTML=rows.length?rows.map(p=>'<article class="mart-product"><h3>'+esc(p.name)+'</h3><span class="caption">'+esc(p.category || 'Uncategorized')+' · '+esc(p.sku)+'</span><strong>'+money(p.price)+'</strong><span>'+esc(p.quantity)+' '+esc(p.unit)+' · '+esc(p.location)+'</span><span class="stock-warning">'+(p.out_of_stock?'Out of stock':p.low_stock?'Low stock — ask the Store Keeper to replenish':'Available')+'</span><button type="button" class="btn btn-outline" data-add="'+p.id+'" '+(p.out_of_stock || pending || busy?'disabled':'')+' aria-label="Add '+esc(p.name)+' to cart">Add to cart</button></article>').join(''):'<p class="text-muted">No matching retail products. An administrator can configure approved sellable inventory items in Mini Mart products.</p>';
  }
  function renderCart() {
    const ids=Object.keys(cart); let total=0;
    $('[data-mart-cart]').innerHTML=ids.length?ids.map(id=>{
      const p=products.find(p=>String(p.id)===id);
      if (!p) return '<div class="mart-cart-line">Product no longer available <button type="button" class="btn" data-remove="'+id+'">Remove</button></div>';
      total+=Number(p.price)*cart[id];
      return '<div class="mart-cart-line"><strong>'+esc(p.name)+'</strong><span>'+money(p.price)+' × '+cart[id]+' = '+money(Number(p.price)*cart[id])+'</span><div class="mart-quantity"><button type="button" class="btn btn-outline" data-adjust="'+id+'" data-delta="-1" aria-label="Decrease '+esc(p.name)+'">−</button><input type="number" min="1" max="'+Math.floor(Number(p.quantity))+'" step="1" value="'+cart[id]+'" data-quantity="'+id+'" aria-label="Quantity of '+esc(p.name)+'"><button type="button" class="btn btn-outline" data-adjust="'+id+'" data-delta="1" aria-label="Increase '+esc(p.name)+'">+</button><button type="button" class="btn btn-outline" data-remove="'+id+'">Remove</button></div></div>';
    }).join(''):'<p class="text-muted">Your cart is empty. Select a product to begin.</p>';
    $('[data-mart-total]').textContent='Subtotal / payable: '+money(total);
    $('[data-mart-checkout] [type=submit]').disabled=busy || !!pending || !ids.length || !$('#mart-session').value;
    $('[data-mart-clear]').disabled=busy || !!pending;
    $('[data-mart-recovery]').hidden=!pending;
    $('[data-mart-recover]').disabled=busy;
  }
  function setQuantity(id,qty) {
    if (busy || pending) return;
    const p=products.find(p=>String(p.id)===String(id));
    if (!p || !Number.isInteger(qty) || qty<1 || qty>Math.min(999,Math.floor(Number(p.quantity)))) {
      message('Choose a whole quantity no greater than the available stock.',true);renderCart();return;
    }
    cart[id]=qty; message(''); renderCart();
  }
  async function refresh() {
    if (busy) return;
    const box=$('[data-mart-products]');
    if (!products.length) box.innerHTML=S.loading();
    try {
      const term=$('#mart-search').value.trim(), category=$('#mart-category').value;
      const query=new URLSearchParams();
      if(term) query.set('search',term);
      if(category) query.set(String(category).match(/^\d+$/)?'category_id':'category',category);
      const data=S.unwrap(await API.get(path+'products/?'+query)); products=data.results || [];
      const cat=$('#mart-category'), keep=cat.value;
      cat.innerHTML='<option value="">All categories</option>';
      if(Array.isArray(data.categories)) data.categories.forEach(c=>cat.add(new Option(c.name,String(c.id))));
      else [...new Set(products.map(p=>p.category).filter(Boolean))].sort().forEach(c=>cat.add(new Option(c,c)));
      cat.value=keep;
      const sel=$('#mart-session'), session=sel.value;
      sel.innerHTML='<option value="">— Select your open cash session —</option>';
      (data.cash_sessions || []).forEach(s=>sel.add(new Option(s.reference+(s.location?' · '+s.location:''),s.reference)));
      if (session) sel.value=session;
      if ((data.cash_sessions || []).length===1) sel.value=data.cash_sessions[0].reference;
      renderProducts();renderCart();
    } catch(err) {box.innerHTML=S.error(err);}
  }
  async function send(payload) {
    if (busy) return;
    busy=true; renderProducts();renderCart();
    try {
      const data=S.unwrap(await API.post(path+'checkout/',payload));
      pending=null;
      try {sessionStorage.removeItem(pendingSlot);} catch(_) {}
      cart={};
      message((data.created?'Sale completed: ':'Previously completed sale recovered: ')+data.sale.reference+' · '+money(data.sale.total)+' · '+data.sale.payment_status);
      page=1;history();
    } catch(err) {
      // A network/5xx failure is ambiguous. Keep the exact payload/key and
      // require recovery, never silently create another sale on the next click.
      const definitive=err.status>=400 && err.status<500 && err.status!==408;
      if (definitive) {pending=null;try {sessionStorage.removeItem(pendingSlot);} catch(_) {}}
      message(err.message || 'Checkout response unavailable. Recover this checkout before retrying.',true);
    } finally {busy=false;renderProducts();renderCart();refresh();}
  }
  async function checkout(e) {
    e.preventDefault(); if (busy || pending || !Object.keys(cart).length) return;
    const isGuest=$('#mart-customer').value==='GUEST', form=e.target;
    if (isGuest && !form.elements.guest_id.value) return message('Select an existing guest.',true);
    const invalid=Object.keys(cart).some(id=>{const p=products.find(p=>String(p.id)===id);return !p || p.out_of_stock || cart[id]>Number(p.quantity);});
    if (invalid) return message('Cart stock changed. Review quantities and refresh stock.',true);
    pending={expected_total:Object.keys(cart).reduce((sum,id)=>sum+Number(products.find(p=>String(p.id)===id).price)*cart[id],0).toFixed(2),idempotency_key:S.key(),cash_session_reference:$('#mart-session').value,
             lines:Object.keys(cart).map(id=>({product_id:Number(id),quantity:cart[id]}))};
    if (isGuest) {pending.guest_id=Number(form.elements.guest_id.value);if (form.elements.booking_id.value) pending.booking_id=Number(form.elements.booking_id.value);}
    try {sessionStorage.setItem(pendingSlot,JSON.stringify(pending));} catch(_) {}
    await send(pending);
  }
  async function history() {
    const box=$('[data-mart-history]'), params=new URLSearchParams(new FormData($('[data-mart-history-filter]')));
    for (const [k,v] of [...params]) if (!v) params.delete(k);
    params.set('page',page);box.innerHTML=S.loading();
    try {
      const data=S.unwrap(await API.get(path+'sales/?'+params)), summary=data.summary;
      $('[data-mart-summary]').textContent=summary.transactions+' transactions · '+summary.items_sold+' items · '+money(summary.total_sales)+' total sales. Best selling: '+(summary.best_selling.map(p=>p.name+' ('+p.quantity+')').join(', ') || 'None')+'. Low stock: '+(summary.low_stock.map(p=>p.name).join(', ') || 'None');
      box.innerHTML=data.results.length?data.results.map(s=>'<details><summary><strong>'+esc(s.reference)+'</strong> · '+esc(JONE.formatDateTime(s.created_at))+' · '+esc(s.guest_name)+' · '+money(s.total)+' · '+esc(s.payment_status)+'</summary><p>'+esc(s.customer_type)+' · Room '+esc(s.room_number || '—')+' · '+esc(s.booking_reference || 'No booking')+' · '+esc(s.payment_method)+' · Staff: '+esc(s.staff_name)+'</p>'+s.lines.map(l=>'<p>'+esc(l.name)+' × '+l.quantity+' @ '+money(l.price)+' = '+money(l.total)+'</p>').join('')+'</details>').join(''):'<p class="text-muted">No Mini Mart sales match these filters.</p>';
      $('[data-mart-page]').textContent='Page '+page+' · '+data.count+' sales';$('[data-mart-prev]').disabled=page<=1;$('[data-mart-next]').disabled=!data.has_next;
    } catch(err) {box.innerHTML=S.error(err);}
  }
  function init() {
    try {pending=JSON.parse(sessionStorage.getItem(pendingSlot));} catch(_) {}
    if (pending) (pending.lines || []).forEach(l=>cart[l.product_id]=l.quantity);
    S.guests($('[data-mart-checkout]'));
    $('#mart-search').addEventListener('input',()=>{renderProducts();clearTimeout(searchTimer);searchTimer=setTimeout(refresh,250);});
    $('#mart-category').addEventListener('change',()=>{renderProducts();refresh();});
    $('[data-mart-products]').addEventListener('click',e=>{const b=e.target.closest('[data-add]');if(b)setQuantity(b.dataset.add,(cart[b.dataset.add] || 0)+1);});
    $('[data-mart-cart]').addEventListener('click',e=>{
      if (busy || pending) return;
      const r=e.target.closest('[data-remove]'),a=e.target.closest('[data-adjust]');
      if(r){delete cart[r.dataset.remove];renderCart();}
      if(a)setQuantity(a.dataset.adjust,cart[a.dataset.adjust]+Number(a.dataset.delta));
    });
    $('[data-mart-cart]').addEventListener('change',e=>{if(e.target.matches('[data-quantity]'))setQuantity(e.target.dataset.quantity,Number(e.target.value));});
    $('[data-mart-clear]').addEventListener('click',()=>{if(!busy&&!pending){cart={};renderCart();}});
    $('#mart-customer').addEventListener('change',()=>{$('[data-mart-guest-fields]').hidden=$('#mart-customer').value!=='GUEST';});
    $('#mart-session').addEventListener('change',renderCart);
    $('[data-mart-checkout]').addEventListener('submit',checkout);$('[data-mart-refresh]').addEventListener('click',refresh);
    $('[data-mart-recover]').addEventListener('click',()=>{if(pending)send(pending);});
    $('[data-mart-history-filter]').addEventListener('submit',e=>{e.preventDefault();page=1;history();});
    $('[data-mart-prev]').addEventListener('click',()=>{if(page>1){page--;history();}});$('[data-mart-next]').addEventListener('click',()=>{page++;history();});
    refresh();history();
    refreshTimer=setInterval(()=>{if(!document.hidden)refresh();},30000);
    window.addEventListener('beforeunload',()=>{clearInterval(refreshTimer);clearTimeout(searchTimer);});
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();
