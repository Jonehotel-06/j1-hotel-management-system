/* tests-js/test-dashboard-page-scripts.js */
/* Every dashboard page must load the script that powers it.

   Reported symptom: opening the Amenities screen (and the Breakfast screen)
   rendered a dashboard shell — the sidebar, the title, the boot guard — but
   nothing on the page worked: no guest list, no issue form, no actions.
   The cause was not in the JS files: both pages shipped WITHOUT their
   page-specific <script src="../js/<page>.js"> include, so the page logic
   never ran at all.

   This suite pins the invariant that lets that failure never ship again:
   for every frontend/dashboard/<page>.html that has a counterpart
   frontend/js/<page>.js, the HTML must include that script. A page whose
   script is missing or renamed fails here instead of silently rendering an
   empty screen in production.

   Run: node --test tests-js/test-dashboard-page-scripts.js   (from frontend/) */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const FRONTEND = path.resolve(__dirname, "..");

function source(rel) {
  return fs.readFileSync(path.join(FRONTEND, rel), "utf8");
}

const dashboardPages = fs
  .readdirSync(path.join(FRONTEND, "dashboard"))
  .filter((name) => name.endsWith(".html"))
  .sort();

test("the dashboard has pages to check", () => {
  assert.ok(dashboardPages.length > 20, "expected the full dashboard suite of pages");
});

test("every dashboard page loads its own page script", () => {
  const problems = [];
  for (const page of dashboardPages) {
    const pageName = page.replace(/\.html$/, "");
    const scriptName = "js/" + pageName + ".js";
    if (!fs.existsSync(path.join(FRONTEND, scriptName))) continue; /* no script to load */
    const html = source("dashboard/" + page);
    /* The include must appear as a script src with the exact relative path;
       a query string (?v=…) is expected and fine. */
    const withQuery = 'src="../' + scriptName + '?';
    const bare = 'src="../' + scriptName + '"';
    if (!html.includes(withQuery) && !html.includes(bare)) {
      problems.push(page + " does not include <script src=\"../" + scriptName + "\">");
    }
  }
  assert.deepEqual(problems, [], "dashboard page(s) shipped without their page script:\n" + problems.join("\n"));
});

test("the two pages that lost their script include are fixed", () => {
  for (const page of ["breakfast.html", "amenities.html"]) {
    const pageName = page.replace(/\.html$/, "");
    const html = source("dashboard/" + page);
    assert.ok(
      html.includes('src="../js/' + pageName + '.js?') || html.includes('src="../js/' + pageName + '.js"'),
      page + " must include ../js/" + pageName + ".js — without it the page renders but its logic never runs"
    );
  }
});

test("the page script include sits after dashboard.js (dependency order)", () => {
  /* The page scripts build on window.JONE.dashboard; they must be evaluated
     after it. A include placed above dashboard.js would run against an
     undefined namespace and silently fail at page load. */
  for (const page of ["breakfast.html", "amenities.html"]) {
    const pageName = page.replace(/\.html$/, "");
    const html = source("dashboard/" + page);
    const dashAt = html.indexOf('src="../js/dashboard.js');
    const pageAt = html.indexOf('src="../js/' + pageName + '.js');
    assert.ok(dashAt !== -1 && pageAt !== -1, page + " includes both scripts");
    assert.ok(pageAt > dashAt, page + ": page script must load after dashboard.js");
  }
});
