/* js/reader-agent.js
   ==========================================================================
   J-ONE HOTEL & LODGE — fingerprint reader panel (J-ONE Reader Agent).

   WHAT CHANGED AND WHY
   ====================
   The old panel reached a Python "bridge" at http://127.0.0.1:8787. That is
   forbidden on a hosted page (Chromium Local Network Access) and it required a
   separate process to be started by hand on the reception PC. This module
   never opens a socket to the reader, never calls a loopback address, and holds
   no biometric data at any point. It works like the badge reader in a bank:

     1. POST /reader-agent/sessions/  → the API mints a single-use challenge
        bound to this operator, this action and this workstation (120 s).
     2. The browser hands that challenge to Windows through the jone-reader:
        URL scheme. Windows starts the reader agent if it is not already
        running (it also starts at logon), so nobody has to launch anything.
     3. The agent reads the finger, matches it locally with the vendor engine,
        and posts a SIGNED result straight to the J-ONE API over HTTPS.
     4. This module polls the session and shows what actually happened: which
        staff member, or the exact reason it was refused.

   THE BROWSER IS NEVER TOLD "THE FINGERPRINT WAS VALID" BY ITSELF. Only the
   API can mark the session DONE, and it only does that after verifying the
   agent's HMAC signature, the challenge, the freshness window, the staff
   member's eligibility, the match threshold and the attendance state machine.

   States rendered (all of them are observed, never assumed):
     agent_missing · not_provisioned · sdk_unavailable · driver_mode_mismatch
     not_connected · initializing · ready · waiting_finger · reading
     success · failed · expired · offline · backend_unavailable
   ========================================================================== */
