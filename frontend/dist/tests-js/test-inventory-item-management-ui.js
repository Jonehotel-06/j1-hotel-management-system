/* Inventory item management UI: the four item types, explicit direct sales,
   internal issues to a department, and the evidence the ledger shows.
   Run from /frontend: node --test tests-js/test-inventory-item-management-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { makeContext, loadScript, FRONTEND, disposeAll } = require("./harness");

test.after(() => disposeAll());

const INVENTORY = fs.readFileSync(path.join(FRONTEND, "dashboard", "inventory.html"), "utf8");

/* The page's inline helpers are single-line minified functions; slice exactly
   one of them (brace-matched) so the assertion runs the real code and not a
   copy of the source text. */
function sliceFunction(source, name) {
  const start = source.indexOf("function " + name + "(");
  assert.notEqual(start, -1, name + " must exist in the inventory page");
  let depth = 0;
  for (let i = source.indexOf("{", start); i < source.length; i += 1) {
    const ch = source[i];
    if (ch === "{") depth += 1;
    else if (ch === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error("unbalanced braces while slicing " + name);
}

function runFunction(name) {
  const code = sliceFunction(INVENTORY, name) + "\n" + name + "();";
  return vm.runInNewContext(code, {});
}

test("the item dialog classifies every stock item into one of the four uses", () => {
  const options = runFunction("itemTypeOptions");
  assert.equal(options.length, 4, "exactly the four documented uses");
  const codes = []; /* host-realm array: the page runs in its own vm realm */
  for (let i = 0; i < options.length; i += 1) codes.push(String(options[i].value));
  assert.deepEqual(codes, ["SELLABLE_PRODUCT", "RECIPE_INGREDIENT", "CONSUMABLE", "REUSABLE_ASSET"]);
  assert.equal(options[0].label, "Directly sellable product (bar/retail, sold through POS)");
  assert.equal(options[1].label, "Recipe ingredient (kitchen; deducted by a recipe)");
  assert.equal(options[2].label, "Operational consumable (issued to a department)");
  assert.equal(options[3].label, "Reusable asset (issued with custody, returned after use)");
  // The classification is offered in both the create and the edit dialog.
  assert.equal((INVENTORY.match(/name:"item_type"/g) || []).length, 2);
  assert.match(INVENTORY, /values:\{base_unit:"PIECE",reorder_level:"0",standard_cost:"0\.00",item_type:"CONSUMABLE",direct_sale_quantity:"1"\}/);
});

test("a directly sellable product is configured explicitly, never inferred from a name", () => {
  assert.match(INVENTORY, /function directSaleMenuPicker\(\)\{return OPS\.searchPicker\(\{name:"direct_sale_menu_item",label:"Sold through POS as \(optional\)",resource:"inventoryMenuItems"/);
  assert.match(INVENTORY, /function directSaleLocationPicker\(\)\{return OPS\.searchPicker\(\{name:"direct_sale_location",label:"Stock is deducted from \(store\)",resource:"inventoryLocations"/);
  assert.match(INVENTORY, /name:"direct_sale_quantity",label:"Base units one menu unit deducts",type:"number",step:"0\.0001",min:"0\.0001",value:"1"/);
  assert.match(INVENTORY, /1 for a bottle sold exactly as it is stocked; 24 when one crate is sold as 24 bottles\./);
  assert.match(INVENTORY, /name:"clear_direct_sale"/);
  // Both dialogs use the shared block, and ticking the box clears the link.
  assert.equal((INVENTORY.match(/concat\(directSaleFields\(/g) || []).length, 2);
  assert.equal((INVENTORY.match(/if\(values\.clear_direct_sale\)\{values\.direct_sale_menu_item=null;\}delete values\.clear_direct_sale;/g) || []).length, 2);
  // The existing item update path is reused — no new endpoint and no new client.
  assert.match(INVENTORY, /API\.update\("inventoryItems"/);
  assert.match(INVENTORY, /function directSaleFields\(configuredNote\)\{/);
});

test("the catalog shows the item type and the direct-sale configuration", () => {
  assert.match(INVENTORY, /<th>Item<\/th><th>Unit<\/th><th>Type<\/th><th>Reorder<\/th><th>Standard cost<\/th><th>Status<\/th>/);
  assert.match(INVENTORY, /JONE\.esc\(item\.item_type_label\|\|""\)/);
  assert.match(INVENTORY, /'<div class="caption">Sold directly as '\+JONE\.esc\(item\.direct_sale_menu_item_sku\|\|""\)/);
  assert.match(INVENTORY, /' from '\+JONE\.esc\(item\.direct_sale_location_code\|\|""\)\+'<\/div>'/);
  // ... and the item rows still declare their empty state for the wider table.
  assert.match(INVENTORY, /DATA\.empty\(itemRows,"inbox","No stock items found","Create an item or adjust the search\.",7\)/);
});

test("stock on hand can be filtered by how the item is used", () => {
  assert.match(INVENTORY, /<label class="field-label" for="inv-item-type">Item type<\/label>/);
  assert.match(INVENTORY, /<option value="REUSABLE_ASSET">Reusable assets<\/option>/);
  assert.match(INVENTORY, /var itemType=document\.getElementById\("inv-item-type"\)\.value;if\(itemType\)params\.item_type=itemType;/);
  assert.match(INVENTORY, /getElementById\("inv-item-type"\)\.addEventListener\("change",function\(\)\{loadBalances\(1\);\}\)/);
  // The balances request keeps the bounded page size it already used.
  assert.match(INVENTORY, /window\.API\.list\("inventoryBalances",params\)/);
});

test("an internal issue records the department, the custodian and the reason", () => {
  const departments = runFunction("departmentOptions");
  const codes = []; /* host-realm array: the page runs in its own vm realm */
  for (let i = 0; i < departments.length; i += 1) codes.push(String(departments[i].value));
  assert.equal(codes[0], "", "the department may be left as not recorded");
  ["KITCHEN", "HOUSEKEEPING", "LAUNDRY", "MAINTENANCE", "BAR", "RESTAURANT"].forEach((code) => {
    assert.ok(codes.indexOf(code) !== -1, code + " must be issuable");
  });
  assert.match(INVENTORY, /title:"Record internal stock issue \/ wastage"/);
  assert.match(INVENTORY, /name:"department",label:"Receiving department",type:"select",required:true,options:departmentOptions\(\)/);
  assert.match(INVENTORY, /name:"recipient",label:"Person receiving \/ taking custody",type:"text",placeholder:"Required for reusable assets \(towels, plates, cutlery\)"/);
  assert.match(INVENTORY, /name:"notes",label:"Reason \/ notes",type:"textarea",rows:2,help:"Why the stock is leaving the store: it is recorded on the immutable movement\."\}/);
  assert.match(INVENTORY, /An internal issue is not a sale: it creates no POS revenue and no extra expense\./);
  // The issue is still idempotent and still goes through the existing endpoint.
  assert.match(INVENTORY, /idempotency_key=OPS\.newIdempotencyKey\("stock-issue"\)/);
  assert.match(INVENTORY, /window\.API\.create\("inventoryIssues",values\)/);
});

test("returns cite the issue they come back from and name the department", () => {
  assert.match(INVENTORY, /name:"department",label:"Returning department",type:"select",required:true,options:departmentOptions\(\)/);
  assert.match(INVENTORY, /name:"recipient",label:"Person returning it",type:"text"/);
  assert.match(INVENTORY, /name:"source_reference",label:"Issue reference \(STM-…\)"/);
  assert.match(INVENTORY, /The ledger refuses a return larger than what was issued\./);
});

test("the movement ledger shows and filters the issuing evidence", () => {
  assert.match(INVENTORY, /<label class="field-label" for="inv-ledger-dept">Department<\/label>/);
  assert.match(INVENTORY, /dept=document\.getElementById\("inv-ledger-dept"\)\.value/);
  assert.match(INVENTORY, /if\(dept\)params\.department=dept;/);
  assert.match(INVENTORY, /row\.department\|\|row\.recipient\?'<div class="caption">'\+JONE\.esc\(row\.department_label\|\|row\.department\|\|""\)/);
  assert.match(INVENTORY, /getElementById\("inv-ledger-dept"\)\.addEventListener\("change",function\(\)\{loadLedger\(1\);\}\)/);
  // Existing ledger filters and columns are untouched.
  assert.match(INVENTORY, /id="inv-ledger-type"/);
  assert.match(INVENTORY, /data-inv-ledger-rows/);
});

test("a hand-off states exactly what was deducted and what was not", () => {
  assert.match(INVENTORY, /var uncontrolled=\(resolution\.uncontrolled\|\|\[\]\)\.map/);
  assert.match(INVENTORY, /Not deducted — no stock recipe and no direct-sale configuration: '/);
  assert.match(INVENTORY, /\+shortage\+missing\+uncontrolled\+/);
  assert.match(INVENTORY, /recipe\.direct_sale\?"Direct sale \(configured\)":human\(recipe\.tracking_mode\|\|""\)/);
  assert.match(INVENTORY, /No stock was deducted for this order\. '/);
  // A parked hand-off says what nothing-deducted means for a guest charge.
  assert.match(INVENTORY, /Nothing was deducted for the products listed below\. Configure the product \(a stock recipe, or a direct-sale configuration\) and process this hand-off again &mdash; the guest was charged exactly as before\./);
  // The exactly-once wording the staff rely on is still there.
  assert.match(INVENTORY, /will not deduct stock a second time/);
  assert.match(INVENTORY, /already processed; no stock was deducted again/);
});

test("a recipe line records the unit the kitchen typed", () => {
  assert.match(INVENTORY, /lineFields:\[\{name:"quantity_per_menu_unit",label:"Quantity per menu unit",type:"number",min:"0\.0001",step:"0\.0001",defaultValue:"1",required:true\},\{name:"unit",label:"Unit you typed \(g, kg, ml, l \\u2014 blank means the item's own unit\)",type:"text",defaultValue:""\}\]/);
  assert.match(INVENTORY, /Type each ingredient in the unit the kitchen actually uses \(250 g of rice is stored as 0\.2500 KG\) and the typed value is kept as evidence; a unit that cannot be converted is refused instead of guessed\./);
  // The converted quantity stays the deducted one, with the typed value shown
  // beside it in the recipe list.
  assert.match(INVENTORY, /qty\(line\.quantity_per_menu_unit,line\)\+\(line\.entered_unit\?" \(typed "\+qty\(line\.entered_quantity,line\)\+" "\+JONE\.esc\(line\.entered_unit\)\+"\)":""\)/);
});

test("no new inventory endpoint was introduced for any of this", () => {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  const endpoints = ctx.window.APP_CONFIG.API_ENDPOINTS;
  assert.equal(endpoints.inventoryItemTypes, undefined);
  assert.equal(endpoints.inventoryDepartments, undefined);
  assert.equal(endpoints.inventoryIssueDepartments, undefined);
  assert.equal(endpoints.inventoryItems, "/api/admin/inventory/items/");
  assert.equal(endpoints.inventoryIssues, "/api/admin/inventory/issues/");
  assert.equal(endpoints.inventoryReturns, "/api/admin/inventory/returns/");
  assert.equal(endpoints.inventoryMovements, "/api/admin/inventory/movements/");
  assert.equal(endpoints.inventoryMenuItems, "/api/admin/inventory/menu-items/");
});
