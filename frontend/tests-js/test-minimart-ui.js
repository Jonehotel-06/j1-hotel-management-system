/* Real Mini Mart DOM + page scripts; no production data or network calls. */
const test=require('node:test'), assert=require('node:assert/strict'), fs=require('node:fs'), path=require('node:path');
const {JSDOM}=require('jsdom');
const root=path.resolve(__dirname,'..');
const tick=()=>new Promise(r=>setTimeout(r,35));
async function boot({fail=false,pending=null}={}) {
  const dom=new JSDOM(fs.readFileSync(path.join(root,'dashboard/minimart.html'),'utf8'),{url:'https://hotel.test/dashboard/minimart.html',runScripts:'outside-only'});
  const w=dom.window, calls=[];
  const products=[{id:1,name:'Test Water',sku:'TW',category:'Drinks',price:'150.00',quantity:'4',unit:'BOTTLE',location:'Reception',low_stock:false,out_of_stock:false},
                  {id:2,name:'Test Soap',sku:'TS',category:'Toiletries',price:'100.00',quantity:'1',unit:'PIECE',location:'Reception',low_stock:true,out_of_stock:false},
                  {id:3,name:'Empty item',sku:'TE',category:'Drinks',price:'50.00',quantity:'0',unit:'PIECE',location:'Reception',low_stock:true,out_of_stock:true}];
  const envelope=data=>({data});
  let rows=[];
  w.API={get:async url=>{
    calls.push({method:'get',url});
    if(url.includes('reception/guests/'))return envelope({results:[{id:8,full_name:'Ada Obi',bookings:[{id:9,reference:'TEST-STAY',rooms:['206'],check_in:'2026-11-16',check_out:'2026-11-18',status:'CHECKED_IN'}]}]});
    if(url.includes('products/'))return envelope({results:products,cash_sessions:[{reference:'TEST-CASH'}]});
    if(url.includes('sales/'))return envelope({results:rows,count:rows.length,has_next:false,summary:{transactions:rows.length,items_sold:rows.length?1:0,total_sales:rows.length?'150.00':'0',best_selling:[],low_stock:[]}});
  },post:async(url,data)=>{
    calls.push({method:'post',url,data});
    if(fail)throw Object.assign(new Error('Response lost'),{status:0});
    const sale={reference:'TEST-SALE',created_at:'2026-11-16T12:00:00Z',guest_name:data.guest_id?'Ada Obi':'Walk-in customer',customer_type:data.guest_id?'GUEST':'WALK_IN',room_number:data.guest_id?'206':'',total:'150.00',payment_status:'PAID',payment_method:'CASH',staff_name:'Test staff',lines:[{name:'Test Water',quantity:1,price:'150.00',total:'150.00'}]};
    rows=[sale];return envelope({created:true,sale,products});
  }};
  if(pending)w.sessionStorage.setItem('jone.minimart.pending',JSON.stringify(pending));
  for(const file of ['utils','reception-services','minimart'])w.eval(fs.readFileSync(path.join(root,'js/'+file+'.js'),'utf8'));
  await tick();return {w,dom,calls,$:s=>w.document.querySelector(s)};
}
function submit(ctx) {ctx.$('[data-mart-checkout]').dispatchEvent(new ctx.w.Event('submit',{bubbles:true,cancelable:true}));}

test('retail search/category, real quantities and disabled out-of-stock product',async()=>{
  const c=await boot();try{
    assert.equal(c.w.document.querySelectorAll('[data-add]').length,3);
    assert.equal(c.$('[data-add="3"]').disabled,true);
    c.$('#mart-search').value='soap';c.$('#mart-search').dispatchEvent(new c.w.Event('input'));
    assert.equal(c.w.document.querySelectorAll('[data-add]').length,1);
    assert.match(c.$('[data-mart-products]').textContent,/Low stock/);
    c.$('#mart-search').value='';c.$('#mart-category').value='Drinks';c.$('#mart-category').dispatchEvent(new c.w.Event('change'));
    assert.equal(c.w.document.querySelectorAll('[data-add]').length,2);
    assert.ok(!/standard_cost|supplier/.test(c.$('[data-mart-products]').textContent));
  }finally{c.w.close();}
});

