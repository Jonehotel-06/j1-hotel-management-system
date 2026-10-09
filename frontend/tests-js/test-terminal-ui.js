/* Optional workstation attribution remains visibly separate from authorization. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

test("workstation registry is API-backed and has an explicit local selection/clear flow", () => {
  const page = fs.readFileSync(path.join(FRONTEND, "dashboard", "terminals.html"), "utf8");
  const nav = fs.readFileSync(path.join(FRONTEND, "js", "dashboard.js"), "utf8");
  const api = fs.readFileSync(path.join(FRONTEND, "js", "api.js"), "utf8");
  assert.match(page, /boot\(\["admin","manager","general_manager"\]\)/);
  assert.match(page, /API\.list\("workstations"/);
  assert.match(page, /API\.create\("workstations"/);
  assert.match(page, /API\.update\("workstations"/);
  assert.match(page, /setWorkstationReference\(""\)/);
  assert.match(page, /attribution only|informational only|not an account\/session lock/i);
  assert.match(nav, /label: "Workstations"[\s\S]*capabilities: \["terminal\.manage"\]/);
  assert.match(api, /X-JONE-Terminal/);
});
