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
  /* Build stamp. It is rendered into the panel (data-jone-reader-agent-version)
     so "is the live page running this build?" is answerable from the screen
     itself, without devtools and without trusting a repository listing. */
  var VERSION = "1.1.38";

  var ROUTES = {
    sessions: "/api/admin/staff-operations/reader-agent/sessions/",
    agents: "/api/admin/staff-operations/reader-agent/agents/",
    setupStatus: "/api/admin/staff-operations/reader-agent/agents/setup-status/",
    provision: "/api/admin/staff-operations/reader-agent/agents/provision/",
    templates: "/api/admin/staff-operations/reader-agent/templates/"
  };

  var FINGER_POSITIONS = [
    { value: "RIGHT_INDEX", label: "Right index finger" },
    { value: "RIGHT_THUMB", label: "Right thumb" },
    { value: "RIGHT_MIDDLE", label: "Right middle finger" },
    { value: "RIGHT_RING", label: "Right ring finger" },
    { value: "RIGHT_LITTLE", label: "Right little finger" },
    { value: "LEFT_INDEX", label: "Left index finger" },
    { value: "LEFT_THUMB", label: "Left thumb" },
    { value: "LEFT_MIDDLE", label: "Left middle finger" },
    { value: "LEFT_RING", label: "Left ring finger" },
    { value: "LEFT_LITTLE", label: "Left little finger" }
  ];

  var ACTIONS = {
    CLOCK_IN: { label: "Clock in with fingerprint", verb: "clock in" },
    CLOCK_OUT: { label: "Clock out with fingerprint", verb: "clock out" },
    BREAK_START: { label: "Start break with fingerprint", verb: "start a break" },
    BREAK_END: { label: "End break with fingerprint", verb: "end the break" },
    ISSUE_CODE: { label: "Issue 6-digit code", verb: "issue a workstation code" },
    VERIFY: { label: "Verify staff status", verb: "verify their status" }
  };

  /* One sentence per state, written for a receptionist rather than a developer. */
  var STATE_TEXT = {
    not_provisioned: {
      badge: "Reader not set up",
      detail: "This computer has not been connected to the fingerprint reader yet. Press “Set up fingerprint reader on this computer” once."
    },
    sdk_unavailable: {
      badge: "Reader software missing",
      detail: "The DigitalPersona reader software (U.are.U SDK) is not installed on this computer or does not match Python bitness. See the fingerprint setup guide."
    },
    driver_mode_mismatch: {
      badge: "Driver mode incompatible",
      detail: "The reader is plugged in but Windows is using the Windows Hello (WBF) driver, which the reader software cannot use. Switch to the DigitalPersona 4500 legacy driver."
    },
    service_unavailable: {
      badge: "Reader service stopped",
      detail: "The DigitalPersona Authentication Service (DpHost) is not running on this computer. Start it from Windows Services (services.msc)."
    },
    not_connected: {
      badge: "Reader not connected",
      detail: "No fingerprint reader is detected. Plug the DigitalPersona 4500 firmly into a USB port on this computer."
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
      detail: "The reader agent is not running on this computer. Press “Start reader agent” — Windows starts it automatically."
    },
    offline: {
      badge: "No internet connection",
      detail: "This computer cannot reach the J-ONE server, so a fingerprint cannot be verified. Attendance is never recorded offline."
    },
    setup_forbidden: {
      badge: "Reader setup needs a manager",
      detail: "This computer's reader has not been set up yet, and this account cannot set it up. An administrator, general manager or attendance manager must press “Set up fingerprint reader on this computer” on this computer once."
    },
    backend_unavailable: {
      badge: "J-ONE server unavailable",
      detail: "The J-ONE server did not answer. Nothing was recorded; try again in a moment."
    }
  };

  var ERROR_TEXT = {
    NO_MATCH: "That fingerprint did not match any enrolled staff member. Try again, or ask a supervisor to check the enrolment.",
    STAFF_NOT_ELIGIBLE: "This staff member is not eligible to record attendance right now.",
    STAFF_UNKNOWN: "This staff account could not be found.",
    STAFF_MISMATCH: "The enrolment request was for a different staff member.",
    ATTENDANCE_REFUSED: "Attendance was refused by the attendance rules (for example, a clock-in already open).",
    CODE_ISSUE_REFUSED: "A workstation code could not be issued for this staff member.",
    SESSION_EXPIRED: "No fingerprint was read in time. Press the button again.",
    SESSION_ALREADY_USED: "This request was already answered. Start the action again.",
    NO_ENROLLED_STAFF: "No staff member has an approved fingerprint yet. Enrol and approve a fingerprint first.",
    ALREADY_ENROLLED: "This staff member already has an active or pending fingerprint. Revoke it first if replacing their finger.",
    READER_NOT_CONNECTED: "No DigitalPersona 4500 reader was detected. Check the USB cable and try again.",
    DRIVER_MODE_MISMATCH: "The reader is using the Windows Hello (WBF) driver instead of the DigitalPersona legacy driver.",
    SDK_UNAVAILABLE: "The DigitalPersona U.are.U SDK is missing or does not match Python bitness on this PC.",
    SERVICE_UNAVAILABLE: "The DigitalPersona Authentication Service (DpHost) is not running on this PC.",
    CAPTURE_TIMEOUT: "Timed out waiting for a finger on the scanner. Try again.",
    CAPTURE_QUALITY: "The fingerprint read was not clear enough. Wipe the glass gently and press the finger flat.",
    READER_DISCONNECTED: "The reader was unplugged during the scan. Reconnect it and try again.",
    READER_BUSY: "The reader is busy with another scan. Wait a moment and try again.",
    ENROLL_NOT_READY: "The four scans did not combine cleanly. Keep the same finger flat on each prompt.",
    ENROLL_FAILED: "The enrolment capture failed on the reader. Try again.",
    AGENT_NOT_PROVISIONED: "This computer's reader agent is not set up yet. Press “Set up fingerprint reader on this computer”.",
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
    /* Repair (2026-11-26) — why this is an anchor and not an iframe.
       The previous version navigated a hidden iframe to ``jone-reader:…``.
       Chromium only starts an external protocol handler from a navigation the
       user actually initiated; a hidden-iframe navigation is silently dropped,
       so Windows never launched the agent, the API session stayed PENDING, and
       the receptionist was told "No fingerprint read in time" after two and a
       half minutes of holding a finger on a reader that had never been asked to
       do anything. Clicking a real anchor inside the click handler is the
       gesture-anchored path every browser honours, it leaves the dashboard
       where it is, and its failure is observable instead of silent. */
    if (!uri) return false;
    try {
      var anchor = doc.createElement("a");
      anchor.href = uri;
      anchor.style.display = "none";
      anchor.setAttribute("aria-hidden", "true");
      anchor.rel = "noopener";
      doc.body.appendChild(anchor);
      if (typeof anchor.click === "function") {
        anchor.click();
      } else {
        window.location.href = uri;
      }
      window.setTimeout(function () {
        try { anchor.parentNode && anchor.parentNode.removeChild(anchor); } catch (_) {}
      }, 2000);
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
    /* A host that was mounted explicitly (staff-profile.html) needs no markup
       attribute to work, but stamping it keeps one check usable everywhere:
       querySelectorAll("[data-jone-reader-agent-host]").length === 1, and it
       lets autoMount recognise the panel as already mounted instead of adding
       a second one. */
    if (host.getAttribute("data-jone-reader-agent-host") === null) {
      host.setAttribute("data-jone-reader-agent-host", "explicit");
    }

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
    var rekeyButton = el("button", "btn btn-sm btn-outline", "Set up this computer again (replaces its key)");
    rekeyButton.type = "button";
    rekeyButton.hidden = true;

    var busy = false;
    var sessionToken = "";
    var pollTimer = null;
    var startedAt = 0;
    var setupStatus = null;   /* last /agents/setup-status/ answer for this account */
    var currentAgent = null;  /* currently matched ReaderAgent row */
    var lastAgents = [];      /* cached list from /agents/ for workstation-aware picking */
    var codeBox = null;       /* the issued 6-digit code, when one was issued */

    var revokeButton = el("button", "btn btn-sm btn-outline", "Remove offline reader agent");
    revokeButton.type = "button";
    revokeButton.hidden = true;

    var fingerSelect = null;
    if (allowEnroll && !options.fingerPosition) {
      fingerSelect = el("select", "select");
      fingerSelect.style.maxWidth = "210px";
      fingerSelect.setAttribute("aria-label", "Finger position for enrolment");
      FINGER_POSITIONS.forEach(function (fp) {
        var opt = doc.createElement("option");
        opt.value = fp.value;
        opt.textContent = fp.label;
        fingerSelect.appendChild(opt);
      });
    }

    host.appendChild(head);
    host.appendChild(detail);
    if (fingerSelect) {
      var fingerRow = el("div", "reader-agent-finger");
      fingerRow.style.marginBottom = "0.5rem";
      var fingerLabel = el("label", "caption muted", "Finger to enrol: ");
      fingerRow.appendChild(fingerLabel);
      fingerRow.appendChild(fingerSelect);
      host.appendChild(fingerRow);
    }
    host.appendChild(actions);
    host.appendChild(setupButton);
    host.appendChild(startButton);
    host.appendChild(rekeyButton);
    host.appendChild(revokeButton);

    function say(state, override) {
      var text = STATE_TEXT[state] || STATE_TEXT.not_connected;
      badge.textContent = (override && override.badge) || text.badge;
      var badgeTone = "badge-warning";
      if (state === "ready" || state === "success") badgeTone = "badge-success";
      else if (state === "reading" || state === "waiting_finger" || state === "initializing") badgeTone = "badge-info";
      else if (state === "failed" || state === "offline" || state === "backend_unavailable" ||
               state === "driver_mode_mismatch" || state === "sdk_unavailable" ||
               state === "service_unavailable" || state === "agent_missing") badgeTone = "badge-danger";
      badge.className = "badge " + badgeTone;
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
      rekeyButton.disabled = disabled;
      revokeButton.disabled = disabled;
      if (fingerSelect) fingerSelect.disabled = disabled;
    }

    function preferredWorkstationRef() {
      var client = api();
      var fromBrowser = client && client.getWorkstationReference && client.getWorkstationReference();
      if (fromBrowser) return String(fromBrowser).trim();
      if (setupStatus && setupStatus.workstation_reference) return String(setupStatus.workstation_reference).trim();
      if (currentAgent && currentAgent.workstation_reference) return String(currentAgent.workstation_reference).trim();
      return "";
    }

    function pickAgent(agents) {
      if (!Array.isArray(agents) || !agents.length) return null;
      var targetRef = setupStatus && setupStatus.agent && setupStatus.agent.reference;
      if (targetRef) {
        var exact = agents.find(function (a) { return a && a.reference === targetRef; });
        if (exact) return exact;
      }
      var client = api();
      var wsRef = (client && client.getWorkstationReference && client.getWorkstationReference()) ||
        (setupStatus && setupStatus.workstation_reference) || "";
      if (wsRef) {
        var byWsOnline = agents.find(function (a) { return a && a.workstation_reference === wsRef && a.online; });
        if (byWsOnline) return byWsOnline;
        var byWs = agents.find(function (a) { return a && a.workstation_reference === wsRef; });
        if (byWs) return byWs;
      }
      var anyOnline = agents.find(function (a) { return a && a.online; });
      return anyOnline || agents[0] || null;
    }

    function stopPolling() {
      if (pollTimer) {
        window.clearInterval(pollTimer);
        pollTimer = null;
      }
    }

    /* ---------------------------------------------------------- agent status */
    function renderAgentState(live) {
      currentAgent = live || null;
      if (!live) {
        say("not_provisioned");
        renderSetupAffordance();
        startButton.hidden = true;
        revokeButton.hidden = true;
        return null;
      }
      if (!live.online) {
        say("agent_missing");
        renderSetupAffordance();
        startButton.hidden = false;
        revokeButton.hidden = !(setupStatus && setupStatus.can_provision && live.reference);
        return live;
      }
      startButton.hidden = true;
      revokeButton.hidden = true;
      renderSetupAffordance();
      var status = String(live.device_status || "").toLowerCase();
      if (live.driver_mode === "WBF") {
        say("driver_mode_mismatch", { badge: "Driver mode incompatible", detail: STATE_TEXT.driver_mode_mismatch.detail });
      } else if (status === "ready" || status === "connected") {
        say("ready", { badge: "Reader ready", detail: STATE_TEXT.ready.detail + (live.device_serial ? " (serial " + live.device_serial + ")" : "") });
      } else if (status === "driver_mode_mismatch") {
        say("driver_mode_mismatch");
      } else if (status === "sdk_unavailable") {
        say("sdk_unavailable", { badge: STATE_TEXT.sdk_unavailable.badge, detail: live.detail || STATE_TEXT.sdk_unavailable.detail });
      } else if (status === "service_unavailable") {
        say("service_unavailable", { badge: STATE_TEXT.service_unavailable.badge, detail: live.detail || STATE_TEXT.service_unavailable.detail });
      } else if (status === "busy") {
        say("busy");
      } else if (status === "initializing") {
        say("initializing");
      } else {
        say("not_connected", { badge: STATE_TEXT.not_connected.badge, detail: live.detail || STATE_TEXT.not_connected.detail });
      }
      return live;
    }

    async function refreshAgent(opts) {
      opts = opts || {};
      var client = api();
      if (!client) return null;
      try {
        var response = await client.get(ROUTES.agents, { cache: "no-store" });
        lastAgents = (response && response.data) || [];
        var live = pickAgent(lastAgents);
        currentAgent = live || null;
        if (opts.silent) return live;
        return renderAgentState(live);
      } catch (err) {
        if (!opts.silent) say("backend_unavailable");
        return null;
      }
    }

    /* ------------------------------------------------------- setup diagnosis
       "Only attendance management can provision a fingerprint reader agent" was
       true but useless: the operator could not tell whether the account or the
       server's role mapping was at fault. The API now answers that question
       exactly (missing capability, role, department, why), so the panel can say
       who has to press the button instead of repeating a refusal. */
    async function refreshSetupStatus() {
      var client = api();
      if (!client || !client.get) return null;
      try {
        var response = await client.get(ROUTES.setupStatus, { cache: "no-store" });
        setupStatus = (response && response.data) || null;
      } catch (err) {
        setupStatus = null;
      }
      if (lastAgents.length) {
        currentAgent = pickAgent(lastAgents);
      }
      renderSetupAffordance();
      return setupStatus;
    }

    function renderSetupAffordance() {
      if (!allowSetup) {
        setupButton.hidden = true;
        rekeyButton.hidden = true;
        revokeButton.hidden = true;
        return;
      }
      /* No answer from the API yet: show the setup control and let the server
         refuse it. Claiming "a manager must do this" without evidence would be
         the same guess the old screen made. */
      if (!setupStatus || typeof setupStatus.can_provision !== "boolean") {
        setupButton.hidden = false;
        rekeyButton.hidden = true;
        revokeButton.hidden = true;
        return;
      }
      var canProvision = !!setupStatus.can_provision;
      var hasAgent = !!(setupStatus && setupStatus.agent && setupStatus.agent.status === "ACTIVE");
      if (hasAgent) {
        /* A valid agent exists: never offer a button that would silently re-key
           it. Re-keying is a deliberate act, only for a replaced machine. */
        setupButton.hidden = true;
        rekeyButton.hidden = !canProvision;
        revokeButton.hidden = !(canProvision && currentAgent && !currentAgent.online && currentAgent.reference);
        return;
      }
      rekeyButton.hidden = true;
      revokeButton.hidden = true;
      if (canProvision) {
        setupButton.hidden = false;
        setupButton.textContent = "Set up fingerprint reader on this computer";
        setupButton.removeAttribute("title");
        return;
      }
      setupButton.hidden = true;
      var reason = (setupStatus && setupStatus.reason) || "";
      var hint = (setupStatus && setupStatus.grant_hint) ||
        "Ask an administrator, general manager or attendance manager to set this computer up.";
      if (host.getAttribute("data-state") === "ready") return;
      say("setup_forbidden", {
        badge: "Reader setup needs a manager",
        detail: hint + (reason ? " " + reason : "")
      });
    }

    /* ------------------------------------------------------------- polling */
    async function poll(token, action, wasOnline) {
      var client = api();
      var deadline = startedAt + 150000; /* the API session itself expires in 120 s */
      var ticks = 0;
      var startupCheckTick = wasOnline === false ? 10 : 6;
      pollTimer = window.setInterval(async function () {
        if (!client) return;
        ticks += 1;
        if (ticks === startupCheckTick) {
          var agent = await refreshAgent({ silent: true });
          if (!agent || agent.online !== true) {
            stopPolling();
            setDisabled(false);
            startButton.hidden = false;
            say("agent_missing", {
              badge: "Reader agent did not start",
              detail: "Windows did not start the J-ONE reader agent on this computer in time, so no fingerprint could be read. " +
                "Press “Start reader agent”; if it does not come online, install or re-run the reader agent installer on this PC once."
            });
            return;
          }
        }
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
              badge: action === "ENROLL" ? "Enrolling fingerprint" : (ticks < 3 ? "Waiting for fingerprint" : "Reading fingerprint"),
              detail: action === "ENROLL"
                ? "Place the same finger flat on the sensor four times when prompted, lifting briefly between touches."
                : "Place the staff member's finger flat on the DigitalPersona 4500 sensor and hold it still."
            });
            if (ticks % 10 === 0) refreshAgent({ silent: true });
            return;
          }
          stopPolling();
          setDisabled(false);
          if (status === "DONE") {
            var result = data.result || {};
            var who = result.staff_name || data.staff_name || "staff member";
            var extra = result.attendance_reference ? " Attendance " + result.attendance_reference + " recorded." : "";
            if (result.access_code) showCode(result.access_code, result.code_expires_at);
            var okDetail = action === "ENROLL"
              ? (who + "'s fingerprint was captured and queued for a supervisor's approval.")
              : (who + " verified for " + (ACTIONS[action] ? ACTIONS[action].verb : "attendance") + "." + extra);
            say("success", {
              badge: action === "ENROLL" ? "Enrolment captured" : "Fingerprint verified",
              detail: okDetail
            });
            refreshAgent({ silent: true });
            if (typeof options.onSuccess === "function") options.onSuccess(result, action);
          } else if (status === "FAILED") {
            say("failed", {
              badge: "Fingerprint not accepted",
              detail: ERROR_TEXT[data.error_code] || data.error_message || "The fingerprint was not accepted."
            });
            refreshAgent({ silent: true });
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

    /* The 6-digit code is the one thing the operator has to read off this
       screen, so it is rendered large and separate from the status sentence.
       It only ever arrives from the API's own DONE result. */
    function showCode(code, expiresAt) {
      if (!code) return;
      if (!codeBox) {
        codeBox = el("div", "reader-agent-code");
        codeBox.style.marginTop = "0.65rem";
        codeBox.style.padding = "0.75rem";
        codeBox.style.border = "1px dashed var(--color-primary,#373435)";
        codeBox.style.borderRadius = "8px";
        codeBox.style.textAlign = "center";
        host.appendChild(codeBox);
      }
      while (codeBox.firstChild) codeBox.removeChild(codeBox.firstChild);
      var caption = el("div", "caption muted", "6-DIGIT WORKSTATION CODE");
      var value = el("div", "reader-agent-code-value", String(code));
      value.style.fontSize = "1.6rem";
      value.style.fontWeight = "800";
      value.style.letterSpacing = "0.28em";
      codeBox.appendChild(caption);
      codeBox.appendChild(value);
      if (expiresAt) {
        codeBox.appendChild(el("p", "caption muted", "Valid until " + expiresAt + " for signing in, and expired by clock-out."));
      } else {
        codeBox.appendChild(el("p", "caption muted", "The code stays valid for this staff member until they clock out."));
      }
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
        var payload = {
          action: action,
          workstation_reference: preferredWorkstationRef()
        };
        if (currentAgent && currentAgent.reference) {
          payload.agent_reference = currentAgent.reference;
        }
        var response = await client.post(ROUTES.sessions, payload);
        var data = (response && response.data) || {};
        sessionToken = data.session_token || "";
        startedAt = Date.now();
        if (!data.agent_reference) {
          setDisabled(false);
          say("not_provisioned");
          renderSetupAffordance();
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
        poll(sessionToken, action, !!data.agent_online);
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
      var chosenFinger = fingerPosition || (fingerSelect && fingerSelect.value) || "RIGHT_INDEX";
      setDisabled(true);
      say("initializing", { badge: "Starting enrolment", detail: "Preparing the enrolment request…" });
      try {
        var payload = {
          action: "ENROLL",
          staff_id: staffId,
          finger_position: chosenFinger,
          workstation_reference: preferredWorkstationRef()
        };
        if (currentAgent && currentAgent.reference) {
          payload.agent_reference = currentAgent.reference;
        }
        var response = await client.post(ROUTES.sessions, payload);
        var data = (response && response.data) || {};
        if (!data.agent_reference) {
          setDisabled(false);
          say("not_provisioned");
          renderSetupAffordance();
          return null;
        }
        var uri = data.launch_uri || "";
        if (!/jone-reader:enroll\?/.test(uri)) {
          uri = uri.replace("jone-reader:capture?", "jone-reader:enroll?");
          if (uri.indexOf("staff_id=") === -1) uri += "&staff_id=" + encodeURIComponent(staffId);
          if (uri.indexOf("finger_position=") === -1) uri += "&finger_position=" + encodeURIComponent(chosenFinger);
        }
        launch(uri);
        startedAt = Date.now();
        say("waiting_finger", {
          badge: "Enrolling fingerprint",
          detail: "Place the same finger flat on the sensor four times when prompted, lifting briefly between touches. The enrolment then waits for a supervisor's approval."
        });
        poll(data.session_token, "ENROLL", !!data.agent_online);
        return data;
      } catch (err) {
        setDisabled(false);
        say("backend_unavailable", { badge: "Enrolment could not start", detail: (err && err.message) || "" });
        return null;
      }
    }

    function pollAgentReady(maxChecks) {
      var remaining = maxChecks || 8;
      var attempt = function () {
        refreshAgent().then(function (live) {
          if (live && live.online) {
            setDisabled(false);
            return;
          }
          remaining -= 1;
          if (remaining > 0) {
            window.setTimeout(attempt, 1000);
          } else {
            setDisabled(false);
            if (!live || !live.online) {
              say("agent_missing", {
                badge: "Reader agent did not come online",
                detail: "Windows did not respond to the reader-agent launch. Ensure the J-ONE Reader Agent installer (workstation_agent/install_reader_agent.bat) has been run once on this PC, then press “Start reader agent”."
              });
            }
          }
        });
      };
      window.setTimeout(attempt, 1200);
    }

    async function provision(options) {
      if (busy) return null;
      options = options || {};
      var client = api();
      if (!client) return null;
      setDisabled(true);
      say("initializing", { badge: "Setting up", detail: "Connecting this computer to the fingerprint reader…" });
      try {
        var response = await client.post(ROUTES.provision, {
          workstation_reference: preferredWorkstationRef(),
          rotate: !!options.rotate
        });
        var data = (response && response.data) || {};
        if (data.already_provisioned) {
          JONE.ui && JONE.ui.toast && JONE.ui.toast(
            "This computer already has a valid reader agent; nothing was changed.", "info");
          await refreshSetupStatus();
          await refreshAgent();
          setDisabled(false);
          return data;
        }
        if (data.install_uri) {
          launch(data.install_uri);
          JONE.ui && JONE.ui.toast && JONE.ui.toast(
            "This computer is being connected to the fingerprint reader. Checking agent connection…",
            "success");
        }
        await refreshSetupStatus();
        pollAgentReady(8);
        return data;
      } catch (err) {
        setDisabled(false);
        var message = (err && err.message) || "The reader could not be set up on this computer.";
        say("setup_forbidden", { badge: "Setup refused", detail: message });
        JONE.ui && JONE.ui.toast && JONE.ui.toast(message, "error");
        return null;
      }
    }

    function startAgent() {
      if (busy) return;
      var cfgBase = (window.APP_CONFIG && window.APP_CONFIG.API_BASE_URL || "").replace(/\/$/, "");
      var origin = (window.location && window.location.origin && window.location.origin !== "null") ? window.location.origin : "";
      var apiBase = cfgBase || origin;
      var uri = apiBase ? ("jone-reader:start?api=" + encodeURIComponent(apiBase)) : "jone-reader:start";
      setDisabled(true);
      say("initializing", { badge: "Starting reader agent", detail: "Asking Windows to start the J-ONE reader agent…" });
      launch(uri);
      pollAgentReady(8);
    }

    async function revokeOfflineAgent() {
      if (busy || !currentAgent || !currentAgent.reference) return;
      var client = api();
      if (!client) return;
      var ref = currentAgent.reference;
      var proceed = true;
      if (JONE.ui && typeof JONE.ui.confirm === "function") {
        proceed = false;
        JONE.ui.confirm({
          title: "Remove offline reader agent (" + ref + ")?",
          message: "Retire this offline agent registration so you can re-register this workstation cleanly. Any PC still using " + ref + " will stop working until set up again.",
          confirmText: "Remove agent",
          danger: true
        }).then(function (ok) { if (ok) doRevokeAgent(ref); });
      }
      if (proceed) doRevokeAgent(ref);
    }

    async function doRevokeAgent(ref) {
      var client = api();
      if (!client) return;
      setDisabled(true);
      try {
        await client.post(ROUTES.agents + encodeURIComponent(ref) + "/revoke/", {
          reason: "Retired from workstation panel"
        });
        JONE.ui && JONE.ui.toast && JONE.ui.toast("Offline reader agent removed. You can set up this computer again now.", "success");
        await refreshSetupStatus();
        await refreshAgent();
      } catch (err) {
        JONE.ui && JONE.ui.toast && JONE.ui.toast((err && err.message) || "Could not remove reader agent.", "error");
      } finally {
        setDisabled(false);
      }
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
        var pos = options.fingerPosition ? options.fingerPosition() : (fingerSelect ? fingerSelect.value : "RIGHT_INDEX");
        enroll(staffId, pos);
      });
      actions.appendChild(enrollButton);
    }

    setupButton.addEventListener("click", function () { provision(); });
    startButton.addEventListener("click", startAgent);
    revokeButton.addEventListener("click", revokeOfflineAgent);
    rekeyButton.addEventListener("click", function () {
      var proceed = true;
      if (JONE.ui && typeof JONE.ui.confirm === "function") {
        proceed = false;
        JONE.ui.confirm({
          title: "Replace this computer's reader key?",
          message: "Only do this if this computer was replaced or its key was lost. The reader agent on this computer must be set up again immediately, and the old key stops working.",
          confirmText: "Replace the key",
          danger: true
        }).then(function (ok) { if (ok) provision({ rotate: true }); });
      }
      if (proceed) provision({ rotate: true });
    });

    host.setAttribute("data-jone-reader-agent-version", VERSION);
    say("initializing");
    refreshAgent();
    refreshSetupStatus();

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

  /* ------------------------------------------------------------------------
     Enrolment approval & revocation queue (ReaderAgentTemplate).
     Allows supervisors / managers to inspect pending or active fingerprint
     enrolments and Approve, Reject, or Revoke them directly in the UI.
  ------------------------------------------------------------------------- */
  function clearNode(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }

  function mountQueue(host, options) {
    if (!host) return null;
    options = options || {};
    var statusFilter = options.status || "";
    var staffFilterId = options.staffId ? String(options.staffId) : "";
    clearNode(host);

    var statusLine = el("p", "caption muted", "Loading fingerprint enrolments…");
    var list = el("div", "reader-agent-queue-list");
    host.appendChild(statusLine);
    host.appendChild(list);

    async function refreshQueue() {
      var client = api();
      if (!client) return [];
      try {
        var url = ROUTES.templates + (statusFilter ? ("?status=" + encodeURIComponent(statusFilter)) : "");
        var resp = await client.get(url, { cache: "no-store" });
        var rows = (resp && resp.data) || [];
        if (staffFilterId) {
          rows = rows.filter(function (r) { return String(r.staff_id) === staffFilterId; });
        }
        clearNode(list);
        if (!rows.length) {
          statusLine.textContent = "No fingerprint enrolments found in this queue.";
          return rows;
        }
        statusLine.textContent = rows.length + " enrolment" + (rows.length === 1 ? "" : "s") + " listed.";
        rows.forEach(function (row) {
          var item = el("div", "card card-pad");
          item.style.marginTop = "0.5rem";
          item.style.display = "flex";
          item.style.flexWrap = "wrap";
          item.style.alignItems = "center";
          item.style.justifyContent = "space-between";
          item.style.gap = "0.65rem";

          var meta = el("div");
          var tone = row.status === "ACTIVE" ? "badge-success" : row.status === "PENDING" ? "badge-warning" : "badge-muted";
          var b = el("span", "badge " + tone, row.status);
          var name = el("strong", "", " " + (row.staff_name || ("Staff #" + row.staff_id)));
          var sub = el("div", "caption muted",
            String(row.finger_position || "RIGHT_INDEX").replace(/_/g, " ") +
            (row.views ? (" · " + row.views + " scans") : "") +
            (row.created_at ? (" · " + row.created_at) : ""));
          meta.appendChild(b);
          meta.appendChild(name);
          meta.appendChild(sub);

          var btns = el("div", "row-actions");
          if (row.status === "PENDING") {
            var approveBtn = el("button", "btn btn-sm", "Approve");
            approveBtn.type = "button";
            approveBtn.setAttribute("data-template-action", "approve");
            approveBtn.addEventListener("click", function () { actOnTemplate(row.id, "approve"); });
            var rejectBtn = el("button", "btn btn-sm btn-outline", "Reject");
            rejectBtn.type = "button";
            rejectBtn.setAttribute("data-template-action", "reject");
            rejectBtn.addEventListener("click", function () { actOnTemplate(row.id, "reject"); });
            btns.appendChild(approveBtn);
            btns.appendChild(rejectBtn);
          } else if (row.status === "ACTIVE") {
            var revokeBtn = el("button", "btn btn-sm btn-danger", "Revoke");
            revokeBtn.type = "button";
            revokeBtn.setAttribute("data-template-action", "revoke");
            revokeBtn.addEventListener("click", function () { actOnTemplate(row.id, "revoke"); });
            btns.appendChild(revokeBtn);
          }
          item.appendChild(meta);
          item.appendChild(btns);
          list.appendChild(item);
        });
        return rows;
      } catch (err) {
        statusLine.textContent = (err && err.message) || "Could not load fingerprint enrolments.";
        return [];
      }
    }

    async function actOnTemplate(id, act) {
      var client = api();
      if (!client) return;
      try {
        await client.post(ROUTES.templates + encodeURIComponent(id) + "/" + act + "/", {});
        JONE.ui && JONE.ui.toast && JONE.ui.toast("Fingerprint enrolment " + act + (act === "revoke" ? "d" : "ed") + ".", "success");
        await refreshQueue();
        if (typeof options.onChanged === "function") options.onChanged(act, id);
      } catch (err) {
        JONE.ui && JONE.ui.toast && JONE.ui.toast((err && err.message) || ("Could not " + act + " fingerprint enrolment."), "error");
      }
    }

    refreshQueue();
    return { refresh: refreshQueue, host: host };
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

  /* ------------------------------------------------------------------------
     Terminal glue that is NOT about the reader: the numeric-code console and
     the supervised fallback both need the desk headers, the identifier bucket
     and the "was a record created?" sentence. These helpers used to live in
     js/biometrics.js, whose other job was the loopback bridge — so loading that
     file to reach them dragged the bridge back onto the page. Re-implemented
     here without a single bridge call: nothing is fabricated, and only the
     audited supervised fallback posts to the legacy terminal endpoint (the
     server accepts that action without a probe by design and refuses every
     fingerprint action that has no verified read behind it).
  ------------------------------------------------------------------------- */
  function deskHeaders() {
    try {
      return (window.Auth && typeof window.Auth.desktopHeaders === "function")
        ? window.Auth.desktopHeaders() : {};
    } catch (_) {
      return {};
    }
  }

  function terminalEndpoint() {
    var cfg = window.APP_CONFIG || {};
    var map = cfg.API_ENDPOINTS || {};
    return map.receptionTerminal || "/api/admin/staff-operations/biometrics/reception-terminal/";
  }

  var terminal = {
    identifierPayload: function (raw) {
      var value = String(raw || "")
        .replace(/[\u200B-\u200D\uFEFF]/g, "")
        .replace(/[\u2010-\u2014\u2212]/g, "-")
        .trim();
      if (!value) return {};
      if (value.indexOf("@") !== -1) return { email: value.replace(/\s+/g, "") };
      if (/^\d+$/.test(value)) return { staff_id: Number(value) };
      return { employee_code: value };
    },
    status: async function () {
      var client = api();
      if (!client) throw new Error("The API client did not load on this page.");
      var response = await client.get(terminalEndpoint(), { headers: deskHeaders(), cache: "no-store" });
      return (response && response.data) || {};
    },
    /* Only ever used for MANUAL_FALLBACK — a supervisor's audited override,
       which the server deliberately allows without a fingerprint. */
    fallback: async function (payload) {
      var client = api();
      if (!client) throw new Error("The API client did not load on this page.");
      var response = await client.post(terminalEndpoint(), payload, { headers: deskHeaders() });
      return (response && response.data) || {};
    },
    attendanceNote: function (result) {
      var res = result || {};
      if (res.created === true || res.attendance_created === true) {
        return "Clocked in now. One attendance record is open for this staff member, and their Clock in stays disabled until they clock out.";
      }
      if (res.created === false || res.attendance_created === false) {
        return "Already clocked in. The open attendance record was reused — no second record was created — and Clock in stays disabled until they clock out.";
      }
      return "";
    }
  };

  window.JONE = window.JONE || {};
  window.JONE.readerAgent = {
    VERSION: VERSION,
    FINGER_POSITIONS: FINGER_POSITIONS,
    terminal: terminal,
    mount: mount,
    mountQueue: mountQueue,
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