(function (window) {
  "use strict";

  var doc = window.document;

  /* Relative routes only — the origin comes from the existing centralised API
     client (window.API) via window.APP_CONFIG.API_BASE_URL. No host is ever
     hardcoded here. */
  var ROUTES = {
    sessions: "/api/admin/staff-operations/reader-agent/sessions/",
    agents: "/api/admin/staff-operations/reader-agent/agents/",
    provision: "/api/admin/staff-operations/reader-agent/agents/provision/",
    templates: "/api/admin/staff-operations/reader-agent/templates/"
  };

  var ACTIONS = {
    CLOCK_IN: { label: "Clock in with fingerprint", verb: "clock in" },
    CLOCK_OUT: { label: "Clock out with fingerprint", verb: "clock out" },
    BREAK_START: { label: "Start break with fingerprint", verb: "start a break" },
    BREAK_END: { label: "End break with fingerprint", verb: "end the break" }
  };

  /* One sentence per state, written for a receptionist rather than a developer. */
  var STATE_TEXT = {
    not_provisioned: {
      badge: "Reader not set up",
      detail: "This computer has not been connected to the fingerprint reader yet. Press “Set up fingerprint reader on this computer” once."
    },
    sdk_unavailable: {
      badge: "Reader software missing",
      detail: "The DigitalPersona reader software (U.are.U SDK) is not installed on this computer. See the setup note in the fingerprint guide — this is a one-time installation, not a daily step."
    },
    driver_mode_mismatch: {
      badge: "Driver mode incompatible",
      detail: "The reader is plugged in but Windows is using the Windows Hello (WBF) driver, which the reader software cannot use. Install the DigitalPersona 4500 legacy driver — one-time."
    },
    not_connected: {
      badge: "Reader not connected",
      detail: "No fingerprint reader is detected. Plug the DigitalPersona 4500 into this computer."
    },
    initializing: {
      badge: "Starting reader",
      detail: "The fingerprint reader is starting up…"
    },
    ready: {
      badge: "Reader ready",
      detail: "Place the staff member's finger flat on the DigitalPersona 4500 when asked."
    },
    busy: {
      badge: "Reading fingerprint",
      detail: "The reader is in use. Wait for the current read to finish."
    },
    agent_missing: {
      badge: "Reader agent not running",
      detail: "The reader agent is not running on this computer. Press “Start reader agent” — Windows starts it automatically, there is nothing to type."
    },
    offline: {
      badge: "No internet connection",
      detail: "This computer cannot reach the J-ONE server, so a fingerprint cannot be verified. Attendance is never recorded offline."
    },
    backend_unavailable: {
      badge: "J-ONE server unavailable",
      detail: "The J-ONE server did not answer. Nothing was recorded; try again in a moment."
    }
  };

  var ERROR_TEXT = {
    NO_MATCH: "That fingerprint did not match any enrolled staff member. Try again, or ask a supervisor to check the enrolment.",
    STAFF_NOT_ELIGIBLE: "This staff member is not eligible to record attendance right now.",
    ATTENDANCE_REFUSED: "Attendance was refused by the attendance rules (for example, a clock-in already open).",
    SESSION_EXPIRED: "No fingerprint was read in time. Press the button again.",
    SESSION_ALREADY_USED: "This request was already answered. Start the action again.",
    NO_ENROLLED_STAFF: "No staff member has an approved fingerprint yet.",
    ALREADY_ENROLLED: "This staff member already has an enrolled fingerprint.",
    AGENT_UNKNOWN: "This computer's reader agent is not recognised by the server any more. Set it up again.",
    AGENT_SIGNATURE_INVALID: "This computer's reader agent key is not valid. Set it up again.",
    AGENT_TIMESTAMP_STALE: "This computer's clock is too far from the server time. Correct the Windows date/time and retry.",
    NETWORK_UNAVAILABLE: "No internet connection, so the fingerprint could not be verified. Nothing was recorded."
  };

  function el(tag, className, text) {
    var node = doc.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function api() {
    return window.API || null;
  }

  /* --------------------------------------------------------------------------
     The jone-reader: hand-off.
     A hidden iframe is used because it does not navigate the dashboard away.
     If the browser refuses iframes for external protocols, the location is used
     as a fallback; both paths only ever hand over the one-time session token.
  -------------------------------------------------------------------------- */
  function launch(uri) {
    if (!uri) return false;
    try {
      var frame = doc.createElement("iframe");
      frame.style.display = "none";
      frame.setAttribute("aria-hidden", "true");
      frame.src = uri;
      doc.body.appendChild(frame);
      window.setTimeout(function () {
        try { frame.parentNode && frame.parentNode.removeChild(frame); } catch (_) {}
      }, 8000);
      return true;
    } catch (_) {
      try {
        window.location.href = uri;
        return true;
      } catch (__) {
        return false;
      }
    }
  }

  /* ------------------------------------------------------------------ panel */
  function mount(host, options) {
    if (!host) return null;
    options = options || {};
    var scopeActions = options.actions || ["CLOCK_IN", "CLOCK_OUT"];
    var allowEnroll = !!options.allowEnroll;
    var allowSetup = options.allowSetup !== false;

    host.replaceChildren();
    host.setAttribute("data-jone-reader-agent", "mounted");

    var badge = el("span", "badge badge-muted", "Checking reader…");
    var title = el("strong", "reader-agent-title", "Fingerprint reader");
    title.style.marginLeft = "0.5rem";
    var head = el("div", "reader-agent-head");
    head.appendChild(badge);
    head.appendChild(title);

    var detail = el("p", "caption muted", "");
    detail.setAttribute("role", "status");
    detail.setAttribute("aria-live", "polite");

    var actions = el("div", "reader-agent-actions");
    var setupButton = el("button", "btn btn-sm btn-outline", "Set up fingerprint reader on this computer");
    setupButton.type = "button";
    setupButton.hidden = true;
    var startButton = el("button", "btn btn-sm btn-outline", "Start reader agent");
    startButton.type = "button";
    startButton.hidden = true;

    var busy = false;
    var sessionToken = "";
    var pollTimer = null;
    var startedAt = 0;

    host.appendChild(head);
    host.appendChild(detail);
    host.appendChild(actions);
    host.appendChild(setupButton);
    host.appendChild(startButton);

    function say(state, override) {
      var text = STATE_TEXT[state] || STATE_TEXT.not_connected;
      badge.textContent = (override && override.badge) || text.badge;
      badge.className = "badge " + (state === "ready" ? "badge" :
        state === "success" ? "badge" :
        (state === "reading" || state === "waiting_finger" || state === "initializing") ? "badge badge-muted" :
        "badge badge-warning");
      detail.textContent = (override && override.detail) || text.detail;
      host.setAttribute("data-state", state);
    }

    function setDisabled(disabled) {
      busy = disabled;
      Array.prototype.forEach.call(actions.querySelectorAll("button"), function (button) {
        button.disabled = disabled;
        button.setAttribute("aria-busy", disabled ? "true" : "false");
      });
      setupButton.disabled = disabled;
      startButton.disabled = disabled;
    }

    function stopPolling() {
      if (pollTimer) {
        window.clearInterval(pollTimer);
        pollTimer = null;
      }
    }

    /* ---------------------------------------------------------- agent status */
    async function refreshAgent() {
      var client = api();
      if (!client) return null;
      try {
        var response = await client.get(ROUTES.agents, { cache: "no-store" });
        var agents = (response && response.data) || [];
        var live = agents[0] || null;
        if (!live) {
          say("not_provisioned");
          setupButton.hidden = false;
          startButton.hidden = true;
          return null;
        }
        if (!live.online) {
          say("agent_missing");
          setupButton.hidden = false;
          startButton.hidden = false;
          return live;
        }
        startButton.hidden = true;
        setupButton.hidden = live.driver_mode === "LEGACY_SDK";
        var status = String(live.device_status || "").toLowerCase();
        if (live.driver_mode === "WBF") {
          say("driver_mode_mismatch", { badge: "Driver mode incompatible", detail: STATE_TEXT.driver_mode_mismatch.detail });
        } else if (status === "ready" || status === "connected") {
          say("ready", { badge: "Reader ready", detail: STATE_TEXT.ready.detail + (live.device_serial ? " (serial " + live.device_serial + ")" : "") });
        } else if (status === "driver_mode_mismatch") {
          say("driver_mode_mismatch");
        } else if (status === "sdk_unavailable") {
          say("sdk_unavailable");
        } else if (status === "busy") {
          say("busy");
        } else if (status === "initializing") {
          say("initializing");
        } else {
          say("not_connected", { badge: STATE_TEXT.not_connected.badge, detail: live.detail || STATE_TEXT.not_connected.detail });
        }
        return live;
      } catch (err) {
        say("backend_unavailable");
        return null;
      }
    }

    /* ------------------------------------------------------------- polling */
    async function poll(token, action) {
      var client = api();
      var deadline = startedAt + 150000; /* the API session itself expires in 120 s */
      var ticks = 0;
      pollTimer = window.setInterval(async function () {
        if (!client) return;
        ticks += 1;
        if (Date.now() > deadline) {
          stopPolling();
          setDisabled(false);
          say("failed", { badge: "No fingerprint read", detail: ERROR_TEXT.SESSION_EXPIRED });
          return;
        }
        try {
          var response = await client.get(ROUTES.sessions + encodeURIComponent(token) + "/", { cache: "no-store" });
          var data = (response && response.data) || {};
          var status = String(data.status || "").toUpperCase();
          if (status === "PENDING") {
            say("waiting_finger", {
              badge: ticks < 3 ? "Waiting for fingerprint" : "Reading fingerprint",
              detail: "Place the staff member's finger flat on the DigitalPersona 4500 sensor and hold it still."
            });
            if (ticks % 4 === 0) refreshAgent();
            return;
          }
          stopPolling();
          setDisabled(false);
          if (status === "DONE") {
            var result = data.result || {};
            var who = result.staff_name || data.staff_name || "staff member";
            var extra = result.attendance_reference ? " Attendance " + result.attendance_reference + " recorded." : "";
            say("success", {
              badge: "Fingerprint verified",
              detail: who + " verified for " + (ACTIONS[action] ? ACTIONS[action].verb : "attendance") + "." + extra
            });
            if (typeof options.onSuccess === "function") options.onSuccess(result, action);
          } else if (status === "FAILED") {
            say("failed", {
              badge: "Fingerprint not accepted",
              detail: ERROR_TEXT[data.error_code] || data.error_message || "The fingerprint was not accepted."
            });
            if (typeof options.onFailure === "function") options.onFailure(data);
          } else {
            say("failed", { badge: "Reader request expired", detail: ERROR_TEXT.SESSION_EXPIRED });
          }
        } catch (err) {
          /* A network failure must never look like a verification. */
          stopPolling();
          setDisabled(false);
          var offline = !window.navigator.onLine;
          say(offline ? "offline" : "backend_unavailable");
        }
      }, 900);
    }

    /* -------------------------------------------------------------- actions */
    async function startAction(action) {
      if (busy) return null;
      var client = api();
      if (!client) {
        say("backend_unavailable", { badge: "Page not ready", detail: "The API client did not load on this page." });
        return null;
      }
      setDisabled(true);
      say("initializing", { badge: "Starting", detail: "Preparing the fingerprint request…" });
      try {
        var response = await client.post(ROUTES.sessions, {
          action: action,
          workstation_reference: (client.getWorkstationReference && client.getWorkstationReference()) || ""
        });
        var data = (response && response.data) || {};
        sessionToken = data.session_token || "";
        startedAt = Date.now();
        if (!data.agent_reference) {
          setDisabled(false);
          say("not_provisioned");
          setupButton.hidden = false;
          return null;
        }
        if (!data.agent_online) {
          /* The agent is not running: Windows starts it from this URI. The
             session stays valid for its whole lifetime, so this is not a race. */
          launch(data.launch_uri);
          say("initializing", { badge: "Starting reader agent", detail: "Windows is starting the fingerprint reader agent…" });
        } else {
          launch(data.launch_uri);
          say("waiting_finger", {
            badge: "Waiting for fingerprint",
            detail: "Place the staff member's finger flat on the DigitalPersona 4500 sensor and hold it still."
          });
        }
        poll(sessionToken, action);
        return data;
      } catch (err) {
        setDisabled(false);
        var offline = !window.navigator.onLine;
        var message = (err && err.message) || "";
        say(offline ? "offline" : "backend_unavailable", offline ? null : {
          badge: "Fingerprint request refused",
          detail: message || STATE_TEXT.backend_unavailable.detail
        });
        return null;
      }
    }

    async function enroll(staffId, fingerPosition) {
      if (busy || !staffId) return null;
      var client = api();
      if (!client) return null;
      setDisabled(true);
      say("initializing", { badge: "Starting enrolment", detail: "Preparing the enrolment request…" });
      try {
        var response = await client.post(ROUTES.sessions, {
          action: "ENROLL",
          staff_id: staffId,
          finger_position: fingerPosition || "RIGHT_INDEX",
          workstation_reference: (client.getWorkstationReference && client.getWorkstationReference()) || ""
        });
        var data = (response && response.data) || {};
        var uri = (data.launch_uri || "") + "&staff_id=" + encodeURIComponent(staffId) +
          "&finger_position=" + encodeURIComponent(fingerPosition || "RIGHT_INDEX");
        /* The agent needs the enrolment verb, not the capture verb. */
        uri = uri.replace("jone-reader:capture?", "jone-reader:enroll?");
        launch(uri);
        startedAt = Date.now();
        say("waiting_finger", {
          badge: "Enrolling fingerprint",
          detail: "Place the same finger on the sensor three times when asked. The enrolment then waits for a supervisor's approval."
        });
        poll(data.session_token, "ENROLL");
        return data;
      } catch (err) {
        setDisabled(false);
        say("backend_unavailable", { badge: "Enrolment could not start", detail: (err && err.message) || "" });
        return null;
      }
    }

    async function provision() {
      if (busy) return null;
      var client = api();
      if (!client) return null;
      setDisabled(true);
      say("initializing", { badge: "Setting up", detail: "Connecting this computer to the fingerprint reader…" });
      try {
        var response = await client.post(ROUTES.provision, {
          workstation_reference: (client.getWorkstationReference && client.getWorkstationReference()) || ""
        });
        var data = (response && response.data) || {};
        if (data.install_uri) launch(data.install_uri);
        window.setTimeout(refreshAgent, 2500);
        JONE.ui && JONE.ui.toast && JONE.ui.toast(
          "This computer is being connected to the fingerprint reader. The reader state appears here in a moment.",
          "success"
        );
        return data;
      } catch (err) {
        say("backend_unavailable", { badge: "Setup refused", detail: (err && err.message) || "" });
        return null;
      } finally {
        setDisabled(false);
      }
    }

    function startAgent() {
      /* Windows starts the agent from the jone-reader: scheme; there is no
         script, service or port for anybody to manage. */
      launch("jone-reader:start");
      window.setTimeout(refreshAgent, 2500);
    }

    /* ---------------------------------------------------------------- wiring */
    scopeActions.forEach(function (action) {
      if (!ACTIONS[action]) return;
      var button = el("button", "btn btn-sm btn-primary", ACTIONS[action].label);
      button.type = "button";
      button.setAttribute("data-reader-action", action);
      button.addEventListener("click", function () { startAction(action); });
      actions.appendChild(button);
    });

    if (allowEnroll) {
      var enrollButton = el("button", "btn btn-sm btn-outline", "Enrol a fingerprint");
      enrollButton.type = "button";
      enrollButton.addEventListener("click", function () {
        var staffId = options.enrollStaffId ? options.enrollStaffId() : null;
        if (!staffId) {
          JONE.ui && JONE.ui.toast && JONE.ui.toast("Choose the staff member first.", "warning");
          return;
        }
        enroll(staffId, options.fingerPosition ? options.fingerPosition() : "RIGHT_INDEX");
      });
      actions.appendChild(enrollButton);
    }

    setupButton.addEventListener("click", provision);
    startButton.addEventListener("click", startAgent);

    say("initializing");
    refreshAgent();

    window.addEventListener("online", function () { refreshAgent(); });

    return {
      refresh: refreshAgent,
      startAction: startAction,
      enroll: enroll,
      provision: provision,
      isBusy: function () { return busy; },
      host: host
    };
  }

  /* Mount every panel on the page, and expose the API for pages that want to
     trigger an action from their own buttons. */
  function autoMount() {
    var hosts = doc.querySelectorAll("[data-jone-reader-agent-host]");
    Array.prototype.forEach.call(hosts, function (host) {
      if (host.getAttribute("data-jone-reader-agent") === "mounted") return;
      var actions = (host.getAttribute("data-reader-actions") || "CLOCK_IN,CLOCK_OUT")
        .split(",").map(function (value) { return value.trim().toUpperCase(); });
      mount(host, {
        actions: actions,
        allowEnroll: host.getAttribute("data-allow-enroll") === "true",
        allowSetup: host.getAttribute("data-allow-setup") !== "false"
      });
    });
  }

  window.JONE = window.JONE || {};
  window.JONE.readerAgent = {
    mount: mount,
    autoMount: autoMount,
    launch: launch,
    ROUTES: ROUTES,
    STATE_TEXT: STATE_TEXT,
    ERROR_TEXT: ERROR_TEXT
  };

  if (doc.readyState === "loading") {
    doc.addEventListener("DOMContentLoaded", autoMount);
  } else {
    autoMount();
  }
})(window);
