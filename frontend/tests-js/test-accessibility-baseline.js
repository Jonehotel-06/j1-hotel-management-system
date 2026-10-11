/* tests-js/test-accessibility-baseline.js
   Static accessibility regression guards for the public pages (audit N11).

   The audit's Axe scans reported aria-hidden-focus, color-contrast,
   heading-order, label-content-name-mismatch and page-has-heading-one on the
   public routes. A browser-based scan is the real check, but it cannot run in
   this suite — so these tests pin the *source-level* invariants that made those
   findings possible, and the contrast maths of the design tokens (which is
   arithmetic, not a rendering question, and so is fully checkable here).

   Run from /frontend: node --test tests-js/test-accessibility-baseline.js
*/
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

/* Every public (non-dashboard) page shipped to guests. */
const PUBLIC_PAGES = fs
  .readdirSync(FRONTEND)
  .filter((name) => name.endsWith(".html"))
  .sort();

function readPage(name) {
  return fs.readFileSync(path.join(FRONTEND, name), "utf8");
}

/* Views inside a page that are mutually exclusive at runtime (one screen per
   state) may each carry their own H1 — that is not a second document title. */
function stripScripts(source) {
  return source.replace(/<script\b[\s\S]*?<\/script>/g, "");
}

function headings(source) {
  const out = [];
  const pattern = /<h([1-6])\b[^>]*>/g;
  let match;
  while ((match = pattern.exec(source)) !== null) out.push(Number(match[1]));
  return out;
}

/* Pages whose screens are mutually exclusive at runtime: exactly one of them is
   rendered, so each screen may carry the page's H1. Anything else must have a
   single H1, and every alternative H1 here must sit in a screen that ships
   hidden (asserted below) — otherwise two H1s would be visible at once. */
const MULTI_SCREEN_H1_PAGES = { "booking-confirmation.html": 3 };

test("every public page declares a single H1, or one per exclusive screen", () => {
  const failures = [];
  for (const name of PUBLIC_PAGES) {
    const source = stripScripts(readPage(name));
    const h1 = headings(source).filter((level) => level === 1).length;
    const expected = MULTI_SCREEN_H1_PAGES[name] || 1;
    if (h1 !== expected) failures.push(`${name}: ${h1} H1 (expected ${expected})`);
    if (expected > 1) {
      const hiddenScreens = (source.match(/id="(success|failure)-screen"[^>]*style="[^"]*display:\s*none/g) || [])
        .length;
      if (hiddenScreens !== expected - 1) {
        failures.push(`${name}: ${hiddenScreens} alternative screen(s) ship hidden, expected ${expected - 1}`);
      }
    }
  }
  assert.deepEqual(failures, [], "each rendered screen needs its own first-level heading");
});

test("no public page skips a heading level", () => {
  const failures = [];
  for (const name of PUBLIC_PAGES) {
    const levels = headings(stripScripts(readPage(name)));
    let previous = null;
    for (const level of levels) {
      if (previous !== null && level > previous + 1) {
        failures.push(`${name}: h${previous} → h${level}`);
      }
      previous = level;
    }
  }
  assert.deepEqual(failures, [], "heading levels may only step down one at a time");
});

