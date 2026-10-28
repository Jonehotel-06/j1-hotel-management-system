/* tests-js/test-pos-table-summary-and-controls.js
   POS table summaries, obsolete status controls, bar-table selection and the
   desktop menu-management layout (static source assertions, same style as
   test-pos-create-errors.js):
     1. every table surface shows the server-computed order count, ordered total
        and (when there is one) the unpaid balance, in naira;
     2. the obsolete Submit / Start / Ready status-change controls are gone from
        the POS order list while the Cancel lifecycle control is untouched;
     3. a draft that is delivered in one step is still submitted to its
        production station first, so the lifecycle stays intact;
     4. bar orders pick a registered, active table from a picker instead of a
        free-text label;
     5. menu management is a full-width desktop surface with a two-column grid,
        not a cramped sidebar widget.
   Run from /frontend: node --test tests-js/test-pos-table-summary-and-controls.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

const POS = fs.readFileSync(path.join(FRONTEND, "dashboard", "pos.html"), "utf8");

test("the table register card shows the server's order count, ordered total and unpaid balance", () => {
  const render = POS.slice(POS.indexOf("function renderRestaurantTables()"), POS.indexOf("function renderBarTableChoices("));
  assert.match(render, /String\(table\.guest_order_count\|\|0\)/, "the register card prints the relevant order count");
  assert.match(render, /money\(table\.total_ordered_amount,'NGN'\)/, "the register card prints the ordered total");
  assert.match(render, /Number\(table\.unpaid_balance\|\|0\)>0/, "an unpaid balance is shown only when it is positive");
  assert.match(render, /money\(table\.unpaid_balance,'NGN'\)/, "the unpaid balance is formatted in naira");
});

test("the open table-session card shows the ordered total alongside the counts", () => {
  const render = POS.slice(POS.indexOf("function renderRestaurantSessions()"), POS.indexOf("function selectRestaurantTableSession("));
  assert.match(render, /session\.unpaid_order_count\+' unpaid · '/, "the ordered total sits next to the existing counts");
  assert.match(render, /JONE\.esc\(money\(session\.total_ordered_amount,'NGN'\)\)\+' ordered'/, "the session total is formatted in naira");
});

test("table summaries refresh after every operation that can change them", () => {
  assert.match(POS, /await loadRestaurantSessions\(true\);await loadRestaurantTables\(true\);\}/, "opening a session refreshes both surfaces");
  assert.match(POS, /Table session transferred\.','success'\);await loadRestaurantSessions\(true\);await loadRestaurantTables\(true\);\}/, "a transfer refreshes both surfaces");
  assert.match(POS, /Restaurant table session closed\.','success'\);await loadRestaurantSessions\(true\);await loadRestaurantTables\(true\);\}/, "closing a session refreshes both surfaces");
  assert.match(POS, /loadOrders\(\);loadRestaurantSessions\(true\);loadRestaurantTables\(true\);\}/, "creating a draft refreshes the summaries");
  assert.match(POS, /loadOrders\(\);loadKitchen\(\);loadRestaurantSessions\(true\);loadRestaurantTables\(true\);\}/, "delivering an order refreshes the summaries");
  assert.match(POS, /refreshes\.push\(loadRestaurantTables\(true\)\);/, "the 15s live refresh also refreshes the register");
});

test("the obsolete Submit / Start / Ready status-change controls are removed", () => {
  assert.doesNotMatch(POS, /data-pos-action="submit"/, "no Submit button remains");
  assert.doesNotMatch(POS, /data-pos-action="PREPARING"/, "no Start button remains");
  assert.doesNotMatch(POS, /data-pos-action="READY"/, "no Ready button remains");
  assert.doesNotMatch(POS, />Submit<\/button>/, "no Submit label remains");
  assert.doesNotMatch(POS, />Start<\/button>/, "no Start label remains");
  assert.doesNotMatch(POS, />Ready<\/button>/, "no Ready label remains");
});

test("the valid lifecycle controls survive: Cancel stays, Deliver covers every open state", () => {
  const buttons = POS.slice(POS.indexOf("function actionButtons(order){"), POS.indexOf("function renderOrders(rows)"));
  assert.match(buttons, /\["DRAFT","SUBMITTED","PREPARING","READY"\]\.indexOf\(status\)>=0\)buttons\+='<button[^>]*data-pos-action="DELIVERED"/, "Deliver is offered for every open state");
  assert.match(buttons, /data-pos-status="'\+JONE\.esc\(status\)\+'"/, "Deliver carries the order's current status");
  assert.match(buttons, /\["DRAFT","SUBMITTED","PREPARING","READY"\]\.indexOf\(status\)>=0\)buttons\+='<button[^>]*data-pos-action="CANCELLED"/, "the Cancel control is untouched");
  assert.match(POS, /data-pos-action="CANCELLED"/);
});

test("a delivered draft is submitted to its production station first", () => {
  const advanceStart = POS.indexOf("async function advanceOrder(");
  const advance = POS.slice(advanceStart, POS.indexOf("\n  function ", advanceStart));
  assert.match(advance, /var deliveringDraft=action==="DELIVERED"&&String\(currentStatus\|\|""\)==="DRAFT";/, "the draft case is detected from the order's status");
  assert.match(advance, /if\(deliveringDraft\)await window\.API\.submitPosOrder\(reference,\{\}\);/, "the draft is submitted before the status update");
  assert.match(advance, /await window\.API\.updatePosOrderStatus\(reference,action\);/, "the delivery status is then applied");
});

test("bar orders pick a registered, active table instead of typing a label", () => {
  assert.match(POS, /<select class="select" id="pos-table">/, "the bar table field is a picker, not a free-text input");
  assert.doesNotMatch(POS, /<input class="input" id="pos-table"/, "the free-text bar table input is gone");
  const choices = POS.slice(POS.indexOf("function renderBarTableChoices("), POS.indexOf("function renderRestaurantSessions(){"));
  assert.match(choices, /filter\(function\(table\)\{return !!table\.is_active;\}\)/, "only active tables are offered");
  assert.match(choices, /<option value="'\+JONE\.esc\(table\.code\)\+'">'\+JONE\.esc\(table\.code\)/, "the registered table code is the option value and label");
  assert.match(choices, /Pickup · no table/, "there is an explicit no-table option");
  assert.match(POS, /register=document\.querySelector\("\[data-pos-restaurant-table-register\]"\);\n    renderBarTableChoices\(\);/, "the picker is (re)built whenever the register renders");
  assert.match(POS, /if\(bar\)renderBarTableChoices\(\);/, "switching to bar mode rebuilds the picker");
  assert.match(POS, /renderBarTableChoices\(item\.table_number\);/, "a QR bar request preselects its table");
});

test("menu management is a full-width desktop surface with a two-column grid", () => {
  // The panel must sit outside the aside: the aside is the narrow right column.
  const asideStart = POS.indexOf('<aside style="display:grid;gap:1.25rem;">');
  const panelStart = POS.indexOf('<details class="panel" data-pos-menu-admin hidden>');
  const asideEnd = POS.indexOf("</aside>", asideStart);
  assert.ok(panelStart > asideEnd, "the menu panel is a sibling of the workspace, not a widget inside the aside");

  const style = POS.slice(POS.indexOf("<style>"), POS.indexOf("</style>"));
  assert.match(style, /\.pos-menu-grid\{display:grid;grid-template-columns:minmax\(0,1fr\);gap:1\.5rem;align-items:start\}/, "the grid stacks on narrow screens");
  assert.match(style, /@media\(min-width:1101px\)\{\.pos-menu-grid\{grid-template-columns:minmax\(0,1fr\) minmax\(0,1\.2fr\)\}\}/, "categories and items sit side by side on desktop");
  assert.match(style, /\.pos-menu-column\{min-width:0\}/, "columns cannot push the grid past the viewport");

  const markup = POS.slice(panelStart, POS.indexOf("</details>", panelStart));
  assert.match(markup, /<div class="pos-menu-grid">/, "the two tables are wrapped in the desktop grid");
  assert.match(markup, /<section class="pos-menu-column">/, "each table lives in its own grid column");
  assert.match(markup, /data-tbody="pos-menu-categories"/, "the categories table is inside a column");
  assert.match(markup, /data-tbody="pos-menu-items"/, "the items table is inside a column");
});
