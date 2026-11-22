/* Inventory-backed retail UI. The server owns prices, availability and settlement. */
(function () {
  "use strict";
  const S=window.ReceptionServices, API=window.API, esc=JONE.esc, money=JONE.formatNaira;
  const path=S.base+'reception/minimart/', $=s=>document.querySelector(s);
  const pendingSlot='jone.minimart.pending';
  let products=[], cart={}, page=1, busy=false, pending=null, refreshTimer, searchTimer;
  /* The cashier's open drawer, as the SERVER reports it for this signed-in user.
     `sessions` is the list the products endpoint returns (already scoped to
     request.user); `session` is the chosen reference and `sessionState` records
     how it was decided, so the UI can explain a missing drawer instead of
     leaving an empty box the cashier cannot act on. */
  let sessions=[], session='', sessionState='unknown';

  function message(text, failed) {
    const box=$('[data-mart-message]'); if(!box) return;
    box.innerHTML=text?'<div class="alert'+(failed?' alert-error':'')+'" role="'+(failed?'alert':'status')+'">'+esc(text)+'</div>':'';
  }
  function filteredProducts() {
    const term=$('#mart-search').value.trim().toLowerCase(), category=$('#mart-category').value;
    return products.filter(p=>{
      const matchesCategory=!category || (String(category).match(/^\d+$/) ? String(p.category_id || '')===category : p.category===category);
      return matchesCategory && [p.name,p.sku,p.barcode || '',p.description || ''].join(' ').toLowerCase().includes(term);
    });
  }
  function currentMethod() { return ($('#mart-payment') && $('#mart-payment').value) || 'CASH'; }
  /* A method that settles through the cash drawer. Only CASH does: card,
     transfer and Paystack settle through clearing, and a room charge is a folio
     receivable. Anything else must NOT carry a cash-session reference. */
  function methodNeedsCashSession(method) { return method==='CASH'; }
  function renderProducts() {
    const rows=filteredProducts();
    $('[data-mart-products]').innerHTML=rows.length?rows.map(p=>'<article class="mart-product"><h3>'+esc(p.name)+'</h3><span class="caption">'+esc(p.category || 'Uncategorized')+' · '+esc(p.sku)+'</span><strong>'+money(p.price)+'</strong><span>'+esc(JONE.formatQuantity(p.quantity,{showUnit:false}))+' '+esc(p.unit)+' · '+esc(p.location)+'</span><span class="stock-warning">'+(p.out_of_stock?'Out of stock':p.low_stock?'Low stock — ask the Store Keeper to replenish':'Available')+'</span><button type="button" class="btn btn-outline" data-add="'+p.id+'" '+(p.out_of_stock || pending || busy?'disabled':'')+' aria-label="Add '+esc(p.name)+' to cart">Add to cart</button></article>').join(''):'<p class="text-muted">No matching retail products. An administrator can configure approved sellable inventory items in Mini Mart products.</p>';
  }
  function cartTotal() {
    return Object.keys(cart).reduce((sum,id)=>{const p=products.find(p=>String(p.id)===id);return sum+(p?Number(p.price)*cart[id]:0);},0);
  }
  function renderCart() {
    const ids=Object.keys(cart); let total=0;
    $('[data-mart-cart]').innerHTML=ids.length?ids.map(id=>{
      const p=products.find(p=>String(p.id)===id);
      if (!p) return '<div class="mart-cart-line">Product no longer available <button type="button" class="btn" data-remove="'+id+'">Remove</button></div>';
      total+=Number(p.price)*cart[id];
      return '<div class="mart-cart-line"><strong>'+esc(p.name)+'</strong><span>'+money(p.price)+' × '+cart[id]+' = '+money(Number(p.price)*cart[id])+'</span><div class="mart-quantity"><button type="button" class="btn btn-outline" data-adjust="'+id+'" data-delta="-1" aria-label="Decrease '+esc(p.name)+'">−</button><input type="number" min="1" max="'+Math.floor(Number(p.quantity))+' step="1" value="'+cart[id]+'" data-quantity="'+id+'" aria-label="Quantity of '+esc(p.name)+'"><button type="button" class="btn btn-outline" data-adjust="'+id+'" data-delta="1" aria-label="Increase '+esc(p.name)+'">+</button><button type="button" class="btn btn-outline" data-remove="'+id+'">Remove</button></div></div>';
    }).join(''):'<p class="text-muted">Your cart is empty. Select a product to begin.</p>';
    $('[data-mart-total]').textContent='Subtotal / payable: '+money(total);
    const method=currentMethod(), room=method==='ROOM_CHARGE';
    /* A room charge is only possible for an in-house guest on a real booking,
       so the button stays disabled until that association exists. */
    const roomReady=!room || ($('#mart-customer').value==='GUEST' && $('#mart-guest').value && $('#mart-booking').value);
    const submit=$('[data-mart-checkout] [type=submit]');
    const needsSession=methodNeedsCashSession(method);
    const cashReady=!needsSession || !!session;
    submit.disabled=busy || !!pending || !ids.length || !cashReady || !roomReady;
    submit.setAttribute('aria-busy',busy?'true':'false');
    submit.textContent=busy?'Processing the sale…':submitLabel(method);
    /* Why the button is disabled has to be visible, not inferred. */
    const hint=[];
    if(!ids.length)hint.push('Add at least one product to the cart.');
    if(needsSession && !cashReady)hint.push(sessionHint());
    if(room && !roomReady)hint.push('Choose the in-house guest and their current booking for a room charge.');
    $('[data-mart-checkout-hint]').textContent=hint.join(' ');
    $('[data-mart-clear]').disabled=busy || !!pending;
    $('[data-mart-recovery]').hidden=!pending;
    $('[data-mart-recover]').disabled=busy;
  }
  function submitLabel(method) {
    const labels={CASH:'Confirm cash received & complete sale',CARD:'Confirm POS payment & complete sale',
                  BANK_TRANSFER:'Confirm transfer received & complete sale',PAYSTACK:'Verify Paystack payment & complete sale',
                  ROOM_CHARGE:'Charge to guest room & complete sale'};
    return labels[method] || labels.CASH;
  }
  function sessionHint() {
    if(sessionState==='loading') return 'Checking your open cash session…';
    if(!sessions.length) return 'You have no open cash session. Open a drawer on the POS page first — a cash sale must be recorded against your own drawer. Card, transfer and room charge do not need one.';
    return 'Select your open cash session.';
  }
  function setQuantity(id,qty) {
    if (busy || pending) return;
    const p=products.find(p=>String(p.id)===String(id));
    if (!p || !Number.isInteger(qty) || qty<1 || qty>Math.min(999,Math.floor(Number(p.quantity)))) {
      message('Choose a whole quantity no greater than the available stock.',true);renderCart();return;
    }
    cart[id]=qty; message(''); renderCart();
  }
  /* ---- Cash session -------------------------------------------------------
     The reference is shown to the cashier in the payment form (and next to the
     drawer it belongs to), always taken from the server's answer for THIS user.
     Nothing is invented: when the server reports no open session the field says
     so and the reason, instead of silently displaying nothing. */
  function applySessions(list) {
    sessions=Array.isArray(list)?list:[];
    const sel=$('#mart-session'); if(!sel) return;
    sel.innerHTML='<option value="">'+(sessions.length?'— Select your open cash session —':'— No open cash session —')+'</option>';
    sessions.forEach(s=>{
      const label=s.reference+(s.location?' · '+s.location:'');
      sel.add(new Option(label,s.reference));
    });
    const references=sessions.map(s=>s.reference);
    if(session && references.indexOf(session)===-1) session='';
    if(!session && sessions.length===1) session=sessions[0].reference;
    if(!session && sessions.length && $('#mart-session').dataset.preferred && references.indexOf($('#mart-session').dataset.preferred)!==-1){
      session=$('#mart-session').dataset.preferred;
    }
    sel.value=session;
    sessionState='ready';
    renderSessionSummary();
    renderCart();
  }
  function renderSessionSummary() {
    const box=$('[data-mart-session-summary]'); if(!box) return;
    const chosen=sessions.find(s=>s.reference===session);
    if(!methodNeedsCashSession(currentMethod())){box.textContent='';box.hidden=true;return;}
    box.hidden=false;
    box.textContent = chosen
      ? 'Your open cash session is '+chosen.reference+(chosen.location?' ('+chosen.location+')':'')+'. This sale will be recorded against it.'
      : (sessions.length?sessionHint():'No open cash session was found for your account, so a cash sale cannot be completed. Open a drawer on the POS page, or take payment by card, transfer or room charge.');
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
      applySessions(data.cash_sessions || []);
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
    const method=currentMethod(), reference=$('#mart-reference').value.trim();
    const declared=String($('#mart-amount').value || '').trim();
    const total=cartTotal();
    const room=method==='ROOM_CHARGE';
    if (room && (!isGuest || !form.elements.guest_id.value || !form.elements.booking_id.value)) {
      return message('A room charge needs the in-house guest and their current booking. Select both, then confirm.',true);
    }
    /* Only a cash sale carries a cash session, and it must be this cashier's
       own open drawer. Switching to card/transfer/room charge earlier in the
       evening must not leave a stale reference in the payload — the server
       rejects that outright ("A room charge does not use a cash session"). */
    if (methodNeedsCashSession(method) && !session) {
      return message(sessionHint(),true);
    }
    if (!room && method!=='CASH' && !reference) return message('Enter the transaction reference printed on the customer\'s proof of payment.',true);
    if (!room && declared && (!(Number(declared)>0) || Number(declared)>total)) return message('Amount received must be greater than zero and no more than the total.',true);
    pending={expected_total:total.toFixed(2),idempotency_key:S.key(),payment_method:method,
             lines:Object.keys(cart).map(id=>({product_id:Number(id),quantity:cart[id]}))};
    if (methodNeedsCashSession(method)) pending.cash_session_reference=session;
    if (!room && method!=='CASH') pending.transaction_reference=reference;
    if (!room && declared) pending.payment_amount=Number(declared).toFixed(2);
    if (isGuest) {pending.guest_id=Number(form.elements.guest_id.value);if (form.elements.booking_id.value) pending.booking_id=Number(form.elements.booking_id.value);}
    try {sessionStorage.setItem(pendingSlot,JSON.stringify(pending));} catch(_) {}
    await send(pending);
  }
  /* Only the fields the chosen method actually needs stay visible: a cash sale
     needs the cashier's drawer, an electronic one needs the customer's
     reference. The amount field is opt-in because most sales are paid in full.

     Switching away from Cash CLEARS the cash-session state (and its stale
     value), because the reference is never submitted for a method that does not
     use a drawer; switching back re-selects this cashier's open drawer from the
     server's answer. Hiding the field while still submitting the value is
     exactly the bug that made a room charge fail with "A room charge does not
     use a cash session." */
  function syncMethodFields() {
    const method=currentMethod(), cash=methodNeedsCashSession(method), room=method==='ROOM_CHARGE';
    const sessionField=$('[data-mart-session-field]');
    const sessionSelect=$('#mart-session');
    if(sessionField) sessionField.hidden=!cash;
    if(sessionSelect){
      sessionSelect.disabled=!cash;
      sessionSelect.required=cash;
      if(cash){
        /* Coming back to Cash: restore this cashier's open drawer (or make it
           obvious that there is none). Nothing is fabricated. */
        if(!session && sessions.length===1) session=sessions[0].reference;
        sessionSelect.value=session;
      }else{
        sessionSelect.value='';
        session='';
      }
    }
    const referenceField=$('[data-mart-reference-field]');
    const amountField=$('[data-mart-amount-field]');
    if(referenceField) referenceField.hidden=cash || room;
    if(amountField) amountField.hidden=cash || room;
    const hint=$('[data-mart-room-charge-hint]');
    if (hint) hint.hidden=!room;
    /* A room charge is a guest-only workflow: switch the customer type and show
       the guest picker rather than silently posting to a walk-in. */
    if (room) {
      const type=$('#mart-customer');
      if (type.value!=='GUEST') {type.value='GUEST';type.dispatchEvent(new Event('change'));}
    }
    const submit=$('[data-mart-submit]');
    if(submit) submit.textContent=submitLabel(method);
    renderSessionSummary();
    renderCart();
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
    $('#mart-session').addEventListener('change',function(){
      session=this.value||'';
      if(this.dataset) delete this.dataset.preferred;
      renderSessionSummary();renderCart();
    });
    /* Room-charge readiness depends on the guest + booking pair, so redraw the
       checkout button whenever either picker changes. */
    $('#mart-guest').addEventListener('change',renderCart);
    $('#mart-booking').addEventListener('change',renderCart);
    $('#mart-payment').addEventListener('change',syncMethodFields);
    syncMethodFields();
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
