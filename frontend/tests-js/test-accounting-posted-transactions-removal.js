/* tests-js/test-accounting-posted-transactions-removal.js
 *
 * Task 08 of targeted_fixes_01 — remove the "Posted Transactions" section from
 * the ACCOUNTING UI ONLY.
 *
 * The risk in a "remove this from the screen" task is accidentally removing the
 * records with it. These tests therefore assert BOTH halves:
 *
 *   A. the interface no longer shows or fetches the posted-transaction list;
 *   B. everything else that touches the same records is intact —
 *      · the transactions API endpoint still exists in the client config;
 *      · the report, summary, balance-sheet, receivables, expenses and night
 *        audit sections still load and still print;
 *      · the manual journal control survives (moved, not deleted);
 *      · the backend models, views and URLs are untouched.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..", "..");
const FRONTEND = path.join(ROOT, "frontend");
const read = (...parts) => fs.readFileSync(path.join(ROOT, ...parts), "utf8");

const ACCOUNTING = read("frontend", "dashboard", "accounting.html");
const CONFIG = read("frontend", "js", "config.js");

/* ---------------------------------------------------------------- *
 * A. The interface removal
 * ---------------------------------------------------------------- */

test("the accounting screen no longer renders a Posted transactions section", () => {
  assert.doesNotMatch(ACCOUNTING, /<h2>Posted transactions<\/h2>/);
  assert.doesNotMatch(ACCOUNTING, /data-acc-section="transactions"/);
  assert.doesNotMatch(ACCOUNTING, /data-tbody="ledger-tx"/);
});

test("no code path populates or opens the removed list", () => {
  assert.doesNotMatch(ACCOUNTING, /function renderTransactions\(/);
  assert.doesNotMatch(ACCOUNTING, /renderTransactions\(/);
  assert.doesNotMatch(ACCOUNTING, /data-ledger-view/);
  /* The list is not fetched at page load either — only the sections that
     remain make requests. */
  const liveFetch = /getReport\(window\.APP_CONFIG\.API_ENDPOINTS\.financeTransactions/;
  assert.doesNotMatch(ACCOUNTING, liveFetch, "the page must not fetch the removed list on load");
});

test("the printed statement no longer lists posted transactions", () => {
  assert.doesNotMatch(ACCOUNTING, /<h2>Posted transactions/);
  assert.doesNotMatch(ACCOUNTING, /buildSheet[\s\S]{0,4000}published transactions/);
  /* ...but it still prints the sections that remain. */
  assert.match(ACCOUNTING, /function buildSheet\(/);
  assert.match(ACCOUNTING, /<h2>Expenses<\/h2>/);
});

test("nothing on the screen still points the reader at the removed list", () => {
  assert.doesNotMatch(ACCOUNTING, /under Guest receivables and Posted transactions/);
  assert.match(ACCOUNTING, /Review the folio balances under Guest receivables below\./);
});

/* ---------------------------------------------------------------- *
 * B. Everything else intact
 * ---------------------------------------------------------------- */

test("the transactions API endpoint and its client config are untouched", () => {
  assert.match(CONFIG, /financeTransactions:\s*"\/api\/admin\/finance\/transactions\/"/);
});

test("the backend records, models and URLs are not touched", () => {
  const urls = read("backend", "apps", "finance", "urls.py");
  assert.match(urls, /transactions\//, "the transactions URL must still be routed");

  const models = read("backend", "apps", "finance", "models.py");
  assert.match(models, /class FinancialTransaction\(/, "transactions model must still exist");
  assert.match(models, /class FinancialLine\(/, "ledger lines model must still exist");
  /* No deletion migration was added for these tables. */
  const migrationsDir = path.join(ROOT, "backend", "apps", "finance", "migrations");
  const migrations = fs.readdirSync(migrationsDir).filter((name) => name.endsWith(".py"));
  for (const name of migrations) {
    const body = fs.readFileSync(path.join(migrationsDir, name), "utf8");
    assert.doesNotMatch(body, /DeleteModel\([\s\S]{0,80}FinancialTransaction/,
      `${name} must not delete the transactions model`);
    assert.doesNotMatch(body, /DeleteModel\([\s\S]{0,80}FinancialLine/,
      `${name} must not delete the ledger-line model`);
  }
});

test("the accounting sections that remain are all still present and loading", () => {
  /* Each of these sections still exists and still has a loader. */
  const sections = [
    'data-acc-section="night-audit"',
    'data-acc-section="expenses"',
  ];
  for (const marker of sections) {
    assert.ok(ACCOUNTING.includes(marker), `missing section ${marker}`);
  }
  assert.match(ACCOUNTING, /EP\.financeSummary/);
  assert.match(ACCOUNTING, /EP\.financeBalanceSheet/);
  assert.match(ACCOUNTING, /EP\.financeArAging/);
  assert.match(ACCOUNTING, /EP\.financeNightAudit/);
  assert.match(ACCOUNTING, /EP\.financeExpenses/);
});

test("the manual journal control survives the removal, at a new home", () => {
  /* The button existed inside the removed section; it must not vanish with it. */
  assert.match(ACCOUNTING, /id="acct-journal"/);
  assert.match(ACCOUNTING, /Post journal entry/);
  /* It is now in the night-audit/operations section. */
  const nightAudit = ACCOUNTING.slice(
    ACCOUNTING.indexOf('data-acc-section="night-audit"'),
    ACCOUNTING.indexOf('data-acc-section="night-audit"') + 1200,
  );
  assert.match(nightAudit, /id="acct-journal"/, "the journal button moved into the operations section");
  /* And it is still wired to the same handler and endpoint. */
  assert.match(ACCOUNTING, /journalBtn\.addEventListener\("click", postJournal\)/);
  assert.match(ACCOUNTING, /window\.API\.create\("financeJournals"/);
});

test("the audit-log and reports routes that still show the records are advertised", () => {
  /* The page explains where the immutable records can still be read, so the
     removal is documented rather than silently losing information. */
  assert.match(ACCOUNTING, /Reports and\s+the audit log|Reports and the audit log/);
});

test("the receivables aging roll and its reconciliation are withdrawn (critical repair A2)", () => {
  /* The guest-receivable aggregate roll must no longer be presented as a
     reliable figure — neither on screen nor in the printed statement. */
  assert.ok(!ACCOUNTING.includes("Guest balances reconcile to"), "the reconciliation roll is withdrawn");
  assert.ok(!ACCOUNTING.includes("do not reconcile to the control account"), "the printed roll warning is withdrawn");
  assert.ok(!ACCOUNTING.includes("Guest receivables as at"), "the printed receivables roll is withdrawn");
  /* The per-folio collections worksheet remains. */
  assert.match(ACCOUNTING, /data-tbody="x-ar-items"/);
});
