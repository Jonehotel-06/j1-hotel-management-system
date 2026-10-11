/* tests-js/test-ux-focus-regressions.js
   Static regression guards for the browser-verified fixes of audit N2, N3 and
   the N11 findings that only surfaced in a real browser (aria-hidden-focus on
   the drawer toggle, accessible names on the booking stepper and the date
   buttons, heading level of a data-driven grid, empty states inside tables).

   The paired browser evidence lives in docs/verification/. These tests keep the
   source-level cause from coming back on a machine where no browser can run.

   Run from /frontend: node --test tests-js/test-ux-focus-regressions.js
*/
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

const DASHBOARD_PAGES = fs
  .readdirSync(path.join(FRONTEND, "dashboard"))
  .filter((name) => name.endsWith(".html"))
  .sort();

function read(relativePath) {
  return fs.readFileSync(path.join(FRONTEND, relativePath), "utf8");
}

test("an icon carrier that is itself a control is never marked aria-hidden (N11)", () => {
  const icons = read("js/icons.js");
  // The injected SVG inside a <button data-icon> must stay decorative, but the
  // button itself must keep its role and accessible name.
  assert.match(
    icons,
    /const control = node\.closest\("button, a\[href\]"\);\s*\n\s*if \(control === node\) return;/,
    "js/icons.js must skip aria-hidden when the [data-icon] element is the control itself"
  );
  assert.doesNotMatch(
    icons,
    /if \(!node\.getAttribute\("aria-label"\)\) node\.setAttribute\("aria-hidden", "true"\);\s*\n\s*\/\/ Preserve/,
    "the unconditional aria-hidden branch that hid the drawer toggle must be gone"
  );
});

