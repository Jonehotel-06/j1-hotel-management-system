/* tests-js/test-inline-scripts-parse.js
   Every inline <script> on every shipped page must PARSE. A single unterminated
   string or stray token silently kills all of a page's behaviour (filters,
   clock buttons, terminal actions) while the page still renders — invisible to
   string-matching tests, which is exactly how it reached the workforce console.

   Run from /frontend: node --test tests-js/test-inline-scripts-parse.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

function pagesIn(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (["node_modules", "tests-js", "tests-pwa", "assets", "favicon", "css", "js"].includes(entry.name)) continue;
      pagesIn(full, out);
    } else if (entry.name.endsWith(".html")) {
      out.push(full);
    }
  }
  return out;
}

/* Classic JavaScript only. A <script> that declares another type is a DATA
   block, not code: the page's JSON-LD structured data is JSON parsed by search
   engines, and compiling it as JavaScript would wrongly treat its top-level
   object literal as a labelled block. Those blocks are validated separately
   and more strictly (JSON.parse + Schema.org field checks) in test-seo.js and
   frontend/seo_build.py --check, so nothing escapes verification. */
const JS_TYPES = new Set([
  "", "text/javascript", "application/javascript", "module",
  "text/ecmascript", "application/ecmascript",
]);

function inlineScripts(html) {
  const scripts = [];
  const tagRe = /<script([^>]*)>([\s\S]*?)<\/script>/gi;
  let tag;
  while ((tag = tagRe.exec(html)) !== null) {
    const attrs = tag[1] || "";
    if (/\bsrc\s*=/i.test(attrs)) continue;
    const typeMatch = attrs.match(/\btype\s*=\s*["']([^"']*)["']/i);
    const type = (typeMatch ? typeMatch[1] : "").trim().toLowerCase();
    if (!JS_TYPES.has(type)) continue;
    scripts.push(tag[2]);
  }
  return scripts;
}

test("every inline <script> in the shipped pages parses", () => {
  const pages = pagesIn(FRONTEND);
  assert.ok(pages.length >= 45, `expected the whole site, found ${pages.length} pages`);

  // `new Function` compiles exactly like a classic <script>: syntax errors throw.
  const { Script } = require("node:vm");
  const broken = [];
  for (const page of pages) {
    const html = fs.readFileSync(page, "utf8");
    inlineScripts(html).forEach((code, index) => {
      try {
        new Script(code);
      } catch (err) {
        broken.push(`${path.relative(FRONTEND, page)} inline script #${index + 1}: ${err.message}`);
      }
    });
  }
  assert.deepEqual(broken, [], "unparseable inline script(s) found");
});

test("the pages that drive attendance/POS/audit carry their handlers", () => {
  const required = {
    "dashboard/workforce.html": ["data-wf-rt-action", "[data-wf-tab]", "loadAttendance"],
    "dashboard/terminals.html": ["data-rt-action", "executeReceptionTerminalAction"],
    "dashboard/pos.html": ["pos-create-error", "reportCreateFailure"],
    "dashboard/audit-logs.html": ["data-audit-open", "addEventListener"],
  };
  for (const [page, needles] of Object.entries(required)) {
    const html = fs.readFileSync(path.join(FRONTEND, page), "utf8");
    for (const needle of needles) {
      assert.ok(html.includes(needle), `${page} is missing ${needle}`);
    }
  }
});

test("no page concatenates a ternary's false branch into an unterminated string", () => {
  // The exact shape that broke workforce.html: `'...':'+` (missing closing quote
  // on the empty string) silently swallows the rest of the line.
  const pages = pagesIn(FRONTEND);
  const suspicious = [];
  for (const page of pages) {
    const html = fs.readFileSync(page, "utf8");
    const match = html.match(/:'\+\s*\n/);
    if (match) suspicious.push(`${path.relative(FRONTEND, page)}: ${JSON.stringify(match[0])}`);
  }
  assert.deepEqual(suspicious, [], "ternary branch continued as a stray string concat");
});
