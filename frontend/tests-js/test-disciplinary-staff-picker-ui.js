/* Disciplinary form staff-picker contract tests.
   Recording an offence must let the operator search for a staff member by
   name/email/employee code; it must never ask for a raw numeric user ID.
   Run from /frontend: node --test tests-js/test-disciplinary-staff-picker-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

const PAGE = fs.readFileSync(path.join(FRONTEND, "dashboard", "staff.html"), "utf8");

test("the disciplinary form selects staff by searchable name, not a typed user ID", () => {
  assert.match(PAGE, /js\/operations\.js/, "staff page must load the shared picker helpers");
  assert.match(PAGE, /OPS\.searchPicker\(/, "disciplinary form must use the shared staff picker");
  assert.match(PAGE, /name:\s*"staff"/, "picker must still submit the staff field the API expects");
  assert.match(PAGE, /label:\s*"Staff member"/);
  assert.match(PAGE, /resource:\s*"staffDirectory"/, "picker must use the centralized staff directory endpoint");
  assert.match(PAGE, /Search staff by name, email, or employee code/);
  assert.doesNotMatch(PAGE, /Staff User ID/, "the raw numeric ID prompt must be gone");
  assert.doesNotMatch(PAGE, /name:\s*"staff",\s*label:\s*"Staff[^"]*",\s*type:\s*"number"/);
});

test("the picker display is name-first, with the ID kept internal", () => {
  assert.match(PAGE, /row\.full_name \|\| row\.email/);
  // The submitted value remains the user id (searchPicker's default value),
  // so the backend payload contract is unchanged.
  assert.match(PAGE, /window\.API\.create\("staffDisciplinary", vals\)/);
});
