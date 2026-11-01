"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { makeContext, loadScript, disposeAll, FRONTEND } = require("./harness");
test.after(disposeAll);

function setup() {
  const ctx = makeContext();
  loadScript(ctx, "js/utils.js");
  const body = {innerHTML:""};
  ctx.JONE.ui = {initChrome(){}, toast(){}};
  ctx.JONE.dashboard = {boot(){return true}, DATA:{}, panel:{open(){},body(){return body}}, statusPill(s){return String(s)}};
  ctx.APP_CONFIG = {API_ENDPOINTS:{procurementAccountingOrders:"/orders/"}, CURRENCY:"NGN"};
  ctx.API = {get:async function(url){ if(url.startsWith('/orders/')) return {data:{supplier_name:'Acme', status:'APPROVED',total_amount:'100',lines:[{item_sku:'A',item_name:'Soap',quantity_ordered:'5',quantity_received:'2',quantity_outstanding:'3',unit_cost:'20'}],goods_receipts:[{reference:'GR-1',received_on:'2026-01-01',received_value:'40'}]}}; return {data:{}}; }};
  ctx.addEventListener = function(){};
  let page = fs.readFileSync(path.join(FRONTEND,'dashboard/accounting.html'),'utf8');
  let script = page.slice(page.indexOf('<script>\n  JONE.ui.initChrome();')+8, page.indexOf('</script>',page.indexOf('<script>\n  JONE.ui.initChrome();')));
  script = script.replace(/\}\)\(\);\s*$/, 'window.__test = {openPurchaseOrder, load};})();');
  vm.runInContext(script,ctx,{filename:'accounting.html'});
  return {ctx, body};
}

test('purchase order opens using helpers in its own scope and displays real data',async()=>{
  const {ctx,body}=setup();
  await ctx.__test.openPurchaseOrder('PO-1');
  assert.doesNotMatch(body.innerHTML,/Purchase order unavailable|not defined/);
  for (const x of ['Acme','Soap','GR-1']) assert.ok(body.innerHTML.includes(x),x);
});

test('reloading reports does not register duplicate delegated financial actions',()=>{
  const {ctx}=setup();
  const before=ctx.document.listeners.click.length;
  ctx.__test.load(); ctx.__test.load();
  assert.equal(ctx.document.listeners.click.length,before);
});
