/* js/pwa.js */
/* ==========================================================================
   PWA layer — service-worker registration, safe update handling, and the
   hotel-branded install experience.

   This is the ONLY place in the frontend that talks to navigator.serviceWorker.
   Pages never register the worker themselves.

   Safety notes
   - Registration is entirely optional: every failure is swallowed and the site
     keeps working exactly as before.
   - A waiting (new) service worker is never activated while the visitor is in
     a booking, payment, or any dirty form. JONE.pwa.beginCriticalFlow() /
     endCriticalFlow() let those flows lock the swap explicitly.
   - Nothing sensitive is written anywhere: only a dismissal flag and the
     install state live in localStorage.
   ========================================================================== */

(function () {
  "use strict";

  window.JONE = window.JONE || {};

  var SW_URL = "/sw.js";
  var SW_SCOPE = "/";
  var DISMISS_KEY = "jone.pwa.installDismissedAt";
  var DISMISS_DAYS = 30;

  var deferredPrompt = null;
  var registration = null;
  var waitingWorker = null;
  var criticalFlows = 0;
  var reloading = false;

  /* ----------------------------- Diagnostics ------------------------------- */
  // Verbose only on local development origins; production stays quiet and
  // never logs URLs, tokens or user data.
  var isDevHost = ["localhost", "127.0.0.1", "[::1]"].indexOf(location.hostname) !== -1;
  function log() {
    if (!isDevHost || !window.console) return;
    var args = Array.prototype.slice.call(arguments);
    args.unshift("[J-ONE PWA]");
    console.info.apply(console, args);
  }

  /* --------------------------- Environment checks --------------------------- */
  function isSupported() {
    return "serviceWorker" in navigator;
  }
  // localhost is a secure context for development; production must be HTTPS.
  function isSecureOrigin() {
    return window.isSecureContext === true || location.protocol === "https:" || isDevHost;
  }
  function isStandalone() {
    return (
      (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) ||
      window.navigator.standalone === true ||
      document.referrer.indexOf("android-app://") === 0
    );
  }
  // iOS detection is only used to swap the install *instructions*; nothing
  // functional depends on it, so a false negative is harmless.
  function isIOS() {
    var ua = navigator.userAgent || "";
    return (
      /iPad|iPhone|iPod/.test(ua) ||
      (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)
    );
  }
  function isIOSSafari() {
    var ua = navigator.userAgent || "";
    return isIOS() && !/CriOS|FxiOS|EdgiOS|OPiOS/.test(ua);
  }

  /* ------------------------------ Dismissal -------------------------------- */
  function recentlyDismissed() {
    try {
      var at = parseInt(localStorage.getItem(DISMISS_KEY) || "0", 10);
      if (!at) return false;
      return Date.now() - at < DISMISS_DAYS * 24 * 60 * 60 * 1000;
    } catch (_) {
      return false;
    }
  }
  function markDismissed() {
    try { localStorage.setItem(DISMISS_KEY, String(Date.now())); } catch (_) {}
  }

  /* ---------------------------- Critical flows ------------------------------ */
  function beginCriticalFlow() { criticalFlows += 1; }
  function endCriticalFlow() { criticalFlows = Math.max(0, criticalFlows - 1); }
  function hasDirtyForm() {
    var forms = document.querySelectorAll("form");
    for (var i = 0; i < forms.length; i++) {
      var fields = forms[i].querySelectorAll("input, select, textarea");
      for (var j = 0; j < fields.length; j++) {
        var f = fields[j];
        if (f.type === "hidden" || f.disabled) continue;
        if (f.type === "checkbox" || f.type === "radio") {
          if (f.checked !== f.defaultChecked) return true;
        } else if (f.value && f.value !== f.defaultValue) {
          return true;
        }
      }
    }
    return false;
  }
  var SENSITIVE_PATH = /(booking|payment|checkout|check-in|check-out|cancellation|review)/i;
  function isSafeToUpdateNow() {
    if (criticalFlows > 0) return false;
    if (SENSITIVE_PATH.test(location.pathname)) return false;
    if (document.visibilityState !== "visible") return false;
    return !hasDirtyForm();
  }

  /* ------------------------------ SW updates -------------------------------- */
  function applyUpdate() {
    if (!waitingWorker || reloading) return;
    waitingWorker.postMessage({ type: "SKIP_WAITING" });
  }

  /* User-driven "Refresh now": guarantee the reload lands on the NEW release.
     1. If a new worker is already WAITING, activate it; the controllerchange
        listener below performs the reload, so the fresh worker (and its fresh
        versioned caches) serve the very next paint.
     2. Otherwise ask the registration to check for one quickly, then reload
        regardless — the ?v=-stamped assets and versioned SW caches take over.
     Never blocks longer than ~2.5s; storage/session are never touched. */
  function refreshToLatest(reload) {
    var fallback = setTimeout(function () {
      fallback = null;
      if (!reloading) { reloading = true; reload(); }
    }, 2500);
    try {
      var waiting = waitingWorker || (registration && registration.waiting);
      if (waiting) {
        // controllerchange fires once the new worker takes control and the
        // registered listener reloads; the timeout above is only a safety net
        // (e.g. the browser refused activation).
        waiting.postMessage({ type: "SKIP_WAITING" });
        return;
      }
      if (registration && registration.update) {
        registration.update().catch(function () {}).then(function () {
          var w = registration.waiting;
          if (w) { w.postMessage({ type: "SKIP_WAITING" }); return; }
          if (fallback) { clearTimeout(fallback); fallback = null; }
          if (!reloading) { reloading = true; reload(); }
        });
        return;
      }
    } catch (_) { /* fall through to the timeout */ }
    if (fallback) { clearTimeout(fallback); fallback = null; }
    if (!reloading) { reloading = true; reload(); }
  }

  var updateWatch = null;
  function offerUpdate(worker) {
    waitingWorker = worker;
    log("update available");

    // Auto-apply strategy: a newly deployed version should take over on its own
    // so users always run the latest code without manually clearing caches or
    // waiting. We only ever swap when it is SAFE (never mid-booking/payment or
    // over a dirty form). If it is not safe right now, poll quietly until it is.
    var tryAutoApply = function () {
      if (reloading || !waitingWorker) return true;   // done
      if (isSafeToUpdateNow()) {
        applyUpdate();                                 // triggers controllerchange -> reload
        return true;
      }
      return false;                                    // not safe yet; keep watching
    };
    if (!tryAutoApply()) {
      if (updateWatch) clearInterval(updateWatch);
      updateWatch = setInterval(function () {
        if (tryAutoApply()) { clearInterval(updateWatch); updateWatch = null; }
      }, 3000);
    }

    // Silent path: if the page is clearly idle, swap on the next navigation.
    // Otherwise surface a quiet, dismissible prompt using the existing toast.
    var announce = function () {
      if (!isSafeToUpdateNow()) return;
      if (window.JONE && window.JONE.ui && window.JONE.ui.toast) {
        var t = window.JONE.ui.toast(
          "A new version of the site is ready. Refresh to update.",
          "info",
          { duration: 0 }
        );
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn btn-sm btn-outline";
        btn.textContent = "Refresh";
        btn.style.marginLeft = "0.5rem";
        btn.addEventListener("click", function () {
          t.dismiss();
          applyUpdate();
        });
        t.node.appendChild(btn);
      }
    };
    // Give the page a moment to settle before interrupting anything.
    setTimeout(announce, 4000);
  }

  function watchInstalling(reg) {
    var installing = reg.installing;
    if (!installing) return;
    installing.addEventListener("statechange", function () {
      if (installing.state === "installed" && navigator.serviceWorker.controller) {
        offerUpdate(installing);
      }
    });
  }

  /* ---------------------------- Registration -------------------------------- */
  function register() {
    if (!isSupported()) { log("service workers not supported"); return; }
    if (!isSecureOrigin()) { log("insecure origin — registration skipped"); return; }
    if (location.protocol === "file:") return;

    navigator.serviceWorker
      .register(SW_URL, { scope: SW_SCOPE })
      .then(function (reg) {
        registration = reg;
        log("registered, scope:", reg.scope);

        if (reg.waiting && navigator.serviceWorker.controller) offerUpdate(reg.waiting);
        watchInstalling(reg);
        reg.addEventListener("updatefound", function () { watchInstalling(reg); });

        // Look for a new deploy when the app is brought back to the foreground.
        document.addEventListener("visibilitychange", function () {
          if (document.visibilityState === "visible") reg.update().catch(function () {});
        });
      })
      .catch(function (err) {
        // Never fatal — the site is fully functional without a service worker.
        log("registration failed:", err && err.name);
      });

    navigator.serviceWorker.addEventListener("controllerchange", function () {
      if (reloading) return;
      reloading = true;
      window.location.reload();
    });
  }

  /* ------------------------------ Install UI -------------------------------- */
  function icon(name, size) {
    return window.JONE && window.JONE.icons
      ? window.JONE.icons.get(name).replace('width="24" height="24"', 'width="' + size + '" height="' + size + '"')
      : "";
  }

  function iosInstructions() {
    if (!window.JONE || !window.JONE.ui || !window.JONE.ui.modal) return;
    window.JONE.ui.modal.open({
      title: "Add J-ONE Hotel to your Home Screen",
      body:
        '<ol style="margin:0;padding-left:1.25rem;line-height:1.7;">' +
        "<li>Tap the <strong>Share</strong> button in Safari&rsquo;s toolbar.</li>" +
        "<li>Scroll down and choose <strong>Add to Home Screen</strong>.</li>" +
        "<li>Tap <strong>Add</strong> &mdash; J-ONE Hotel &amp; Lodge will open like an app.</li>" +
        "</ol>" +
        '<p style="margin-top:1rem;color:var(--color-text-muted);">Booking and payment still require an internet connection.</p>'
    });
  }

  /* All install controls carry [data-pwa-install]; they are hidden until an
     install is actually possible, so nothing dead ever shows in the UI. */
  function installControls() {
    return document.querySelectorAll("[data-pwa-install]");
  }

  function canInstall() {
    if (isStandalone()) return false;
    if (deferredPrompt) return true;
    return isIOSSafari();               // iOS: manual Add to Home Screen
  }

  function syncControls() {
    var show = canInstall();
    installControls().forEach(function (el) {
      el.hidden = !show;
      el.setAttribute("aria-hidden", show ? "false" : "true");
    });
    if (!show) hideBanner();
  }

  function doInstall(trigger) {
    if (deferredPrompt) {
      var prompt = deferredPrompt;
      deferredPrompt = null;
      prompt.prompt();
      prompt.userChoice
        .then(function (choice) {
          log("install choice:", choice && choice.outcome);
          if (choice && choice.outcome === "dismissed") markDismissed();
          syncControls();
        })
        .catch(function () { syncControls(); });
      return;
    }
    if (isIOSSafari()) { iosInstructions(); return; }
    if (trigger && window.JONE && window.JONE.ui && window.JONE.ui.toast) {
      window.JONE.ui.toast(
        "Use your browser menu and choose \u201cInstall app\u201d or \u201cAdd to Home Screen\u201d.",
        "info"
      );
    }
  }

  /* Injects the branded install action into the existing mobile drawer and
     the footer — no new navigation, no redesign. */
  function injectControls() {
    if (isStandalone()) return;

    var drawerRow = document.querySelector(".mobile-drawer .mobile-footer-row");
    if (drawerRow && !drawerRow.querySelector("[data-pwa-install]")) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "btn btn-outline";
      b.setAttribute("data-pwa-install", "");
      b.hidden = true;
      b.innerHTML = '<span aria-hidden="true">' + icon("download", 18) + "</span> Install J-ONE Hotel App";
      drawerRow.appendChild(b);
    }

    var footerBottom = document.querySelector(".site-footer .footer-bottom");
    if (footerBottom && !footerBottom.querySelector("[data-pwa-install]")) {
      var link = document.createElement("button");
      link.type = "button";
      link.className = "pwa-install-link";
      link.setAttribute("data-pwa-install", "");
      link.hidden = true;
      link.innerHTML = '<span aria-hidden="true">' + icon("download", 16) + "</span> Install J-ONE Hotel App";
      footerBottom.appendChild(link);
    }

    document.addEventListener("click", function (e) {
      var t = e.target.closest && e.target.closest("[data-pwa-install]");
      if (!t) return;
      e.preventDefault();
      doInstall(t);
    });
  }

  /* -------------------------- Dismissible banner ---------------------------- */
  var bannerEl = null;
  function hideBanner() {
    if (bannerEl) { bannerEl.remove(); bannerEl = null; }
  }
  function showBanner() {
    if (bannerEl || isStandalone() || recentlyDismissed() || !canInstall()) return;
    // Only on the public homepage — never on booking, payment or the dashboard.
    var p = location.pathname;
    var onHome = p === "/" || /\/index\.html$/.test(p);
    if (!onHome || p.indexOf("/dashboard/") === 0) return;

    bannerEl = document.createElement("div");
    bannerEl.className = "pwa-banner";
    bannerEl.setAttribute("role", "region");
    bannerEl.setAttribute("aria-label", "Install the J-ONE Hotel app");
    bannerEl.innerHTML =
      '<span class="pwa-banner-mark" aria-hidden="true">' +
        '<img src="/favicon/icon-192.png" alt="" width="32" height="32">' +
      "</span>" +
      '<span class="pwa-banner-copy">' +
        "<strong>Install J-ONE Hotel App</strong>" +
        "<span>Faster access to rooms, offers and your stays.</span>" +
      "</span>" +
      '<button type="button" class="btn btn-sm btn-accent" data-pwa-install>Install</button>' +
      '<button type="button" class="btn-icon btn-ghost pwa-banner-close" aria-label="Dismiss install prompt">' +
        icon("x", 18) +
      "</button>";
    bannerEl.querySelector(".pwa-banner-close").addEventListener("click", function () {
      markDismissed();
      hideBanner();
    });
    document.body.appendChild(bannerEl);
  }

  /* ------------------------------ Connectivity ------------------------------ */
  /* navigator.onLine is a HINT, never a guarantee: it can report "online" on a
     captive portal, and some browsers report a stale value on a document
     restored from the back/forward or service-worker cache. So it is used only
     to fail FAST and EARLY with a clear message. The authoritative guard is
     always js/api.js — every booking/availability/payment call goes to the
     network (the service worker never answers /api/), so an unreachable
     backend surfaces as a real error and never as a cached success. */
  var onlineHint = navigator.onLine !== false;
  window.addEventListener("online", function () { onlineHint = true; });
  window.addEventListener("offline", function () { onlineHint = false; });
  // Re-sync when a restored document becomes visible again.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") onlineHint = navigator.onLine !== false;
  });
  window.addEventListener("pageshow", function () { onlineHint = navigator.onLine !== false; });

  function isOnline() { return onlineHint && navigator.onLine !== false; }

  function requireOnline(context) {
    if (isOnline()) return true;
    var msg =
      context === "payment"
        ? "Payment requires an active internet connection. Please reconnect before continuing."
        : context === "booking"
          ? "An internet connection is required to complete a booking. Please reconnect and try again."
          : "You appear to be offline. Please reconnect and try again.";
    if (window.JONE && window.JONE.ui && window.JONE.ui.toast) {
      window.JONE.ui.toast(msg, "warning", { assertive: true });
    }
    return false;
  }

  /* --------------------------------- Boot ----------------------------------- */
  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();               // suppress the default mini-infobar
    deferredPrompt = e;
    syncControls();
    if (!recentlyDismissed()) setTimeout(showBanner, 6000);
  });

  window.addEventListener("appinstalled", function () {
    deferredPrompt = null;
    hideBanner();
    syncControls();
    log("installed");
  });

  function boot() {
    injectControls();
    syncControls();
    register();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  window.JONE.pwa = {
    isSupported: isSupported,
    isStandalone: isStandalone,
    isIOS: isIOS,
    isOnline: isOnline,
    requireOnline: requireOnline,
    beginCriticalFlow: beginCriticalFlow,
    endCriticalFlow: endCriticalFlow,
    isCriticalFlowActive: function () { return criticalFlows > 0; },
    promptInstall: doInstall,
    applyUpdate: applyUpdate,
    refreshToLatest: refreshToLatest,
    get registration() { return registration; }
  };
})();
