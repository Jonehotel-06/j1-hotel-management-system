"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { FRONTEND } = require("./harness");
const html = fs.readFileSync(path.join(FRONTEND,"dashboard","procurement.html"),"utf8");

function fn(name) {
  const start=html.indexOf("function "+name+"(");
  assert.ok(start>=0, name);
  const open=html.indexOf("{",start); let depth=0;
  for(let i=open;i<html.length;i++){
    if(html[i]==="{") depth++;
    else if(html[i]==="}") {
      depth--;
      if(depth===0)return html.slice(start,i+1);
    }
  }
  throw new Error("Unbalanced function "+name);
}

test("advance-paid orders show receipt only while quantity remains",()=>{
  const ctx={Number};vm.createContext(ctx);vm.runInContext(fn("canReceive"),ctx);
  assert.equal(ctx.canReceive({status:"PAID",lines:[{quantity_outstanding:"0.6"}]}),true);
  assert.equal(ctx.canReceive({status:"PAID",lines:[{quantity_outstanding:"0.0000"}]}),false);
  assert.equal(ctx.canReceive({status:"DRAFT",lines:[{quantity_outstanding:"1"}]}),false);
  assert.equal(ctx.canReceive({status:"APPROVED",lines:[{quantity_ordered:"1",quantity_received:"0.5"}]}),true);
  assert.match(html,/if\(canReceive\(order\)\)action\("Record receipt"/);
});

test("one receipt dialog keeps its source key across retries",()=>{
  const start=html.indexOf("async function recordReceipt(");
  const end=html.indexOf("function activate(",start);
  const section=html.slice(start,end);
  assert.match(section,/receiptContext\.idempotency_key=OPS\.newIdempotencyKey\("goods-receipt"\)/);
  assert.match(section,/onSubmit:[\s\S]*values\.idempotency_key=receiptContext\.idempotency_key/);
  assert.equal((section.match(/newIdempotencyKey\(/g)||[]).length,1);
});
