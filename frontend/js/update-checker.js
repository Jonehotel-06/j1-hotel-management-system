/* js/update-checker.js */
/* ==========================================================================
   J-ONE HOTEL & LODGE — deployment update checker.

   Detects that a NEWER frontend build has been deployed and tells the user
   with an accessible, J-ONE-branded modal ("A new version is available") so
   nobody keeps running stale JavaScript after a release.

   How it works
   - version.json at the site root is the single source of truth for the
     DEPLOYED version; window.JONE_VERSION (js/version.js, stamped by the
     build) is the version THESE pages were loaded from.
   - The checker fetches version.json with cache-busting ("no-store") on a
     slow cadence (every 15 minutes, at most every 5 when the tab is brought
     back to the foreground) — never every few seconds.
   - A version the user has already dismissed (or refreshed past) is
     remembered per version, so the modal never nags in a loop.
   - SAFETY (this is a live booking system): the checker never reloads on its
     own, never clears booking drafts, tokens or any storage. On PUBLIC
     booking/payment flow pages and during active booking or Paystack flows it
     stays quiet (a toast at most) and remembers the pending update, retrying
     until it is safe to prompt. DASHBOARD staff DO get the modal — a staff
     console must not silently run stale code — but never over a dirty form
     or an in-flight critical operation.
   - "Refresh now" also activates any WAITING service worker (via
     JONE.pwa.refreshToLatest) before reloading, so the click genuinely lands
     on the new release instead of the old worker's caches.
   ========================================================================== */