test("an aria-hidden container never holds focusable content", () => {
  /* The mobile drawer ships aria-hidden="true"; its links must therefore also
     be non-focusable. js/navigation.js sets `inert` and css/public.css hides
     the closed drawer's subtree, which is what this test pins. */
  const navigation = fs.readFileSync(path.join(FRONTEND, "js", "navigation.js"), "utf8");
  assert.match(navigation, /drawer\.inert\s*=\s*!open/, "the closed drawer must be made inert");
  assert.match(navigation, /setAttribute\(\s*"inert"/, "browsers without the inert property need the attribute");

  const css = fs.readFileSync(path.join(FRONTEND, "css", "public.css"), "utf8");
  assert.match(
    css,
    /\.mobile-drawer:not\(\.open\)\s*\{[^}]*visibility:\s*hidden/,
    "the closed drawer must leave the accessibility tree, not just slide off-canvas"
  );

  /* Static markup: nothing focusable may sit inside a permanently aria-hidden
     wrapper. The drawer is handled at runtime, so it is excluded by name. */
  const focusable = /<(a|button|input|select|textarea|iframe)\b/i;
  for (const name of PUBLIC_PAGES) {
    const source = stripScripts(readPage(name));
    const pattern = /<([a-z0-9-]+)([^>]*aria-hidden="true"[^>]*)>([\s\S]*?)<\/\1>/gi;
    let match;
    while ((match = pattern.exec(source)) !== null) {
      const attrs = match[2] + " " + match[3];
      if (/mobile-drawer/.test(match[0])) continue; // runtime-managed (inert + visibility)
      if (focusable.test(attrs)) {
        assert.fail(`${name}: focusable content inside aria-hidden="${match[0].slice(0, 60)}…"`);
      }
    }
  }
});

test("a labelled control's visible text is part of its accessible name", () => {
  const failures = [];
  for (const name of PUBLIC_PAGES) {
    const source = stripScripts(readPage(name));
    const pattern = /<(a|button)\b([^>]*aria-label="([^"]+)"[^>]*)>([\s\S]*?)<\/\1>/gi;
    let match;
    while ((match = pattern.exec(source)) !== null) {
      const [, tag, , label, inner] = match;
      const visible = inner
        .replace(/<(script|style|svg)\b[\s\S]*?<\/\1>/gi, " ")
        .replace(/<[^>]+>/g, " ")
        .replace(/\s+/g, " ")
        .trim();
      if (!visible) continue; // icon-only control: the label IS the name
      if (!label.toLowerCase().includes(visible.toLowerCase())) {
        failures.push(`${name}: <${tag} aria-label="${label}"> shows "${visible.slice(0, 40)}"`);
      }
    }
  }
  assert.deepEqual(failures, [], "visible label text must be contained in the accessible name");
});

/* ----------------------------------------------------------- contrast maths */

function relativeLuminance(hex) {
  const value = hex.replace("#", "");
  const channels = [0, 2, 4].map((offset) => parseInt(value.slice(offset, offset + 2), 16) / 255);
  const linear = channels.map((channel) =>
    channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
  );
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
}

function contrastRatio(a, b) {
  const [hi, lo] = [relativeLuminance(a), relativeLuminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

function token(block, name) {
  const match = new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})`).exec(block);
  assert.ok(match, `token --${name} must be a hex colour`);
  return match[1];
}

function themeBlock(source, selector) {
  const start = source.indexOf(selector);
  assert.notEqual(start, -1, `${selector} block must exist`);
  return source.slice(start, source.indexOf("}", start));
}

test("text tokens meet WCAG AA on every surface they are used on", () => {
  const variables = fs.readFileSync(path.join(FRONTEND, "css", "variables.css"), "utf8");
  const themes = fs.readFileSync(path.join(FRONTEND, "css", "themes.css"), "utf8");

  const light = themeBlock(variables, ":root {");
  const dark = themeBlock(themes, 'html[data-theme="dark"]');

  const cases = [
    ["light", light, ["color-background", "color-surface", "color-surface-alt", "color-surface-deep"]],
    ["dark", dark, ["color-background", "color-surface", "color-surface-alt", "color-surface-deep"]],
  ];

  for (const [theme, block, surfaces] of cases) {
    for (const textToken of ["color-text", "color-text-muted", "color-text-faint", "color-heading"]) {
      const foreground = token(block, textToken);
      for (const surface of surfaces) {
        const ratio = contrastRatio(foreground, token(block, surface));
        assert.ok(
          ratio >= 4.5,
          `${theme}: --${textToken} (${foreground}) on --${surface} is ${ratio.toFixed(2)}:1, below AA 4.5:1`
        );
      }
    }
  }
});

test("the small-text tokens in the footer keep AA contrast", () => {
  const css = fs.readFileSync(path.join(FRONTEND, "css", "public.css"), "utf8");
  const variables = fs.readFileSync(path.join(FRONTEND, "css", "variables.css"), "utf8");
  const footerBackground = token(themeBlock(variables, ":root {"), "brand-charcoal-deep");

  const colours = [...css.matchAll(/\.footer-(?:col|brand)[^{]*\{[^}]*color:\s*(#[0-9a-fA-F]{6})/g)].map(
    (match) => match[1]
  );
  assert.ok(colours.length > 0, "footer text colours must be discoverable");
  for (const colour of colours) {
    const ratio = contrastRatio(colour, footerBackground);
    assert.ok(ratio >= 4.5, `footer text ${colour} on ${footerBackground} is ${ratio.toFixed(2)}:1`);
  }
});
