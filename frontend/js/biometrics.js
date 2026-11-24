/* js/biometrics.js */
/* ==========================================================================
   J-ONE HOTEL & LODGE — Fingerprint biometric capture client.

   Capture contract (audited 2026-10-14):
   the browser NEVER invents a fingerprint. A probe comes from one of three
   places only:

     1. the workstation fingerprint bridge service (the supported path on
        Windows 11), reached DIRECTLY at its loopback origin — default
        http://127.0.0.1:8787 — as `GET /status`, `POST /initialize` and
        `POST /capture`. The bridge, not the hosted API, owns the USB reader.
        See workstation_bridge/fingerprint_bridge_service.py and
        docs/WINDOWS_11_FINGERPRINT_SETUP.md; or
     2. a reader SDK installed directly in the desk page, exposed as
        `window.JoneFingerprintReader.capture({fingerPosition, timeout})`; or
     3. an explicitly configured lab/demo simulation
        (`__APP_CONFIG__.BIOMETRIC_READER_MODE = "simulated"`), which is
        announced on the console and must never be enabled in production.

   With neither present the helper raises a FingerprintReaderError carrying
   an actionable message ("connect the reader / use the supervised fallback")
   instead of fabricating minutiae that would look like a verified fingerprint
   to the server. MANUAL_FALLBACK never captures at all: it exists because no
   capture was possible, and the server records it as an audited override.

   Browser Local Network Access (repaired 2026-11-20)
   ==================================================
   Chromium 138+ enforces Local Network Access: a PUBLIC page
   (https://jonehotels.com) that calls a LOOPBACK address
   (http://127.0.0.1:8787) is refused with

     Access to fetch ... blocked by CORS policy: Permission was denied for
     this request to access the `loopback` address space.

   A browser `fetch()` failure is an opaque TypeError with no status and no
   reason, so "no bridge", "firewall blocked", "Chrome blocked loopback" and
   "the operator denied the prompt" all looked identical and were reported as
   BRIDGE_UNREACHABLE "start the bridge". The mitigation, verified in Chromium:

     * the desk pages are served with
         Permissions-Policy: local-network-access=(self)
       (frontend/_headers + frontend/vercel.json + the checkout-owned
       configuration/frontend copies);
     * before the first bridge call the page asks for the `local-network-access`
       permission, still under a real user gesture (the button the operator
       pressed), so the browser shows its normal prompt;
     * the transport remembers that the operator DENIED it, and reports
       BRIDGE_PERMISSION_BLOCKED with instructions instead of "start the
       bridge" — which can never fix a permission denial.

   The file:// (offline kit) path and any browser without the permission
   descriptor behave exactly as before.

   Reader status model (returned by getReaderStatus / initializeReader):
     connected             — reader connected and ready
     initializing          — reader is initializing
     not_connected         — no reader detected
     driver_missing        — reader detected but driver missing/outdated
     sdk_unavailable       — reader+driver present but SDK missing/not running
     permission_denied     — access to reader denied
     busy                  — capture in progress
     communication_failure — reader communication failed
========================================================================== */
(function (window) {
  "use strict";

  var MIN_CAPTURE_POINTS = 10;
  var READER_MODE_SIMULATED = "simulated";
  var DEFAULT_TIMEOUT_MS = 30000;

  // Reader status values — mirror the bridge service constants.
  var STATUS_CONNECTED = "connected";
  var STATUS_INITIALIZING = "initializing";
  var STATUS_NOT_CONNECTED = "not_connected";
  var STATUS_USB_DETECTED = "usb_detected";
  var STATUS_RECONNECTING = "reconnecting";
  var STATUS_DRIVER_MISSING = "driver_missing";
  var STATUS_SDK_UNAVAILABLE = "sdk_unavailable";
  var STATUS_PERMISSION_DENIED = "permission_denied";
  var STATUS_BUSY = "busy";
  var STATUS_COMMUNICATION_FAILURE = "communication_failure";
  // Driver DLLs present and loaded, but the vendor service did not answer
  // dpfpdd_init(). The repair is to start the DigitalPersona service, NOT to
  // reinstall the driver, so it must never be reported as driver_missing.
  var STATUS_SERVICE_UNAVAILABLE = "service_unavailable";
  // Not a reader state: the BROWSER refused the page access to the loopback
  // bridge. Kept distinct so the operator is never told to "start the bridge"
  // when the bridge may well be running.
  var STATUS_PERMISSION_BLOCKED = "permission_blocked";

  var UI_LABELS = {
    not_connected: "Fingerprint reader not connected",
    usb_detected: "Reader detected",
    initializing: "Initializing fingerprint reader",
    driver_missing: "Driver/interface unavailable",
    sdk_unavailable: "Driver/interface unavailable",
    service_unavailable: "Biometric service unavailable",
    bridge_unavailable: "Biometric service unavailable",
    connected: "Fingerprint reader ready",
    busy: "Reading fingerprint",
    capturing: "Reading fingerprint",
    capture_success: "Fingerprint captured successfully",
    reconnecting: "Reconnecting to fingerprint reader",
    communication_failure: "Reconnecting to fingerprint reader",
    permission_blocked: "Browser blocked local network access",
    wrong_workstation: "Open this on the reception workstation"
  };

  var STATUS_DESCRIPTIONS = {};
  STATUS_DESCRIPTIONS[STATUS_CONNECTED] = "Fingerprint reader connected and ready.";
  STATUS_DESCRIPTIONS[STATUS_INITIALIZING] = "Initializing fingerprint reader. Please wait.";
  STATUS_DESCRIPTIONS[STATUS_NOT_CONNECTED] = "No fingerprint reader detected. Connect the DigitalPersona 4500 reader to this workstation.";
  STATUS_DESCRIPTIONS[STATUS_USB_DETECTED] = "DigitalPersona USB reader detected. Initializing driver interface.";
  STATUS_DESCRIPTIONS[STATUS_RECONNECTING] = "Reconnecting to fingerprint reader. Please wait.";
  STATUS_DESCRIPTIONS[STATUS_DRIVER_MISSING] = "Reader detected but the Windows driver is missing or outdated. Install the DigitalPersona 4500 driver.";
  STATUS_DESCRIPTIONS[STATUS_SDK_UNAVAILABLE] = "Reader and driver are present but the DigitalPersona SDK is not installed or not running.";
  STATUS_DESCRIPTIONS[STATUS_PERMISSION_DENIED] = "Permission denied to access the fingerprint reader. Run the bridge as administrator.";
  STATUS_DESCRIPTIONS[STATUS_BUSY] = "The fingerprint reader is busy. Please wait and try again.";
  STATUS_DESCRIPTIONS[STATUS_COMMUNICATION_FAILURE] = "Communication with the fingerprint reader failed. Reconnecting automatically.";
  STATUS_DESCRIPTIONS[STATUS_SERVICE_UNAVAILABLE] = "The DigitalPersona driver is installed but its Windows service is not running or did not answer, so the reader cannot be opened. Start the DigitalPersona service on this workstation and replug the reader, then retry.";
  STATUS_DESCRIPTIONS[STATUS_PERMISSION_BLOCKED] = "This browser has not granted the page permission to reach the fingerprint bridge on this computer, so the request never left the browser. Press Allow reader access (the lock icon → Local network access also works), then press Retry. The bridge itself may already be running.";

  function FingerprintReaderError(message, code) {
    var error = new Error(message);
    error.name = "FingerprintReaderError";
    if (code) error.code = code;
    return error;
  }

  function config() {
    var runtime = window.__APP_CONFIG__;
    var app = window.APP_CONFIG;
    var mode = "";
    if (app && app.BIOMETRIC_READER_MODE) mode = app.BIOMETRIC_READER_MODE;
    if (runtime && runtime.BIOMETRIC_READER_MODE) mode = runtime.BIOMETRIC_READER_MODE;
    return { mode: String(mode || "hardware").toLowerCase() };
  }

  function readerBridge() {
    var bridge = window.JoneFingerprintReader;
    return bridge && typeof bridge.capture === "function" ? bridge : null;
  }

  /* ------------------------- Local bridge transport -------------------------
     The fingerprint reader is a USB device attached to ONE Windows
     workstation, so the capture request must be answered by the bridge
     service running on that workstation (loopback), not by the hosted API.

     The previous implementation built an ABSOLUTE backend URL here and handed
     it to window.API.post(), which prefixes the API origin again. The browser
     received

       https://api.jonehotels.comhttps://api.jonehotels.com/api/.../capture/

     and refused it (malformed URL + CSP), surfacing as
     "Bridge communication error: We couldn't reach our servers…". The hosted
     API could never answer that call usefully anyway: it runs in a data centre
     with no USB reader attached.

     Everything below therefore talks to the workstation bridge directly over
     its own origin, with the same request/response contract and no secrets in
     the page (the bridge holds no credential; authorisation stays with the
     API's JWT, which is sent only to the API). */
  var DEFAULT_BRIDGE_URL = "http://127.0.0.1:8787";
  var STATUS_TIMEOUT_MS = 6000;
  var CAPTURE_GRACE_MS = 8000;

  /* The reader is USB hardware on ONE Windows workstation, and 127.0.0.1
     always means "the device running this browser". On a phone or tablet
     the loopback request goes to the phone itself, so the honest answer is
     not "start the bridge" (there is nowhere to start it) but "open this
     page on the reception PC the reader is plugged into".

     Detection is POSITIVE evidence only — a match on a mobile/tablet platform
     token. The previous test also matched the bare word "Mobile", which
     appears in the compatibility string of some Windows browsers; a reception
     PC misdetected as "mobile" would never attempt a capture at all. Always
     return to the user agent guess (with retries) before changing this: a
     false positive disables fingerprint attendance, which is far worse than a
     false negative (a false negative only produces the ordinary
     BRIDGE_UNREACHABLE guidance).

     Everything else — including a Windows PC where the bridge is not running,
     the reader is unplugged, the driver is missing or the browser blocked
     loopback — is classified from real evidence in async describeReader(). */
  function isMobileBrowserContext() {
    var ua = (window.navigator && window.navigator.userAgent) || "";
    return /Android|webOS|iPhone|iPad|iPod|Windows Phone|BlackBerry|Opera Mini|IEMobile/i.test(ua) ||
      /Mobile\s*Safari/i.test(ua);
  }

  /* Session-scoped operator choice for the workstation mode (see
     workstationMode()): "desktop" shows the reader controls on a device that
     positively identified itself as not-a-desktop (a Windows tablet in
     desktop mode, for example). sessionStorage only — a choice made at the desk
     must not follow the account to another device. */
  var WORKSTATION_OVERRIDE_KEY = "jone.fingerprint.workstation";

  function readOverride() {
    try {
      var raw = window.sessionStorage && window.sessionStorage.getItem(WORKSTATION_OVERRIDE_KEY);
      return raw === "desktop" || raw === "mobile" ? raw : "";
    } catch (_) { return ""; }
  }

  function setWorkstationOverride(value) {
    try {
      if (!window.sessionStorage) return;
      if (value === "desktop" || value === "mobile") {
        window.sessionStorage.setItem(WORKSTATION_OVERRIDE_KEY, value);
      } else {
        window.sessionStorage.removeItem(WORKSTATION_OVERRIDE_KEY);
      }
    } catch (_) { /* private mode / disabled storage: detection still works */ }
  }

  var WRONG_WORKSTATION_MESSAGE =
    "Fingerprint attendance is available only on the designated Windows reception " +
    "workstation connected to the DigitalPersona fingerprint reader. Open this page " +
    "on that reception Windows workstation to scan fingerprints. This page is open on a mobile " +
    "device, where 127.0.0.1 is the device itself, so no reader can be reached from " +
    "here; open this page on the reception PC the reader is plugged into. A manager " +
    "or administrator can authorise a supervised fallback " +
    "meanwhile if the attendance policy permits it.";

  function bridgeBase() {
    var configured = window.APP_CONFIG && window.APP_CONFIG.FINGERPRINT_BRIDGE_URL;
    var base = typeof configured === "string" && configured.trim() ? configured.trim() : DEFAULT_BRIDGE_URL;
    return base.replace(/\/+$/, "");
  }

  function bridgeUrl(path) {
    var base = bridgeBase();
    var suffix = String(path || "");
    if (suffix && suffix.charAt(0) !== "/") suffix = "/" + suffix;
    return base + suffix;
  }

  /* ------------------------ Local Network Access ---------------------------
     Chromium 138+ refuses a public page's fetch() to a loopback address until
     the `local-network-access` permission is granted (the shipping behaviour:
     "Permission was denied for this request to access the `loopback` address
     space."). The permission prompt is only offered from a user gesture, so
     the capture click asks for it synchronously before the first capture.

     Nothing here is required on browsers without the descriptor (Firefox,
     Safari, older Chrome, the file:// offline kit): requestLocalNetworkAccess()
     then resolves to "unsupported"/"unknown" and the transport behaves
     exactly as it always did. */
  var LNA_STATE = { permission: "unknown", checked: false, read_error: false };

  function lnaSupported() {
    try {
      var permissions = window.navigator && window.navigator.permissions;
      return !!(permissions && typeof permissions.query === "function" &&
        window.isSecureContext !== false);
    } catch (_) { return false; }
  }

  /* Read the browser's Local Network Access state.

     Three genuinely different answers hide behind one rejected promise, and
     conflating them is what made the desk page tell an operator to "start the
     bridge" when nothing was wrong with the bridge:

       * the browser has no such permission           -> "unsupported"
       * the DESK PAGE'S OWN Permissions-Policy forbids reading it outside a
         user gesture (Chromium: SecurityError, "cannot be read from a document
         with a permissions policy that disallows it") -> "prompt": the operator
         has not been asked yet, and the next button press is what asks
       * anything else                                 -> "unknown"

     Only "denied" means the operator refused. "prompt" must never be rendered
     as a refusal and must never be rendered as "the bridge is not running". */
  async function localNetworkPermissionState() {
    if (!lnaSupported()) return "unsupported";
    try {
      var status = await window.navigator.permissions.query({ name: "local-network-access" });
      return (status && status.state) || "unknown";
    } catch (err) {
      if (err && String(err.name) === "SecurityError") {
        LNA_STATE.read_error = true;
        return "prompt";
      }
      // Not a browser with LNA (older Chrome/Firefox/Safari, or the file://
      // offline kit): the descriptor itself is rejected.
      return "unsupported";
    }
  }

  function localNetworkPermissionBlocked() {
    return LNA_STATE.checked && LNA_STATE.permission === "denied";
  }

  /* Ask once, under the caller's click. Returns the observed state:
       granted | prompt | denied | unsupported | unknown
     A denial is remembered for the page's lifetime so Retry cannot loop the
     operator back into the same silent failure. */
  async function requestLocalNetworkAccess() {
    if (!lnaSupported()) {
      LNA_STATE = { permission: "unsupported", checked: true, read_error: false };
      return "unsupported";
    }
    var state = await localNetworkPermissionState();
    if (state === "prompt") {
      try {
        var permissions = window.navigator.permissions;
        if (typeof permissions.request === "function") {
          var granted = await permissions.request({ name: "local-network-access" });
          state = (granted && granted.state) || "prompt";
        }
      } catch (_) {
        // No programmatic request (the browser only prompts on the fetch);
        // keep "prompt" and let the transport attempt the call.
        state = "prompt";
      }
    }
    LNA_STATE = { permission: state, checked: true, read_error: LNA_STATE.read_error };
    return state;
  }

  /* The browser reports the Local Network Access state as "prompt": it has NOT
     refused, it simply has not asked the operator yet, and a fetch with no user
     gesture is silently refused. That refusal is byte-identical to a stopped
     bridge, so reporting "start the bridge" here sends the operator to fix the
     one thing that is provably fine. Both causes are named, in likelihood
     order, with the single action that resolves the common one. */
  function permissionNotGrantedError() {
    var host = (window.location && window.location.hostname) || "this site";
    return bridgeError(
      "This browser has not yet been given permission to reach the fingerprint bridge at " +
      bridgeBase() + ". Press Allow reader access (or Retry) — the request only succeeds from a " +
      "button press, because " + host + " must be granted local network access first. " +
      "If allowing it does not clear this message, then start the J-ONE Fingerprint Bridge on this " +
      "workstation (Start → J-ONE Fingerprint Bridge) and check the reader is plugged in.",
      "BRIDGE_PERMISSION_PROMPT",
      { reachable: false, status: STATUS_PERMISSION_BLOCKED }
    );
  }

  function permissionBlockedError() {
    return bridgeError(
      "This browser blocked the page from contacting the fingerprint bridge at " + bridgeBase() + ". " +
      "The bridge itself may already be running — the block is the browser's Local Network Access " +
      "protection. Open the site information (lock icon) in the address bar, allow local network " +
      "access for " + ((window.location && window.location.hostname) || "this site") + ", " +
      "then press Retry. If no prompt appears, ask an administrator to confirm the site's " +
      "Permissions-Policy allows local-network-access, or allow the origin in chrome://settings " +
      "→ Privacy and security → Local network access.",
      "BRIDGE_PERMISSION_BLOCKED",
      { reachable: false, status: STATUS_PERMISSION_BLOCKED }
    );
  }

  function bridgeError(message, code, extra) {
    var error = FingerprintReaderError(message, code);
    if (extra) {
      error.bridgeStatus = extra.status || "";
      error.bridgeDetail = extra.detail || "";
      error.bridgeUrl = bridgeBase();
      error.bridgeReachable = !!extra.reachable;
      error.httpStatus = extra.httpStatus || 0;
    }
    return error;
  }

  /* One bridge request: JSON in, JSON out, hard timeout, no credentials.

     Returns the parsed body. Throws a FingerprintReaderError whose `code`
     distinguishes the three genuinely different failures an operator must act
     on differently:

       BRIDGE_UNREACHABLE — nothing is listening on the bridge origin (service
                            not started, wrong port, or the request was blocked
                            by policy). Says so instead of blaming the network
                            or the hotel server.
       BRIDGE_TIMEOUT     — the bridge accepted the request but did not answer.
       BRIDGE_HTTP_<n>    — the bridge answered with an error status.
       BRIDGE_BAD_RESPONSE— the bridge answered with something unreadable. */
  async function bridgeRequest(path, options) {
    options = options || {};
    var url = bridgeUrl(path);
    var timeout = options.timeout || STATUS_TIMEOUT_MS;
    var controller = typeof AbortController !== "undefined" ? new AbortController() : null;
    var timer = setTimeout(function () { if (controller) controller.abort(); }, timeout);
    var response;
    try {
      response = await fetch(url, {
        method: options.method || "GET",
        headers: Object.assign({ "Accept": "application/json" }, options.body ? { "Content-Type": "application/json" } : {}),
        body: options.body ? JSON.stringify(options.body) : undefined,
        mode: "cors",
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
        referrerPolicy: "no-referrer",
        signal: controller ? controller.signal : undefined
      });
    } catch (err) {
      clearTimeout(timer);
      var aborted = err && (err.name === "AbortError" || err.name === "TimeoutError");
      if (aborted) {
        throw bridgeError(
          "The fingerprint bridge at " + bridgeBase() + " did not answer within " +
          Math.round(timeout / 1000) + " seconds. Check that the J-ONE Fingerprint Bridge is running and that the reader is connected.",
          "BRIDGE_TIMEOUT",
          { reachable: true }
        );
      }
      /* A refused fetch is opaque: no status, no reason. Classify from what
         IS observable — a remembered Local Network Access denial, or the
         permission state the browser reports — and only fall back to the
         "start the bridge" guidance when the browser never had to ask. */
      if (localNetworkPermissionBlocked()) throw permissionBlockedError();
      var permission = await localNetworkPermissionState();
      if (permission === "denied") {
        LNA_STATE = { permission: "denied", checked: true, read_error: false };
        throw permissionBlockedError();
      }
      /* "prompt" (and an unreadable state) means the browser never granted
         access, so it refused the request before it ever reached the port.
         Calling that a stopped bridge is a false diagnosis. */
      if (permission === "prompt" || permission === "unknown") {
        LNA_STATE = { permission: permission, checked: true, read_error: LNA_STATE.read_error };
        throw permissionNotGrantedError();
      }
      throw bridgeError(
        "The fingerprint bridge is not reachable at " + bridgeBase() + ". " +
        "Start the J-ONE Fingerprint Bridge on this workstation (Start → J-ONE Fingerprint Bridge), " +
        "then press Retry. If it is already running, confirm it is bound to 127.0.0.1 on that port, that " +
        "the Windows firewall is not blocking it, and that this browser allows local network access. " +
        "Until it is running no fingerprint can be read here — a manager or administrator can " +
        "authorise a supervised fallback instead.",
        "BRIDGE_UNREACHABLE",
        { reachable: false }
      );
    }
    clearTimeout(timer);
    var text = "";
    try { text = await response.text(); } catch (_) { text = ""; }
    var payload = null;
    if (text) { try { payload = JSON.parse(text); } catch (_) { payload = null; } }
    if (!response.ok) {
      var detail = (payload && (payload.detail || payload.message)) || ("HTTP " + response.status);
      throw bridgeError(
        "The fingerprint bridge answered with an error (" + response.status + "): " + detail,
        "BRIDGE_HTTP_" + response.status,
        { reachable: true, status: (payload && payload.status) || "", detail: detail, httpStatus: response.status }
      );
    }
    if (!payload || typeof payload !== "object") {
      throw bridgeError(
        "The fingerprint bridge returned a response this page cannot read. Restart the bridge, and report it if it continues.",
        "BRIDGE_BAD_RESPONSE",
        { reachable: true }
      );
    }
    return payload;
  }

  function uiLabelForReport(report) {
    if (!report) return UI_LABELS.bridge_unavailable;
    if (report.mode === "mobile" || report.status === "wrong_workstation") return UI_LABELS.wrong_workstation;
    if (report.status === STATUS_PERMISSION_BLOCKED) return UI_LABELS.permission_blocked;
    if (!report.bridge_reachable) return UI_LABELS.bridge_unavailable;
    if (report.ready || report.status === STATUS_CONNECTED) return UI_LABELS.connected;
    if (UI_LABELS[report.status]) return UI_LABELS[report.status];
    if (report.ui_label) return report.ui_label;
    return UI_LABELS.not_connected;
  }

  function normalizeBridgeStatus(payload) {
    var d = (payload && (payload.data && typeof payload.data === "object" ? payload.data : payload)) || {};
    var status = String(d.status || "").toLowerCase();
    if (!STATUS_DESCRIPTIONS[status]) status = STATUS_NOT_CONNECTED;
    return {
      status: status,
      ui_state: d.ui_state || status,
      ui_label: d.ui_label || UI_LABELS[status] || STATUS_DESCRIPTIONS[status],
      detail: d.detail || STATUS_DESCRIPTIONS[status],
      connected: status === STATUS_CONNECTED || status === STATUS_BUSY ? true : !!d.connected,
      ready: status === STATUS_CONNECTED,
      device_serial: d.device_serial || "",
      device_product: d.device_product || "",
      capture_in_progress: !!d.capture_in_progress,
      driver_installed: d.driver_installed === undefined ? null : !!d.driver_installed,
      driver_ready: d.driver_ready === undefined ? null : !!d.driver_ready,
      sdk_loaded: d.sdk_loaded === undefined ? null : !!d.sdk_loaded,
      usb_detected: d.usb_detected === undefined ? null : !!d.usb_detected,
      service_running: d.service_running === undefined ? null : d.service_running,
      initializing: !!d.initializing,
      reconnecting: !!d.reconnecting,
      bridge_url: bridgeBase(),
      bridge_reachable: true
    };
  }

  function unreachableStatus(err) {
    var code = (err && err.code) || "BRIDGE_UNREACHABLE";
    var status = STATUS_NOT_CONNECTED;
    if (code === "BRIDGE_TIMEOUT") status = STATUS_COMMUNICATION_FAILURE;
    else if (code === "BRIDGE_PERMISSION_BLOCKED") status = STATUS_PERMISSION_BLOCKED;
    else if (code === "BRIDGE_PERMISSION_PROMPT") status = STATUS_PERMISSION_BLOCKED;
    else if (code === "WRONG_WORKSTATION") status = "wrong_workstation";
    var uiLabel = (status === STATUS_PERMISSION_BLOCKED)
      ? UI_LABELS.permission_blocked
      : (status === "wrong_workstation" ? UI_LABELS.wrong_workstation : UI_LABELS.bridge_unavailable);
    return {
      status: status,
      ui_state: status,
      ui_label: uiLabel,
      detail: (err && err.message) || "The fingerprint bridge is not reachable on this workstation.",
      connected: false,
      ready: false,
      device_serial: "",
      capture_in_progress: false,
      driver_installed: null,
      sdk_loaded: null,
      usb_detected: null,
      bridge_url: bridgeBase(),
      bridge_reachable: false,
      bridge_error_code: code,
      local_network_permission: LNA_STATE.permission
    };
  }

  // Normalise whatever the reader SDK returns into the ISO/IEC 19794-2 point
  // list the API validates. Anything unusable is rejected here, with the reason,
  // so a malformed read can never be sent as if it were a fingerprint.
  function normalizePoints(raw) {
    if (!Array.isArray(raw) || raw.length < MIN_CAPTURE_POINTS) return null;
    var points = [];
    for (var i = 0; i < raw.length; i++) {
      var point = raw[i];
      if (!point || typeof point !== "object") return null;
      var x = Number(point.x);
      var y = Number(point.y);
      var angle = Number(point.angle);
      if (!isFinite(x) || !isFinite(y) || !isFinite(angle)) return null;
      var type = String(point.type || "ending").toLowerCase();
      if (type !== "ending" && type !== "bifurcation" && type !== "other") type = "ending";
      var quality = Number(point.quality);
      points.push({
        x: Math.round(x),
        y: Math.round(y),
        angle: ((angle % 360) + 360) % 360,
        type: type,
        quality: isFinite(quality) ? Math.max(0, Math.min(100, Math.round(quality))) : 80
      });
    }
    return points;
  }

  // Lab/demo only: derives a stable ISO minutiae list from a seed so the demo
  // terminal has something to match. Never reached unless the deployment set
  // BIOMETRIC_READER_MODE = "simulated".
  function simulateMinutiae(seed) {
    var seedStr = String(seed || "");
    var seedVal = 30;
    if (seedStr) {
      seedVal = 0;
      for (var s = 0; s < seedStr.length; s++) {
        seedVal = (seedVal * 31 + seedStr.charCodeAt(s)) % 360;
      }
    }
    var minutiae = [];
    for (var i = 0; i < 15; i++) {
      minutiae.push({
        x: 100 + i * 8 + (seedVal % 7),
        y: 120 + i * 6 + (seedVal % 5),
        angle: (seedVal + i * 20) % 360,
        type: i % 2 === 0 ? "ending" : "bifurcation",
        quality: 85 + (i % 15)
      });
    }
    return minutiae;
  }

  // Capture via the legacy `window.JoneFingerprintReader` bridge (if present).
  async function captureFromLegacyBridge(options) {
    var bridge = readerBridge();
    if (!bridge) return null;
    var capture;
    try {
      capture = await bridge.capture({
        fingerPosition: options.fingerPosition || "RIGHT_INDEX",
        timeout: options.timeout || DEFAULT_TIMEOUT_MS,
        purpose: options.purpose || "verification"
      });
    } catch (err) {
      throw FingerprintReaderError(
        "The fingerprint reader could not capture a finger: " +
        ((err && err.message) || "the reader reported an error") +
        ". Try again or use the supervised fallback.",
        "READER_CAPTURE_ERROR"
      );
    }
    if (capture && capture.cancelled) {
      throw FingerprintReaderError("The fingerprint capture was cancelled before a finger was read.", "READER_CAPTURE_CANCELLED");
    }
    var raw = capture && (capture.minutiae || capture.points || capture.template);
    var points = normalizePoints(raw);
    if (!points) {
      throw FingerprintReaderError(
        "The fingerprint reader returned an incomplete capture (" +
        (Array.isArray(raw) ? raw.length + " points" : "no template") +
        "). Clean the sensor and try again, or use the supervised fallback.",
        "READER_INCOMPLETE_CAPTURE"
      );
    }
    return points;
  }

  /* One capture attempt against the workstation bridge.

     The bridge answers
       { success: true,  minutiae: [{x, y, angle, type, quality}, …], … }
       { success: false, error: "READER_NOT_CONNECTED", message: "…" }
     and only a successful capture with enough minutiae leaves this function —
     a failure is always reported, never converted into a template. */
  async function captureOnceFromBridge(options) {
    var timeout = options.timeout || DEFAULT_TIMEOUT_MS;
    var payload = await bridgeRequest("/capture", {
      method: "POST",
      body: {
        timeout_ms: timeout,
        finger_position: options.fingerPosition || "RIGHT_INDEX"
      },
      timeout: timeout + CAPTURE_GRACE_MS
    });
    var data = (payload && payload.data && typeof payload.data === "object") ? payload.data : payload;
    if (!data || data.success === false) {
      var code = (data && (data.error || data.code)) || "BRIDGE_CAPTURE_ERROR";
      var message = (data && data.message) || "The fingerprint reader did not complete the capture.";
      throw bridgeError(message, code, { reachable: true, status: (data && data.status) || "", detail: message });
    }
    var points = normalizePoints((data && data.minutiae) || []);
    if (!points) {
      throw bridgeError(
        "The fingerprint reader returned an incomplete capture (" +
        (Array.isArray(data && data.minutiae) ? (data.minutiae.length + " minutiae points") : "no template") +
        "). Clean the sensor, place the finger flat and try again, or use the supervised fallback.",
        "BRIDGE_INCOMPLETE_CAPTURE",
        { reachable: true, status: STATUS_COMMUNICATION_FAILURE }
      );
    }
    return points;
  }

  function isRecoverableBridgeFailure(err) {
    var code = String((err && err.code) || "");
    return code === "READER_NOT_CONNECTED" ||
      code === "READER_RECONNECT_NEEDED" ||
      code === "READER_OPEN_FAILED" ||
      code === "READER_COMMUNICATION_FAILURE" ||
      code === "COMMUNICATION_FAILURE" ||
      code === "READER_BUSY" ||
      code === "BUSY" ||
      code === "BRIDGE_TIMEOUT" ||
      code === "READER_INIT_FAILED";
  }

  /* Capture via the workstation bridge, with ONE bounded recovery attempt.

     A reader that was unplugged and plugged back in, a bridge that was
     restarted after the page was already open, or a device left busy by a
     previous attempt all present as a normal capture failure. Re-initialising
     the reader and retrying exactly once recovers those cases without hiding a
     genuine "no finger on the sensor" problem or inventing a template. */
  async function captureFromHttpBridge(options) {
    if (workstationMode() === "mobile") {
      throw bridgeError(WRONG_WORKSTATION_MESSAGE, "WRONG_WORKSTATION", { reachable: false });
    }
    if (options.requestPermission !== false) {
      await requestLocalNetworkAccess();
    }
    try {
      return await captureOnceFromBridge(options);
    } catch (err) {
      if (!isRecoverableBridgeFailure(err)) throw err;
      var recovered = false;
      try {
        var status = normalizeBridgeStatus(await bridgeRequest("/initialize", { method: "POST", body: {}, timeout: 20000 }));
        recovered = !!status.connected;
      } catch (initErr) {
        throw err;
      }
      if (!recovered) throw err;
      return await captureOnceFromBridge(options);
    }
  }

  /* Probe the bridge with a POST, used only when a plain GET could not tell
     "bridge missing" from "browser refused the request".

     The capture endpoint is the ONLY bridge route the operator's browser has
     always been able to reach: Chromium answers the POST preflight itself when
     the page's Permissions-Policy allows local-network-access, so a preflight
     that comes back with `Access-Control-Allow-Origin` proves the bridge is
     running and the browser allowed the call — with no capture performed and
     nothing sent to the API. An intentionally invalid body (timeout_ms far
     below the bridge's minimum) makes the bridge answer 400 BAD_REQUEST, which
     this function maps to "reachable". A network-level failure is still a
     network-level failure: nothing is ever inferred from it. */
  async function probeBridgeViaPreflight() {
    var controller = typeof AbortController !== "undefined" ? new AbortController() : null;
    var timer = setTimeout(function () { if (controller) controller.abort(); }, STATUS_TIMEOUT_MS);
    try {
      var response = await fetch(bridgeUrl("/capture"), {
        method: "POST",
        headers: { "Accept": "application/json", "Content-Type": "application/json" },
        // timeout_ms is clamped to >= 5000 by the bridge, so this body is
        // rejected with 400 before any device access happens.
        body: JSON.stringify({ probe: true, timeout_ms: 1 }),
        mode: "cors",
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
        referrerPolicy: "no-referrer",
        signal: controller ? controller.signal : undefined
      });
      var payload = null;
      try { payload = await response.json(); } catch (_) { payload = null; }
      return { reachable: true, http_status: response.status, payload: payload };
    } catch (_) {
      return { reachable: false, http_status: 0, payload: null };
    } finally {
      clearTimeout(timer);
    }
  }

  /* Query reader status. `retries` allows the page to distinguish "the bridge
     is still starting" from "the bridge is not installed": a short bounded
     back-off (0.4s, 0.8s, 1.6s …) is used, never an unbounded poll loop. */
  async function getReaderStatus(options) {
    options = options || {};
    if (workstationMode() === "mobile") {
      return {
        status: "wrong_workstation",
        detail: WRONG_WORKSTATION_MESSAGE,
        connected: false,
        device_serial: "",
        capture_in_progress: false,
        driver_installed: null,
        sdk_loaded: null,
        bridge_url: bridgeBase(),
        bridge_reachable: false,
        bridge_error_code: "WRONG_WORKSTATION",
        local_network_permission: LNA_STATE.permission
      };
    }
    var retries = options.retries === undefined ? 1 : Math.max(0, Math.min(5, options.retries));
    var lastError = null;
    for (var attempt = 0; attempt <= retries; attempt++) {
      try {
        var status = normalizeBridgeStatus(await bridgeRequest("/status", { timeout: STATUS_TIMEOUT_MS }));
        LNA_STATE = { permission: "granted", checked: true, read_error: false };
        return status;
      } catch (err) {
        lastError = err;
        if ((err && (err.code === "BRIDGE_UNREACHABLE" || err.code === "BRIDGE_TIMEOUT")) && attempt < retries) {
          await new Promise(function (resolve) { setTimeout(resolve, 400 * Math.pow(2, attempt)); });
          continue;
        }
        break;
      }
    }
    /* A refused GET is ambiguous: "nothing is listening" and "the browser
       refused this request before it left the page" are byte-identical to
       fetch(). Before telling the operator the bridge is not running, ask the
       one bridge route the browser has always permitted (see
       probeBridgeViaPreflight). Only a genuinely reachable bridge can change
       the answer — a failed probe is reported exactly as before. */
    if (lastError && (lastError.code === "BRIDGE_UNREACHABLE" || lastError.code === "BRIDGE_TIMEOUT")) {
      var probe = await probeBridgeViaPreflight();
      if (probe.reachable) {
        var message = (LNA_STATE.permission === "denied")
          ? "The fingerprint bridge at " + bridgeBase() + " is running — the browser answered its " +
            "request — but this page has not been granted local network access, so the reader " +
            "status could not be read. Open the site information (lock icon), allow local network " +
            "access for this site, then press Check fingerprint station again."
          : "The fingerprint bridge at " + bridgeBase() + " is running (it answered a bridge " +
            "request), but this page could not read the reader status. Press Check fingerprint " +
            "station again; if it repeats, restart the J-ONE Fingerprint Bridge on this workstation.";
        return {
          status: STATUS_COMMUNICATION_FAILURE,
          detail: message,
          connected: false,
          device_serial: "",
          capture_in_progress: false,
          driver_installed: null,
          sdk_loaded: null,
          bridge_url: bridgeBase(),
          bridge_reachable: true,
          bridge_error_code: "BRIDGE_REACHABLE_STATUS_UNREADABLE",
          local_network_permission: LNA_STATE.permission
        };
      }
    }
    return unreachableStatus(lastError);
  }

  /* "mobile" | "desktop" — decided from positive evidence only. */
  function workstationMode() {
    var override = readOverride();
    if (override) return override;
    if (typeof window === "object" && window.__JONE_FORCE_WORKSTATION__ === "desktop") return "desktop";
    return isMobileBrowserContext() ? "mobile" : "desktop";
  }

  // Initialize (or re-initialize) the fingerprint reader via the bridge.
  async function initializeReader(options) {
    options = options || {};
    if (workstationMode() === "mobile") return unreachableStatus(bridgeError(WRONG_WORKSTATION_MESSAGE, "WRONG_WORKSTATION"));
    if (options.requestPermission !== false) await requestLocalNetworkAccess();
    try {
      return normalizeBridgeStatus(await bridgeRequest("/initialize", {
        method: "POST",
        body: { reopen: options.reopen !== false },
        timeout: 25000
      }));
    } catch (err) {
      return unreachableStatus(err);
    }
  }

  /* ---- One honest picture of why a capture can or cannot happen --------

     Every desk page must tell SIX genuinely different situations apart, and
     the previous single "bridge not reachable" message merged all of them:

       mobile                  the page is on a phone/tablet — 127.0.0.1 is
                               that device, so no instruction about a Windows
                               bridge can ever be acted on (WRONG_WORKSTATION)
       permission_blocked      the BROWSER refused the page loopback access
                               (Local Network Access); the bridge may be fine
       bridge_unreachable      nothing answered on the bridge origin
       bridge_timeout          the bridge accepted the request but did not reply
       reader_not_ready        the bridge answered: reader/driver/SDK missing,
                               or a capture already running
       ready                   the reader is connected and a capture can start

   ``source`` says WHERE the answer came from (bridge / browser / policy), so a
   support call can tell a USB fault from a policy fault without guessing.
   Optional ``initialize: true`` re-opens the reader once when the bridge says
   it is not ready — that is what recovers a reader that was unplugged and
   plugged back in while the page was open. Nothing is cached: every call asks
   the workstation again. */
  async function describeReader(options) {
    options = options || {};
    var mode = workstationMode();
    var result = {
      mode: mode,
      ready: false,
      status: STATUS_NOT_CONNECTED,
      ui_state: STATUS_NOT_CONNECTED,
      ui_label: UI_LABELS.not_connected,
      detail: "",
      code: "",
      source: "policy",
      bridge_reachable: false,
      reader_connected: false,
      device_serial: "",
      device_product: "",
      driver_installed: null,
      driver_ready: null,
      sdk_loaded: null,
      usb_detected: null,
      service_running: null,
      initializing: false,
      reconnecting: false,
      local_network_permission: LNA_STATE.permission,
      permission_prompt_available: lnaSupported(),
      bridge_url: bridgeBase(),
      retryable: true
    };
    if (mode === "mobile") {
      result.status = "wrong_workstation";
      result.ui_state = "wrong_workstation";
      result.ui_label = UI_LABELS.wrong_workstation;
      result.code = "WRONG_WORKSTATION";
      result.detail = WRONG_WORKSTATION_MESSAGE;
      result.retryable = false;
      return result;
    }
    if (options.requestPermission) await requestLocalNetworkAccess();
    var status = await getReaderStatus({ retries: options.retries === undefined ? 1 : options.retries });
    result.status = status.status;
    result.ui_state = status.ui_state || status.status;
    result.detail = status.detail;
    result.code = status.bridge_error_code || "";
    result.bridge_reachable = !!status.bridge_reachable;
    result.reader_connected = !!status.connected;
    result.device_serial = status.device_serial || "";
    result.device_product = status.device_product || "";
    result.driver_installed = status.driver_installed;
    result.driver_ready = status.driver_ready === undefined ? null : status.driver_ready;
    result.sdk_loaded = status.sdk_loaded;
    result.usb_detected = status.usb_detected === undefined ? null : status.usb_detected;
    result.service_running = status.service_running === undefined ? null : status.service_running;
    result.initializing = !!status.initializing;
    result.reconnecting = !!status.reconnecting;
    result.local_network_permission = LNA_STATE.permission;
    result.source = status.bridge_reachable ? "bridge" : "browser";

    if (!status.bridge_reachable) {
      if (result.code === "BRIDGE_PERMISSION_BLOCKED" || result.code === "BRIDGE_PERMISSION_PROMPT") {
        result.status = STATUS_PERMISSION_BLOCKED;
        result.ui_state = STATUS_PERMISSION_BLOCKED;
        // The grant happens on the next real capture attempt (user gesture);
        // there is nothing to retry automatically from here.
        result.retryable = true;
      } else if (result.code === "BRIDGE_TIMEOUT") {
        result.status = STATUS_COMMUNICATION_FAILURE;
        result.ui_state = STATUS_COMMUNICATION_FAILURE;
      }
      result.ui_label = uiLabelForReport(result);
      return result;
    }

    if (!status.connected && options.initialize) {
      var reinitialized = await initializeReader({ reopen: true, requestPermission: false });
      if (reinitialized.bridge_reachable) {
        result.status = reinitialized.status;
        result.ui_state = reinitialized.ui_state || reinitialized.status;
        result.detail = reinitialized.detail;
        result.reader_connected = !!reinitialized.connected;
        result.device_serial = reinitialized.device_serial || result.device_serial;
        result.device_product = reinitialized.device_product || result.device_product;
        result.driver_installed = reinitialized.driver_installed;
        result.driver_ready = reinitialized.driver_ready === undefined ? result.driver_ready : reinitialized.driver_ready;
        result.sdk_loaded = reinitialized.sdk_loaded;
        result.usb_detected = reinitialized.usb_detected === undefined ? result.usb_detected : reinitialized.usb_detected;
        result.service_running = reinitialized.service_running === undefined ? result.service_running : reinitialized.service_running;
        result.initializing = !!reinitialized.initializing;
        result.reconnecting = !!reinitialized.reconnecting;
        result.source = "bridge";
      }
    }
    result.ready = result.status === STATUS_CONNECTED;
    result.ui_label = uiLabelForReport(result);
    if (!result.ready) {
      result.code = result.code || ("READER_" + String(result.status).toUpperCase());
      result.retryable = result.status !== STATUS_PERMISSION_DENIED;
    }
    return result;
  }

  function emitCaptureState(options, state, detail) {
    var payload = {
      state: state,
      label: UI_LABELS[state] || detail || state,
      detail: detail || UI_LABELS[state] || ""
    };
    try {
      if (options && typeof options.onCaptureState === "function") {
        options.onCaptureState(payload);
      }
    } catch (_) {}
    try {
      if (typeof window.CustomEvent === "function" && typeof window.dispatchEvent === "function") {
        window.dispatchEvent(new window.CustomEvent("jone:reader-capture-state", { detail: payload }));
      }
    } catch (_) {}
  }

  function isCaptureReady(report) {
    if (config().mode === READER_MODE_SIMULATED || readerBridge()) return true;
    return !!(report && report.ready);
  }

  /* Live monitor for desk pages: keeps reader status synchronized automatically
     when USB is plugged/unplugged, driver initializes, or bridge starts —
     without requiring a manual page reload. Respects LNA policy: never fires
     unsolicited loopback fetches while permission is 'prompt' or 'denied'. */
  function startReaderMonitor(options) {
    options = options || {};
    var intervalMs = Math.max(1000, Number(options.intervalMs || 2000));
    var onStatus = typeof options.onStatus === "function" ? options.onStatus : function () {};
    var isBusyFn = typeof options.isBusy === "function" ? options.isBusy : function () { return false; };
    var stopped = false;
    var timer = null;
    var eventSource = null;
    var inFlight = false;

    function reportFromBridgeSnapshot(raw) {
      var norm = normalizeBridgeStatus(raw);
      var out = {
        mode: workstationMode(),
        ready: norm.status === STATUS_CONNECTED,
        status: norm.status,
        ui_state: norm.ui_state || norm.status,
        ui_label: norm.ui_label || UI_LABELS[norm.status] || "",
        detail: norm.detail,
        code: norm.status === STATUS_CONNECTED ? "" : ("READER_" + String(norm.status).toUpperCase()),
        source: "bridge",
        bridge_reachable: true,
        reader_connected: !!norm.connected,
        device_serial: norm.device_serial || "",
        device_product: norm.device_product || "",
        driver_installed: norm.driver_installed,
        driver_ready: norm.driver_ready,
        sdk_loaded: norm.sdk_loaded,
        usb_detected: norm.usb_detected,
        service_running: norm.service_running,
        initializing: norm.initializing,
        reconnecting: norm.reconnecting,
        local_network_permission: LNA_STATE.permission,
        permission_prompt_available: lnaSupported(),
        bridge_url: bridgeBase(),
        retryable: true
      };
      out.ui_label = uiLabelForReport(out);
      return out;
    }

    function ensureEventStream() {
      if (stopped || eventSource || typeof window.EventSource !== "function") return;
      if (workstationMode() === "mobile") return;
      if (LNA_STATE.permission !== "granted" && LNA_STATE.permission !== "unsupported") return;
      try {
        eventSource = new window.EventSource(bridgeUrl("/events"));
        eventSource.addEventListener("status", function (ev) {
          if (stopped || isBusyFn()) return;
          try {
            var parsed = JSON.parse(ev.data);
            onStatus(reportFromBridgeSnapshot(parsed));
          } catch (_) {}
        });
        eventSource.onerror = function () {
          if (eventSource) {
            try { eventSource.close(); } catch (_) {}
            eventSource = null;
          }
        };
      } catch (_) {
        eventSource = null;
      }
    }

    async function pollOnce(force) {
      if (stopped || inFlight || isBusyFn()) return null;
      if (!force && window.document && window.document.hidden) return null;
      if (workstationMode() === "mobile") {
        var mobileReport = await describeReader({ initialize: false, retries: 0 });
        onStatus(mobileReport);
        return mobileReport;
      }
      var perm = await localNetworkPermissionState();
      if (perm === "prompt" || perm === "denied") {
        LNA_STATE = { permission: perm, checked: true, read_error: LNA_STATE.read_error };
        if (!force) {
          onStatus({
            mode: workstationMode(),
            ready: false,
            status: STATUS_PERMISSION_BLOCKED,
            ui_state: STATUS_PERMISSION_BLOCKED,
            ui_label: UI_LABELS.permission_blocked,
            detail: perm === "denied" ? permissionBlockedError().message : permissionNotGrantedError().message,
            code: perm === "denied" ? "BRIDGE_PERMISSION_BLOCKED" : "BRIDGE_PERMISSION_PROMPT",
            source: "browser",
            bridge_reachable: false,
            reader_connected: false,
            device_serial: "",
            driver_installed: null,
            sdk_loaded: null,
            local_network_permission: perm,
            permission_prompt_available: lnaSupported(),
            bridge_url: bridgeBase(),
            retryable: true
          });
          return null;
        }
      }
      inFlight = true;
      try {
        var report = await describeReader({ initialize: !!options.autoInitialize, retries: 0 });
        if (!stopped) onStatus(report);
        if (report && report.bridge_reachable) ensureEventStream();
        return report;
      } finally {
        inFlight = false;
      }
    }

    function schedule() {
      if (stopped) return;
      timer = setTimeout(async function () {
        await pollOnce(false);
        schedule();
      }, intervalMs);
    }

    function onVisibilityOrFocus() {
      if (!stopped && (!window.document || !window.document.hidden)) {
        pollOnce(true);
      }
    }

    try {
      if (window.document && typeof window.document.addEventListener === "function") {
        window.document.addEventListener("visibilitychange", onVisibilityOrFocus);
      }
      if (typeof window.addEventListener === "function") {
        window.addEventListener("focus", onVisibilityOrFocus);
      }
    } catch (_) {}

    pollOnce(false).then(function () { schedule(); });

    return {
      refreshNow: function () { return pollOnce(true); },
      stop: function () {
        stopped = true;
        if (timer) clearTimeout(timer);
        if (eventSource) {
          try { eventSource.close(); } catch (_) {}
          eventSource = null;
        }
        try {
          if (window.document && typeof window.document.removeEventListener === "function") {
            window.document.removeEventListener("visibilitychange", onVisibilityOrFocus);
          }
          if (typeof window.removeEventListener === "function") {
            window.removeEventListener("focus", onVisibilityOrFocus);
          }
        } catch (_) {}
      }
    };
  }

  // Capture a minutiae probe. Tries the workstation bridge, then the lab
  // simulation (when explicitly configured), or throws an actionable error.
  async function captureMinutiaeProbe(options) {
    options = options || {};
    emitCaptureState(options, "capturing", UI_LABELS.capturing);

    // 1. A vendor SDK installed directly in the page (highest precedence: it
    //    is already talking to the attached device).
    try {
      var points = await captureFromLegacyBridge(options);
      if (points) {
        emitCaptureState(options, "capture_success", UI_LABELS.capture_success);
        return points;
      }
    } catch (legacyErr) {
      emitCaptureState(options, "capture_failed", (legacyErr && legacyErr.message) || "Fingerprint capture failed.");
      throw legacyErr;
    }

    // 2. The workstation bridge service (the supported path on Windows 11).
    var lastFailure = null;
    try {
      points = await captureFromHttpBridge(options);
      if (points) {
        emitCaptureState(options, "capture_success", UI_LABELS.capture_success);
        return points;
      }
    } catch (err) {
      /* FingerprintReaderError is a factory (it returns an Error with a name),
         so `instanceof` is always false — check the name it stamps instead, or
         a precise bridge error would be re-wrapped as a vague one. */
      lastFailure = (err && err.name === "FingerprintReaderError")
        ? err
        : bridgeError("Fingerprint capture failed: " + ((err && err.message) || String(err)), "CAPTURE_ERROR");
    }

    // 3. Lab/demo simulation (explicitly configured only).
    if (config().mode === READER_MODE_SIMULATED) {
      if (window.console && window.console.warn) {
        window.console.warn(
          "J-ONE biometrics: BIOMETRIC_READER_MODE=simulated — using a simulated minutiae template. " +
          "This is a lab/demo setting and must not be enabled in production."
        );
      }
      var sim = simulateMinutiae(options.seed);
      emitCaptureState(options, "capture_success", UI_LABELS.capture_success);
      return sim;
    }

    // No capture source produced a template. The reader error is the honest
    // answer; it names the bridge origin and what the operator must do.
    if (lastFailure) {
      emitCaptureState(options, "capture_failed", lastFailure.message);
      throw lastFailure;
    }

    var fallbackErr = FingerprintReaderError(
      "No fingerprint reader is available to this browser. Start the J-ONE Fingerprint Bridge on this " +
      "workstation (" + bridgeBase() + "), confirm the DigitalPersona 4500 is connected, and try again — " +
      "or ask a manager or administrator to authorise a supervised fallback.",
      "READER_NOT_AVAILABLE"
    );
    emitCaptureState(options, "capture_failed", fallbackErr.message);
    throw fallbackErr;
  }

  // Request fingerprint enrollment (staff profile → Enroll / Re-Enroll)
  async function requestEnrollment(fingerPosition, minutiae) {
    fingerPosition = fingerPosition || "RIGHT_INDEX";
    var probe = minutiae || (await captureMinutiaeProbe({ fingerPosition: fingerPosition, purpose: "enrollment" }));
    return window.API.post(window.APP_CONFIG.API_ENDPOINTS.fingerprintEnrollment, {
      finger_position: fingerPosition,
      minutiae: probe
    });
  }

  // Issue temporary workstation login token + 6-digit numeric access code via fingerprint scan
  async function issueTemporaryWorkstationToken(email, minutiae) {
    var probe = minutiae || (await captureMinutiaeProbe({ seed: email, purpose: "verification" }));
    var headers = (window.Auth && typeof window.Auth.desktopHeaders === "function") ? window.Auth.desktopHeaders() : {};
    var res = await window.API.post(window.APP_CONFIG.API_ENDPOINTS.temporaryCredential, {
      email: email,
      fingerprint_probe: probe
    }, { auth: false, headers: headers });
    return res.data && res.data.temporary_token;
  }

  // Dedicated Reception Attendance Workstation status & actions
  async function getReceptionTerminalStatus() {
    var headers = (window.Auth && typeof window.Auth.desktopHeaders === "function") ? window.Auth.desktopHeaders() : {};
    var res = await window.API.get(window.APP_CONFIG.API_ENDPOINTS.receptionTerminal, { headers: headers });
    return res && res.data;
  }

  /* Bucket a free-text terminal identifier into the request field the
     server resolves it from: an "@" means the account email, all digits
     means the numeric staff id, anything else is the employee code
     (STF-... or the displayed EMP-... form). The pages used to send
     every non-email value as employee_code, so a numeric staff id —
     an advertised identifier — could never be recognised. The server
     additionally cross-resolves identifiers between fields, so an older
     cached page still works; this keeps newly built pages explicit. */
  function identifierPayload(raw) {
    var value = String(raw || "")
      .replace(/[\u200B-\u200D\uFEFF]/g, "")
      .replace(/[\u2010-\u2014\u2212]/g, "-")
      .trim();
    if (!value) return {};
    if (value.indexOf("@") !== -1) return { email: value.replace(/\s+/g, "") };
    if (/^\d+$/.test(value)) return { staff_id: Number(value) };
    return { employee_code: value };
  }

  async function executeReceptionTerminalAction(payload) {
    var data = Object.assign({}, payload || {});
    // MANUAL_FALLBACK exists precisely BECAUSE no probe could be captured: the
    // supervisor path must never carry a fabricated probe, otherwise the server
    // would treat it as a verification instead of a recorded override.
    if (String(data.action || "").toUpperCase() !== "MANUAL_FALLBACK" &&
        !data.fingerprint_probe && !data.minutiae) {
      data.fingerprint_probe = await captureMinutiaeProbe({
        seed: data.email || data.employee_code || String(data.staff_id || ""),
        purpose: String(data.action || "").toLowerCase() || "verification"
      });
    }
    var headers = (window.Auth && typeof window.Auth.desktopHeaders === "function") ? window.Auth.desktopHeaders() : {};
    var res = await window.API.post(window.APP_CONFIG.API_ENDPOINTS.receptionTerminal, data, { headers: headers });
    return res && res.data;
  }

  /* Identifying a staff member at the workstation IS their clock-in: the server
     reuses the open attendance record or opens one, so every terminal action
     leaves exactly ONE open record and the staff member's own Clock in control
     stays disabled until they clock out. `created` (CLOCK_IN / ISSUE_CODE) and
     `attendance_created` (supervised fallback) say whether THIS action opened
     the record — the one honest difference between "clocked in now" and
     "already clocked in, code re-issued". An empty string means the action did
     not touch attendance (verify status, break, clock-out). */
  function attendanceNote(result) {
    var res = result || {};
    if (res.created === true || res.attendance_created === true) {
      return "Clocked in now. One attendance record is open for this staff member, and their Clock in stays disabled until they clock out.";
    }
    if (res.created === false || res.attendance_created === false) {
      return "Already clocked in. The open attendance record was reused — no second record was created — and Clock in stays disabled until they clock out.";
    }
    return "";
  }

  window.JONE = window.JONE || {};
  window.JONE.biometrics = {
    MIN_CAPTURE_POINTS: MIN_CAPTURE_POINTS,
    FingerprintReaderError: FingerprintReaderError,
    STATUS_CONNECTED: STATUS_CONNECTED,
    STATUS_INITIALIZING: STATUS_INITIALIZING,
    STATUS_NOT_CONNECTED: STATUS_NOT_CONNECTED,
    STATUS_USB_DETECTED: STATUS_USB_DETECTED,
    STATUS_RECONNECTING: STATUS_RECONNECTING,
    STATUS_DRIVER_MISSING: STATUS_DRIVER_MISSING,
    STATUS_SDK_UNAVAILABLE: STATUS_SDK_UNAVAILABLE,
    STATUS_PERMISSION_DENIED: STATUS_PERMISSION_DENIED,
    STATUS_BUSY: STATUS_BUSY,
    STATUS_COMMUNICATION_FAILURE: STATUS_COMMUNICATION_FAILURE,
    STATUS_SERVICE_UNAVAILABLE: STATUS_SERVICE_UNAVAILABLE,
    STATUS_PERMISSION_BLOCKED: STATUS_PERMISSION_BLOCKED,
    STATUS_DESCRIPTIONS: STATUS_DESCRIPTIONS,
    UI_LABELS: UI_LABELS,
    uiLabelForReport: uiLabelForReport,
    isCaptureReady: isCaptureReady,
    startReaderMonitor: startReaderMonitor,
    captureMinutiaeProbe: captureMinutiaeProbe,
    isMobileBrowserContext: isMobileBrowserContext,
    workstationMode: workstationMode,
    setWorkstationMode: setWorkstationOverride,
    permissionNotGrantedError: permissionNotGrantedError,
    describeReader: describeReader,
    requestLocalNetworkAccess: requestLocalNetworkAccess,
    localNetworkPermissionState: localNetworkPermissionState,
    getReaderStatus: getReaderStatus,
    initializeReader: initializeReader,
    getBridgeUrl: bridgeBase,
    probeBridgeViaPreflight: probeBridgeViaPreflight,
    bridgeCapturedThrough: "workstation-bridge",
    requestEnrollment: requestEnrollment,
    issueTemporaryWorkstationToken: issueTemporaryWorkstationToken,
    getReceptionTerminalStatus: getReceptionTerminalStatus,
    executeReceptionTerminalAction: executeReceptionTerminalAction,
    identifierPayload: identifierPayload,
    attendanceNote: attendanceNote
  };
})(window);