test("booking stepper keeps a content-derived accessible name that contains its visible text (N11)", () => {
  const stepper = read("js/booking-stepper.js");
  // No overriding aria-label: the name must come from the button's own text.
  assert.doesNotMatch(
    stepper,
    /rec\.btn\.setAttribute\("aria-label"/,
    "the stepper must not override the accessible name with an aria-label"
  );
  assert.match(stepper, /rec\.btn\.removeAttribute\("aria-label"\)/);
  // The step state is still announced, as extra content rather than a label.
  assert.match(stepper, /\(current step\)/);
  assert.match(stepper, /class="sr-only bk-step-state"/);
  // A literal space separates the marker from the label so the visible text
  // ("1 Stay") is a contiguous run inside the accessible name.
  assert.match(stepper, /<\/span> ' \+\s*\n\s*'<span class="bk-step-label">/);
});

test("the booking date buttons fold their visible value into the accessible name (N11)", () => {
  const booking = read("booking.html");
  for (const which of ["checkin", "checkout"]) {
    assert.match(
      booking,
      new RegExp(`data-cal-display="${which}" aria-labelledby="lbl-${which} cal-${which}-value"`),
      `the ${which} button must reference both its field label and its own value`
    );
    assert.match(booking, new RegExp(`id="cal-${which}-value"`));
  }
  // The page script keeps the referenced value span in sync with the display.
  assert.match(booking, /displayIn\.querySelector\("\.cal-display-value"\)\.textContent/);
});

test("the mobile dashboard topbar drops the subtitle and clamps the title (N2)", () => {
  const css = read("css/dashboard.css");
  // Locate the phone media block by its content, not by the first @media rule
  // (the file has several max-width blocks, and only this one owns the topbar).
  const marker = css.indexOf(".dash-title p { display: none; }");
  assert.notEqual(marker, -1, "the phone rule for the topbar subtitle must exist");
  const start = css.lastIndexOf("@media (max-width: 560px)", marker);
  const block = css.slice(start, marker + 400);
  assert.match(block, /\.dash-title p \{ display: none; \}/, "the wrapping subtitle must be dropped on phones");
  assert.match(block, /\.dash-title h1 \{[^}]*-webkit-line-clamp: 2/, "the title must clamp to two lines instead of clipping at 18px");
  assert.match(block, /font-size: var\(--fs-sm\)/, "the phone title size must come from the token");
});

test("a horizontally scrolling table advertises itself and shows a live edge cue (N3)", () => {
  const css = read("css/dashboard.css");
  const wrap = css.slice(css.indexOf(".table-wrap {"));
  assert.match(wrap, /background-attachment: local, local, scroll, scroll/, "the edge shadows must use local/scroll attachment so they track the scroll position");
  assert.match(wrap, /overflow-x: auto/);
  const js = read("js/dashboard.js");
  assert.match(js, /function enhanceScrollRegions\(/, "dashboard.js must make pointer-free scrolling possible");
  assert.match(js, /wrap\.setAttribute\("tabindex", "0"\)/);
  assert.match(js, /wrap\.setAttribute\("role", "region"\)/);
  assert.match(js, /wrap\.setAttribute\("aria-label", /);
  assert.match(js, /enhanceScrollRegions\(\);/, "boot() must call it for every dashboard page");
});

test("no dashboard table ships an unlabelled action column or an unnamed select", () => {
  const failures = [];
  for (const name of DASHBOARD_PAGES) {
    const html = read(path.join("dashboard", name));
    const theads = html.match(/<thead>[\s\S]*?<\/thead>/g) || [];
    for (const head of theads) {
      if (/<th>\s*<\/th>/.test(head)) failures.push(`${name}: empty <th>`);
    }
    // Every select with an id must be named by a label, aria-label or
    // aria-labelledby (axe rule: select-name).
    for (const match of html.matchAll(/<select\b[^>]*>/g)) {
      const tag = match[0];
      const id = (tag.match(/id="([^"]+)"/) || [])[1];
      if (!id) continue;
      const named =
        /aria-label="[^"]+"/.test(tag) ||
        /aria-labelledby="[^"]+"/.test(tag) ||
        html.includes(`for="${id}"`) ||
        html.includes(`<label[^>]*for="${id}"`);
      if (!named) failures.push(`${name}: select #${id} has no accessible name`);
    }
  }
  assert.deepEqual(failures, [], "every table header and select needs an accessible name");
});

test("dynamic empty states and section headings never skip a heading level", () => {
  const dashboardJs = read("js/dashboard.js");
  assert.match(
    dashboardJs,
    /'<h2 class="h3">' \+ JONE\.esc\(title\)/,
    "the shared empty-state renderer must emit h2 (styled as h3), not a bare h3"
  );
  const bookingDetails = read("dashboard/booking-details.html");
  assert.doesNotMatch(bookingDetails, /<div class="review-section"><h4>/, "section headings must not jump from the page h1 to h4");
  assert.match(bookingDetails, /<div class="review-section"><h3 class="h4">/);
  const receiptDetails = read("dashboard/receipt-details.html");
  assert.match(receiptDetails, /<h1 class=\\'h2\\'>Receipt unavailable<\/h1>/, "the receipt error state replaces the only h1 and must carry one");
});

test("text drawn as a loading placeholder is not rendered as low-contrast text", () => {
  const css = read("css/dashboard.css");
  const block = css.slice(css.indexOf(".kpi-grid.is-loading .kpi-label"));
  assert.match(block.slice(0, 700), /font-size: 0;/, "the skeleton must remove the text rather than paint it transparent");
  assert.doesNotMatch(block.slice(0, 700), /color: transparent;/);
  // The unavailable state must stay legible (measured 1.67:1 with opacity .35).
  assert.doesNotMatch(css, /\.kpi-grid\.has-error \.kpi \{ opacity:/);
  assert.match(css, /\.kpi-grid\.has-error \.kpi-value,\s*\n\.kpi-grid\.has-error \.kpi-sub \{ color: var\(--color-text-faint\); \}/);
});

test("navigation never hard-codes an API path and fails loudly when one is missing", () => {
  const auth = read("js/auth.js");
  assert.doesNotMatch(auth, /\|\| "\/api\/auth\/capabilities\/"/, "the capability path must come from the endpoint map");
  assert.match(auth, /The capabilities endpoint is not configured\./);
  const siteConfig = read("js/config.js");
  for (const key of ["authProfile", "passwordChange", "fingerprintEnrollment", "temporaryCredential"]) {
    assert.match(siteConfig, new RegExp(`${key}:\\s*"/api/`), `${key} must exist in the endpoint map`);
  }
  const biometrics = read("js/biometrics.js");
  assert.doesNotMatch(biometrics, /API\.post\("\/api\//, "js/biometrics.js must resolve its paths from config");
});
