/* tests-js/test-pos-create-errors.js
   POS "Create draft" failure handling: the error has to be VISIBLE and
   actionable, the cart has to survive, and a retry must never duplicate a draft.
   Run from /frontend: node --test tests-js/test-pos-create-errors.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

const POS = fs.readFileSync(path.join(FRONTEND, "dashboard", "pos.html"), "utf8");
const COMPONENTS = fs.readFileSync(path.join(FRONTEND, "css", "components.css"), "utf8");

test("the create-error element is a real, visible alert — not an invisible hidden div", () => {
  const markup = POS.match(/<div[^>]*data-pos-create-error[^>]*><\/div>/);
  assert.ok(markup, "the error container exists");
  assert.match(markup[0], /class="field-error is-banner"/, "it carries the standalone banner class");
  assert.match(markup[0], /role="alert"/, "assistive tech is told when it appears");
  assert.match(markup[0], /tabindex="-1"/, "it can take focus when it appears");
  assert.match(markup[0], /hidden/, "it starts hidden");

  // .field-error is display:none and only .field.has-error shows it; a standalone
  // banner must therefore define its own visible rule, or every message is lost.
  assert.match(COMPONENTS, /\.field-error\.is-banner\s*\{[^}]*display: block/s);
  assert.match(COMPONENTS, /\.field-error\.is-banner:empty\s*\{\s*display: none/);
});

test("setCreateError paints the banner and only the failure path focuses it", () => {
  assert.match(POS, /function setCreateError\(message,options\)\{[^}]*createError\.hidden=!text/,
    "hidden is toggled with the message");
  assert.match(POS, /if\(text&&options&&options\.focus\)/, "focus moves only when asked");
  assert.match(POS, /function reportCreateFailure\(message,options\)\{setCreateError\(message,\{focus:true\}\);JONE\.ui\.toast\(message,"error"/,
    "failures are announced in the banner AND as a toast");
});

test("every rejected create path reports through the visible failure helper", () => {
  const body = POS.slice(POS.indexOf('addEventListener("submit"'), POS.indexOf('document.getElementById("pos-apply")'));
  const failures = body.match(/reportCreateFailure\(/g) || [];
  assert.ok(failures.length >= 6, `expected every validation path to report visibly (got ${failures.length})`);
  assert.match(body, /reportCreateFailure\("Add at least one menu item before creating the draft\."\)/);
  assert.match(body, /reportCreateFailure\("Select an in-house guest for room service\."\)/);
  assert.match(body, /reportCreateFailure\("Select an open registered table session for restaurant dine-in\."\)/);
  assert.doesNotMatch(body, /setCreateError\("(?!")/, "no validation path may bypass the visible helper");
});

test("the cart survives a failure and a lost response is explained honestly", () => {
  const handler = POS.slice(POS.indexOf('addEventListener("submit"'), POS.indexOf('document.getElementById("pos-apply")'));
  assert.match(handler, /catch\(err\)\{/, "the failure path exists");
  assert.doesNotMatch(handler, /catch\(err\)\{[^}]*state\.cart=\[\]/, "a failure must never clear the cart");
  assert.match(handler, /kind==="timeout"\|\|kind==="network"\|\|kind==="offline"/,
    "network/timeout failures are distinguished from validation failures");
  assert.match(handler, /no duplicate draft is created/i,
    "the operator is told the retry is safe (the same order key is reused)");
  // Success is the only place that clears the cart, and it is guarded by the
  // awaited API call resolving.
  assert.match(handler, /var res=await window\.API\.create\("posOrders",payload\);var order=res\.data\|\|\{\};setCreateError\(""\);/);
});

test("the idempotency key follows the order content", () => {
  assert.match(POS, /orderKeyContent:"",/);
  assert.match(POS,
    /var orderContent=JSON\.stringify\(payload\);\s*if\(state\.orderKeyContent!==orderContent\)\{state\.orderKey=newKey\(\);state\.orderKeyContent=orderContent;\}\s*payload\.idempotency_key=state\.orderKey;/s,
    "unchanged content reuses the key (safe retry); a changed order gets a fresh key");
  assert.match(POS, /state\.orderKeyContent="";/, "the key is retired once the draft exists");
});

test("a background menu reload can no longer silently drop the operator's choice", () => {
  assert.match(POS, /function renderItemChoices\(preferredItemId\)/);
  assert.match(POS, /if\(previous&&rows\.some\(function\(item\)\{return String\(item\.id\)===previous;\}\)\)select\.value=previous;/,
    "the item selection is restored when the reloaded menu still offers it");
  assert.match(POS, /if\(previousCategory&&state\.menu\.some\(function\(group\)\{return String\(group\.id\)===previousCategory;\}\)\)category\.value=previousCategory;/,
    "same for the category");
  assert.match(POS, /if\(state\.menuArea!==area\)\{state\.menuArea=area;loadMenu\(area\);\}/,
    "the menu is only re-requested when the mode actually changes service area");
});

test("adding a line and choosing an item still give immediate feedback", () => {
  assert.match(POS, /if\(!item\)\{JONE\.ui\.toast\("Choose a menu item first\.","warning"\);return;\}/);
  assert.match(POS, /if\(!Number\.isInteger\(quantity\)\|\|quantity<1\|\|quantity>100\)\{JONE\.ui\.toast\("Quantity must be between 1 and 100\.","warning"\);return;\}/);
});