(function () {
  "use strict";

  window.JONE = window.JONE || {};

  var VERSION_URL = "/version.json";
  var DISMISS_KEY = "jone.update.dismissedVersion";
  var REGULAR_INTERVAL_MS = 15 * 60 * 1000;   // slow background cadence
  var FOCUS_MIN_INTERVAL_MS = 5 * 60 * 1000;  // foreground re-check floor
  var modalShown = false;
  var lastCheck = 0;
  var timer = null;

  /* PUBLIC guest flows where a modal could interrupt a live booking/payment.
     The dashboard is intentionally NOT here anymore: staff must receive
     deployment updates too. Their safety net is the dirty-form/critical-flow
     checks below, plus retry-until-safe scheduling. Dashboard paths that ARE
     mid-operation (check-in/check-out screens with a dirty form) are covered
     by hasDirtyForm(). */
  var SENSITIVE_PATH = /(booking|payment|checkout|check-in|check-out|cancellation|review)/i;
  var RETRY_WHEN_SAFE_MS = 30 * 1000;  // pending update found but not safe yet
  var pendingVersion = null;
  var retryTimer = null;

  function isDevHost() {
    return ["localhost", "127.0.0.1", "[::1]", ""].indexOf(location.hostname) !== -1;
  }
  function log() {
    if (!isDevHost() || !window.console) return;
    var args = Array.prototype.slice.call(arguments);
    args.unshift("[J-ONE update]");
    console.info.apply(console, args);
  }

  function currentVersion() {
    return (window.JONE_VERSION || "").trim();
  }

  function dismissedVersion() {
    try { return localStorage.getItem(DISMISS_KEY) || ""; } catch (_) { return ""; }
  }
  function markDismissed(version) {
    try { localStorage.setItem(DISMISS_KEY, String(version)); } catch (_) {}
  }

  /* A safe moment: not a sensitive PUBLIC flow page, no critical flow in
     progress, no dirty form, page visible. On a sensitive page we defer and
     RETRY — the booking/payment flow must never be interrupted, but the
     update is offered as soon as the visitor is somewhere safe.

     Dashboard pages are handled by the dirty-form/critical-flow checks alone:
     staff must be told about deployments while ON the console, not after
     they happen to leave it. */
  function isSensitivePage() {
    if (/^\/dashboard\//i.test(location.pathname)) return false;
    return SENSITIVE_PATH.test(location.pathname + location.search);
  }
  function inCriticalFlow() {
    return !!(window.JONE && JONE.pwa && JONE.pwa.isCriticalFlowActive && JONE.pwa.isCriticalFlowActive());
  }
  function hasDirtyForm() {
    var forms = document.querySelectorAll("form");
    for (var i = 0; i < forms.length; i++) {
      var fields = forms[i].querySelectorAll("input, select, textarea");
      for (var j = 0; j < fields.length; j++) {
        var f = fields[j];
        if (f.type === "hidden" || f.disabled) continue;
        if (f.type === "checkbox" || f.type === "radio") {
          if (f.checked !== f.defaultChecked) return true;
        } else if (f.value && f.value !== f.defaultValue) return true;
      }
    }
    return false;
  }

  function fetchLatestVersion() {
    return fetch(VERSION_URL + "?t=" + Date.now(), {
      method: "GET",
      cache: "no-store",
      headers: { "Accept": "application/json" }
    }).then(function (res) {
      if (!res.ok) throw new Error("HTTP " + res.status);
      return res.json();
    }).then(function (data) {
      var v = data && data.version;
      return (typeof v === "string" && v.trim()) ? v.trim() : null;
    });
  }

  /* ------------------------- the update modal ------------------------------ */
  function showUpdateModal(latest) {
    if (modalShown) return;
    if (!window.JONE || !JONE.ui || !JONE.ui.modal) return;
    modalShown = true;

    var laterBtn = document.createElement("button");
    laterBtn.type = "button";
    laterBtn.className = "btn btn-outline";
    laterBtn.textContent = "Not now";

    var refreshBtn = document.createElement("button");
    refreshBtn.type = "button";
    refreshBtn.className = "btn btn-accent";
    refreshBtn.textContent = "Refresh now";
    refreshBtn.setAttribute("aria-label", "Refresh now to load the latest version of the site");

    var body = document.createElement("div");
    body.className = "update-dialog";

    var mark = document.createElement("div");
    mark.className = "update-dialog-mark";
    mark.setAttribute("aria-hidden", "true");
    mark.innerHTML =
      '<img src="/assets/icons/logo-official.svg" alt="" width="46" height="62">';

    var heading = document.createElement("h4");
    heading.textContent = "A new version is available";
    heading.style.margin = "0 0 0.35rem";

    var message = document.createElement("p");
    message.style.margin = "0";
    message.textContent =
      "J-ONE HOTEL & LODGE has been updated. Refresh now to load the latest version.";

    var hint = document.createElement("p");
    hint.className = "muted";
    hint.style.margin = "0.75rem 0 0";
    hint.style.fontSize = "0.85rem";
    hint.textContent = "Your booking details are safe — nothing is lost by refreshing.";

    var actions = document.createElement("div");
    actions.className = "modal-actions";
    actions.style.marginTop = "1.25rem";
    actions.style.display = "flex";
    actions.style.gap = "0.75rem";
    actions.style.justifyContent = "flex-end";
    actions.style.flexWrap = "wrap";
    actions.appendChild(laterBtn);
    actions.appendChild(refreshBtn);

    body.appendChild(mark);
    body.appendChild(heading);
    body.appendChild(message);
    body.appendChild(hint);
    body.appendChild(actions);

    JONE.ui.modal.open({
      title: "Update available",
      body: body,
      size: "modal-sm",
      onClose: function () {
        // Closing via Escape / X / backdrop behaves like "Not now" for THIS
        // version: recorded so the same release never re-prompts.
        markDismissed(latest);
        modalShown = false;
      }
    });

    laterBtn.addEventListener("click", function () {
      markDismissed(latest);
      JONE.ui.modal.close();
    });
    refreshBtn.addEventListener("click", function () {
      // Storage (booking draft, session, theme) is deliberately untouched.
      // JONE.pwa.refreshToLatest() first activates a WAITING service worker
      // (so its versioned caches take over) and asks the registration to
      // re-check for one, then reloads; the reload then pulls fresh
      // ?v=-stamped assets. Without this, reload() alone could resurrect the
      // OLD worker's stale-while-revalidate caches on the very next paint.
      refreshBtn.disabled = true;
      refreshBtn.innerHTML = '<span class="btn-spinner"></span> Refreshing\u2026';
      markDismissed(latest);
      var reloadNow = function () {
        try { window.location.reload(); } catch (_) { window.location.href = window.location.pathname; }
      };
      try {
        if (window.JONE && JONE.pwa && JONE.pwa.refreshToLatest) {
          JONE.pwa.refreshToLatest(reloadNow);
        } else {
          reloadNow();
        }
      } catch (_) { reloadNow(); }
    });

    log("update modal shown for", latest);
  }

  function quietNotice(latest) {
    // On sensitive pages: never a modal. A single calm toast says the update
    // will be picked up when the flow is finished.
    if (quietNotice._said) return;
    quietNotice._said = true;
    if (window.JONE && JONE.ui && JONE.ui.toast) {
      JONE.ui.toast(
        "A new version of the site is available and will load when you finish this page.",
        "info",
        { duration: 7000 }
      );
    }
    log("deferred update notice (sensitive page) for", latest);
  }

  /* A deployment was detected but the moment was unsafe (payment in flight,
     dirty form, hidden tab). Remember it and retry on a short LOCAL timer —
     no network request, just re-evaluating safety — so the prompt appears as
     soon as the user is free instead of waiting for the next slow poll. */
  function rememberPending(latest, showQuietlyWhenSensitive) {
    pendingVersion = latest;
    if (showQuietlyWhenSensitive) quietNotice(latest);
    if (retryTimer) return;
    retryTimer = setInterval(function () {
      if (!pendingVersion || modalShown) { clearInterval(retryTimer); retryTimer = null; return; }
      if (document.visibilityState !== "visible") return;
      if (isSensitivePage() || inCriticalFlow() || hasDirtyForm()) return;
      var v = pendingVersion;
      pendingVersion = null;
      clearInterval(retryTimer); retryTimer = null;
      if (v !== dismissedVersion()) showUpdateModal(v);
    }, RETRY_WHEN_SAFE_MS);
  }

  function check(showQuietlyWhenSensitive) {
    lastCheck = Date.now();
    var mine = currentVersion();
    if (!mine) { log("no embedded version marker; skipping"); return Promise.resolve(null); }
    return fetchLatestVersion()
      .then(function (latest) {
        if (!latest || latest === mine) return null;
        log("deployed version", latest, "≠ loaded version", mine);
        if (latest === dismissedVersion()) return null;     // already handled
        if (document.visibilityState !== "visible") { rememberPending(latest, false); return null; }
        if (isSensitivePage() || inCriticalFlow()) {
          rememberPending(latest, showQuietlyWhenSensitive);
          return null;                                       // prompt when safe
        }
        if (hasDirtyForm()) { rememberPending(latest, true); return null; }
        showUpdateModal(latest);
        return latest;
      })
      .catch(function (err) {
        log("version check failed:", err && err.message);
        return null;
      });
  }

  function schedule() {
    if (timer) clearInterval(timer);
    timer = setInterval(function () {
      if (document.visibilityState === "visible") check(false);
    }, REGULAR_INTERVAL_MS);
  }

  function boot() {
    if (!window.fetch) return;                       // ancient browser: skip
    // First check shortly after load (page has settled by then).
    setTimeout(function () { check(true); }, 4000);
    schedule();
    // Re-check when the visitor returns to the tab — but rate-limited, so
    // tab-flipping can never spam the server or the user.
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible" && Date.now() - lastCheck >= FOCUS_MIN_INTERVAL_MS) {
        check(true);
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  window.JONE.updateChecker = {
    check: check,
    currentVersion: currentVersion,
    _internals: {
      showUpdateModal: showUpdateModal,
      isSensitivePage: isSensitivePage,
      hasDirtyForm: hasDirtyForm
    }
  };
})();
