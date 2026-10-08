/* tests-js/test-sidebar-collapse.js */
/* Desktop sidebar collapse: pure mapper functions in dashboard.js.
   behaviour covers: initial paint from a saved/corrupt preference, toggling,
   and aria-label/title copy — the same logic the runtime click path uses. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { makeContext, loadScript, disposeAll } = require("./harness");

function boot(storageSeed) {
  const context = makeContext();
  if (storageSeed) {
    for (const [k, v] of Object.entries(storageSeed)) context.localStorage.setItem(k, v);
  }
  loadScript(context, "js/config.js");
  loadScript(context, "js/utils.js");
  loadScript(context, "js/dashboard.js");
  return context;
}

test("sidebarPref state: canonical collapsed/expanded values survive", () => {
  for (const [saved, expected] of [["collapsed", "collapsed"], ["expanded", "expanded"]]) {
    const c = boot({ "jone.dashboard.sidebar": saved });
    assert.equal(c.JONE.dashboard._test.sidebarPrefState(saved), expected);
    disposeAll();
  }
});

test("sidebarPref state: corrupt/absent values resolve to expanded (fail-open)", () => {
  for (const saved of [null, "closed", "true", "COLLAPSED", "0", ""]) {
    const c = boot();
    assert.equal(c.JONE.dashboard._test.sidebarPrefState(saved), "expanded",
      `value ${JSON.stringify(saved)} must default to expanded`);
    disposeAll();
  }
});

test("toggle alternates expansion state", () => {
  const c = boot();
  assert.equal(c.JONE.dashboard._test.sidebarNext("expanded"), "collapsed");
  assert.equal(c.JONE.dashboard._test.sidebarNext("collapsed"), "expanded");
  disposeAll();
});

test("button copy keeps terminology: only Collapse/Expand sidebar", () => {
  const c = boot();
  const open = c.JONE.dashboard._test.sidebarLabelFor(false);
  const shut = c.JONE.dashboard._test.sidebarLabelFor(true);
  assert.equal(open.aria, "Collapse sidebar");
  assert.equal(open.icon, "chevronLeft");
  assert.equal(open.expanded, "true");
  assert.equal(shut.aria, "Expand sidebar");
  assert.equal(shut.icon, "chevronRight");
  assert.equal(shut.expanded, "false");
  disposeAll();
});

test("saved collapsed preference paints collapsed", () => {
  const c = boot({ "jone.dashboard.sidebar": "collapsed" });
  const map = c.JONE.dashboard._test;
  assert.equal(map.sidebarPrefState(c.localStorage.getItem("jone.dashboard.sidebar")), "collapsed");
  assert.equal(map.sidebarLabelFor(true).icon, "chevronRight");
  disposeAll();
});