test('cart add, increase, decrease, remove, clear and over-stock validation',async()=>{
  const c=await boot();try{
    c.$('[data-add="1"]').click();c.$('[data-adjust="1"][data-delta="1"]').click();
    assert.match(c.$('[data-mart-total]').textContent,/300/);
    c.$('[data-adjust="1"][data-delta="-1"]').click();assert.match(c.$('[data-mart-total]').textContent,/150/);
    c.$('[data-add="2"]').click();c.$('[data-add="2"]').click();assert.match(c.$('[data-mart-message]').textContent,/available stock/);
    c.$('[data-remove="2"]').click();c.$('[data-mart-clear]').click();assert.match(c.$('[data-mart-cart]').textContent,/empty/);
    assert.equal(c.calls.filter(x=>x.method==='post').length,0);
  }finally{c.w.close();}
});

test('walk-in checkout sends identifiers/quantities and stale-total guard, not prices or paid flags',async()=>{
  const c=await boot();try{
    c.$('[data-add="1"]').click();submit(c);submit(c);await tick();
    const posts=c.calls.filter(x=>x.method==='post');assert.equal(posts.length,1);
    const data=posts[0].data;assert.equal(data.cash_session_reference,'TEST-CASH');assert.equal(data.expected_total,'150.00');assert.ok(data.idempotency_key);
    assert.equal(data.guest_id,undefined);assert.equal(data.price,undefined);assert.equal(data.payment_status,undefined);
    assert.deepEqual(JSON.parse(JSON.stringify(data.lines)),[{product_id:1,quantity:1}]);
    assert.match(c.$('[data-mart-message]').textContent,/TEST-SALE/);assert.match(c.$('[data-mart-history]').textContent,/PAID/);
    assert.equal(c.w.sessionStorage.getItem('jone.minimart.pending'),null);
  }finally{c.w.close();}
});

test('guest selection uses actual name, booking ID and room context',async()=>{
  const c=await boot();try{
    c.$('#mart-customer').value='GUEST';c.$('#mart-customer').dispatchEvent(new c.w.Event('change'));
    c.$('#mart-guest').value='8';c.$('#mart-guest').dispatchEvent(new c.w.Event('change'));
    assert.equal(c.$('[data-mart-guest-fields]').hidden,false);assert.match(c.$('[data-guest-context]').textContent,/Ada Obi.*206/);
    c.$('[data-add="1"]').click();submit(c);await tick();
    const data=c.calls.find(x=>x.method==='post').data;assert.equal(data.guest_id,8);assert.equal(data.booking_id,9);
  }finally{c.w.close();}
});

test('lost response retains exact checkout for recovery instead of a second sale',async()=>{
  const c=await boot({fail:true});let payload;
  try{
    c.$('[data-add="1"]').click();submit(c);await tick();payload=JSON.parse(c.w.sessionStorage.getItem('jone.minimart.pending'));
    assert.ok(payload.idempotency_key);assert.equal(c.$('[data-mart-recovery]').hidden,false);
    assert.equal(c.$('[data-mart-checkout] [type=submit]').disabled,true);
    c.$('[data-mart-recover]').click();await tick();
    const posts=c.calls.filter(x=>x.method==='post');assert.equal(posts.length,2);assert.equal(posts[0].data.idempotency_key,posts[1].data.idempotency_key);
  }finally{c.w.close();}
  const recovered=await boot({pending:payload});try{
    assert.equal(recovered.$('[data-mart-recovery]').hidden,false);recovered.$('[data-mart-recover]').click();await tick();
    assert.equal(recovered.calls.find(x=>x.method==='post').data.idempotency_key,payload.idempotency_key);
  }finally{recovered.w.close();}
});
