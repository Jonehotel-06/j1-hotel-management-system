/* js/reader-check.js */
/* ==========================================================================
   J-ONE HOTEL & LODGE — Shared fingerprint-station check.

   WHY THIS FILE EXISTS
   ====================
   The fingerprint reader is USB hardware on ONE Windows workstation. Browser
   Local Network Access (Chromium 138+: Firefox 137+, Safari 18.4+) means a
   public page may ONLY reach http://127.0.0.1:<port> after the operator has
   granted this site "local network access". That grant is a permission-policy
   permission: it can only be requested from a real user gesture (a click), and
   a page that has never been granted it cannot even READ the permission state
   without the same gesture — Chromium throws

     SecurityError: The 'local-network-access' permission cannot be read from a
     document with a permissions policy that disallows it

   The desk pages used to work around this by only asking on a capture click.
   The side effect was that the reader panel on Workforce / Terminals / Staff
   Profile could do nothing at all until the operator started a biometric
   action: a status refresh from inside the gesture-less boot had to report
   "reader state unavailable". That is not honest either — the true state is
   "this browser has not been asked yet".

   This module puts ONE control on those pages — "Check fingerprint station" —
   and runs the whole supported detection sequence inside that click:

     1. ask for Local Network Access (the browser's own prompt),
     2. GET  /health   (is the bridge process listening?)
     3. GET  /status   (is the reader attached, driver present, SDK loaded?)
     4. POST /initialize ONLY when the bridge is running and the reader is not
        ready (a reader re-plugged while the page was open),
     5. render exactly the situation that was observed.

   It never captures a fingerprint, never sends anything to the API, holds no
   credential, and never reports a state it did not observe. Pages that already
   expose a richer reader panel (Workforce / Terminals) keep that panel; this
   file only supplies the gesture entry point they were missing, and a compact
   panel for pages that have none (Staff Profile → Enroll fingerprint).
========================================================================== */
(function (window) {
  "use strict";

  var doc = window.document;

  function bio() { return (window.JONE && window.JONE.biometrics) || null; }

  /* Human label for each state the transport can report. Kept in one place so
     the toast, the panel and the console all say the same thing. */
  var LABELS = {
    ready: "Reader ready",
    checking: "Checking…",
    wrong_workstation: "Open this on the reception workstation",
    permission_blocked: "Browser blocked local network access",
    bridge_unreachable: "Bridge not running",
    bridge_timeout: "Bridge did not answer",
    reader_not_ready: "Bridge running, reader not ready",
    unauthorized: "Reader busy or access denied"
  };

  function el(tag, className, text) {
    var node = doc.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  /* Render one describeReader() report into *host*. The report already tells
     six genuinely different failures apart (see js/biometrics.js), so the panel
     only has to pick the right sentence and the right next action. */
  /* The single state key for a report. Also published as data-state on the host
     so a page (and the test suite) can tell the six situations apart without
     parsing sentences. */
  function stateKey(report) {
    if (!report) return "unknown";
    if (report.mode === "mobile") return "wrong_workstation";
    if (report.status === "checking") return "checking";
    if (report.ready) return "ready";
    if (report.status === "permission_blocked") return "permission_blocked";
    if (report.code === "BRIDGE_TIMEOUT") return "bridge_timeout";
    if (!report.bridge_reachable) return "bridge_unreachable";
    if (report.status === "busy") return "busy";
    return "reader_not_ready";
  }

  function render(host, report, onRetry) {
    if (!host) return;
    host.replaceChildren();
    var status = report && report.status;
    var state = stateKey(report);
    host.setAttribute("data-state", state);
    if (report && report.mode) host.setAttribute("data-mode", report.mode);
    var row = el("div", "reader-check-row");
    var badge = el("span", "badge", LABELS.ready);
    if (state === "wrong_workstation") {
      badge.textContent = LABELS.wrong_workstation;
      badge.className = "badge badge-warning";
    } else if (state === "ready") {
      badge.textContent = LABELS.ready;
      badge.className = "badge";
    } else if (state === "checking") {
      badge.textContent = LABELS.checking;
      badge.className = "badge badge-muted";
    } else if (state === "bridge_timeout") {
      badge.textContent = LABELS.bridge_timeout;
      badge.className = "badge badge-danger";
    } else if (state === "bridge_unreachable") {
      badge.textContent = LABELS.bridge_unreachable;
      badge.className = "badge badge-danger";
    } else if (state === "permission_blocked") {
      badge.textContent = LABELS.permission_blocked;
      badge.className = "badge badge-danger";
    } else if (state === "busy") {
      badge.textContent = LABELS.unauthorized;
      badge.className = "badge badge-warning";
    } else {
      badge.textContent = LABELS.reader_not_ready;
      badge.className = "badge badge-warning";
    }
    row.appendChild(badge);

    var detail = el("p", "caption reader-check-detail", (report && report.detail) || "");
    var meta = [];
    if (report && report.bridge_url) meta.push("Bridge: " + report.bridge_url);
    if (report && report.device_serial) meta.push("Serial: " + report.device_serial);
    if (report && report.local_network_permission && report.local_network_permission !== "unsupported") {
      meta.push("Local network access: " + report.local_network_permission);
    }
    var metaLine = el("p", "caption reader-check-meta", meta.join(" · "));

    host.appendChild(row);
    host.appendChild(detail);
    if (meta.join("")) host.appendChild(metaLine);

    if (onRetry) {
      var again = el("button", "btn btn-sm btn-outline", "Check fingerprint station");
      again.type = "button";
      again.addEventListener("click", onRetry);
      host.appendChild(again);
    }
  }

  /* Run the full supported detection sequence. Must be called from a click (or
     any real user gesture) the FIRST time on a page, because that is when the
     browser shows its Local Network Access prompt. */
  var running = false;

  /* Another control on the same page may already hold the reader (a capture in
     progress on Workforce). Two detection sequences at once make the browser
     ask for the same permission twice and make /status race the sensor, so the
     second one is refused here with a sentence instead of a false state. The
     page publishes this hook; a page without one is unaffected. */
  function pageRemainsBusy() {
    try {
      return typeof window.__JONE_READER_BUSY__ === "function" && !!window.__JONE_READER_BUSY__();
    } catch (_) { return false; }
  }

  async function check(host, button) {
    /* Two presses must not start two detection sequences: the second one would
       race the first for the browser's permission prompt and the reader. */
    if (running) return null;
    if (pageRemainsBusy()) {
      render(host, {
        status: "busy", bridge_reachable: true, mode: bio() && bio().workstationMode(),
        detail: "A fingerprint action is already using the reader. Finish it (or wait for it to " +
          "finish) and check again — starting a second read now would interrupt the first.",
        code: "READER_BUSY"
      });
      return null;
    }
    running = true;
    if (button) {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
    }
    try {
      return await runCheck(host);
    } finally {
      running = false;
      if (button) {
        button.disabled = false;
        button.setAttribute("aria-busy", "false");
      }
    }
  }

  async function runCheck(host) {
    var api = bio();
    if (!api) {
      render(host, {
        status: "unavailable", bridge_reachable: false, mode: "desktop",
        detail: "The biometrics module did not load on this page. Reload the page and try again.",
        code: "MODULE_MISSING"
      });
      return null;
    }
    render(host, {
      status: "checking", bridge_reachable: false, mode: api.workstationMode(),
      detail: "Asking this browser for local network access, then checking the bridge at " +
        api.getBridgeUrl() + "…", code: "CHECKING"
    });
    var report;
    try {
      report = await api.describeReader({ requestPermission: true, initialize: true });
    } catch (err) {
      report = {
        status: "unavailable", bridge_reachable: false, mode: api.workstationMode(),
        detail: (err && err.message) || "The reader state could not be read.",
        code: (err && err.code) || "CHECK_FAILED"
      };
    }
    render(host, report, function () { check(host); });
    return report;
  }

  /* Attach the "Check fingerprint station" button and its result host to any
     page. Returns {run} so a page can trigger the same sequence from its own
     controls (e.g. "Enroll fingerprint" asks for the permission here first and
     then captures, instead of the capture click being the first attempt). */
  function mount(options) {
    options = options || {};
    var button = typeof options.button === "string"
      ? doc.querySelector(options.button) : options.button;
    var host = typeof options.host === "string"
      ? doc.querySelector(options.host) : options.host;
    /* Nothing has been observed yet, and the panel says so. Before this the
       host was either empty (which reads as "fine") or carried a sentence from
       a gesture-less boot that could not be trusted. */
    if (host && !host.childNodes.length && !host.textContent.trim()) {
      host.setAttribute("data-state", "not_checked");
      host.appendChild(el("p", "caption reader-check-detail",
        "Not checked yet — press Check fingerprint station to look for the reader."));
    }
    if (button) {
      button.hidden = false;
      button.addEventListener("click", function () { check(host, button); });
    }
    var run = function () { return check(host, button); };
    return { run: run, host: host, button: button };
  }

  window.JONE = window.JONE || {};
  window.JONE.readerCheck = { check: check, mount: mount, render: render, stateKey: stateKey, LABELS: LABELS };
})(window);
