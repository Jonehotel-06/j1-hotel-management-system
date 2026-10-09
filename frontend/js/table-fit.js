/* js/table-fit.js */
/* ==========================================================================
   Table fit — one shared rule set for every staff data table.

   Long prose wraps between words; short ATOMIC values (amounts, dates,
   references, statuses, counts, action clusters) must stay on one line.
   Rather than hand-marking several hundred cells across forty pages and the
   JavaScript templates that render them, this module reads each table's
   column headings and tags matching heading and body cells with `cell-fit`
   (desktop only, see dashboard.css). It re-runs after a render, so rows added
   by pagination, search or refresh are handled without a page reload.

   Pure DOM work: no network, no storage, no layout reads. The classifier is
   exported for unit tests. Tables may opt out with data-table-fit="off".
   ========================================================================== */
(function (window) {
  "use strict";

  // Headings whose values are short and atomic.
  var ATOMIC = /(^|[^a-z])(date|dates|day|days|time|when|amount|amounts|total|totals|price|prices|rate|rates|balance|balances|paid|due|subtotal|tax|fee|fees|discount|qty|quantity|nights?|rooms?|no|number|ref|reference|id|code|status|state|actions?|phone|ip|count|updated|created|last|seen|check|clock|cost|value|stock|level|period|shift|percent|ngn|rating|score|pct)([^a-z]|$)/i;

  // Headings that hold prose, names or free text. A match here always wins
  // over ATOMIC so "Room type", "Guest name" or "Notes" keep wrapping.
  var PROSE = /(name|description|desc|notes?|summary|address|message|detail|details|comment|comments|reason|subject|title|email|guest|item|menu|type|label|location|text|contact|reviewer|staff|actor|by|preview|body|question|answer|feedback)/i;

  function labelOf(cell) {
    return String((cell && cell.textContent) || "").replace(/\s+/g, " ").trim();
  }

  // Exported for unit tests: is this column heading atomic?
  function isAtomicHeading(label) {
    var text = String(label || "").trim();
    if (!text) return false;
    if (PROSE.test(text)) return false;
    return ATOMIC.test(text);
  }

  function headRow(table) {
    var head = table.tHead;
    if (!head || !head.rows || !head.rows.length) return null;
    return head.rows[head.rows.length - 1];
  }

  // Cells that already declare their own wrapping contract are never touched.
  var OWN_CONTRACT = ["cell-nowrap", "cell-clamp", "cell-guest"];
  function ownsContract(cell) {
    for (var i = 0; i < OWN_CONTRACT.length; i++) {
      if (cell.classList && cell.classList.contains(OWN_CONTRACT[i])) return true;
    }
    return false;
  }

  function columnFlags(table) {
    var row = headRow(table);
    if (!row) return null;
    var flags = [];
    for (var i = 0; i < row.cells.length; i++) {
      var cell = row.cells[i];
      var atomic = cell.colSpan === 1 && isAtomicHeading(labelOf(cell));
      flags.push(atomic);
      if (atomic && !ownsContract(cell)) cell.classList.add("cell-fit");
    }
    return flags;
  }

  function fitTable(table) {
    if (!table || (table.getAttribute && table.getAttribute("data-table-fit") === "off")) return;
    var flags = columnFlags(table);
    if (!flags || flags.indexOf(true) === -1) return;
    var bodies = table.tBodies || [];
    for (var b = 0; b < bodies.length; b++) {
      var rows = bodies[b].rows || [];
      for (var r = 0; r < rows.length; r++) {
        var cells = rows[r].cells || [];
        for (var c = 0; c < cells.length; c++) {
          var cell = cells[c];
          if (cell.colSpan !== 1 || ownsContract(cell)) continue;
          if (flags[cell.cellIndex]) cell.classList.add("cell-fit");
        }
      }
    }
  }

  function fitAll(root) {
    var scope = root && root.querySelectorAll ? root : (typeof document !== "undefined" ? document : null);
    if (!scope || typeof scope.querySelectorAll !== "function") return 0;
    var tables = scope.querySelectorAll("table.table");
    for (var i = 0; i < tables.length; i++) fitTable(tables[i]);
    return tables.length;
  }

  var scheduled = false;
  function schedule() {
    if (scheduled) return;
    scheduled = true;
    var run = function () { scheduled = false; fitAll(); };
    if (typeof window.requestAnimationFrame === "function") window.requestAnimationFrame(run);
    else setTimeout(run, 0);
  }

  function start() {
    fitAll();
    if (typeof MutationObserver !== "function" || !document.body) return;
    // Only element insertions matter (new rows or tables). Our own class
    // changes are attribute mutations and cannot re-trigger the observer.
    new MutationObserver(function (records) {
      for (var i = 0; i < records.length; i++) {
        if (records[i].addedNodes && records[i].addedNodes.length) { schedule(); return; }
      }
    }).observe(document.body, { childList: true, subtree: true });
  }

  window.JONE_TABLE_FIT = { isAtomicHeading: isAtomicHeading, fitTable: fitTable, fitAll: fitAll };

  if (typeof document !== "undefined" && document && document.body) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
    else start();
  }
})(typeof window !== "undefined" ? window : globalThis);
