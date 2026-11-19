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
  var STATUS_DRIVER_MISSING = "driver_missing";
  var STATUS_SDK_UNAVAILABLE = "sdk_unavailable";
  var STATUS_PERMISSION_DENIED = "permission_denied";
  var STATUS_BUSY = "busy";
  var STATUS_COMMUNICATION_FAILURE = "communication_failure";

  var STATUS_DESCRIPTIONS = {};
  STATUS_DESCRIPTIONS[STATUS_CONNECTED] = "Fingerprint reader connected and ready.";
  STATUS_DESCRIPTIONS[STATUS_INITIALIZING] = "Fingerprint reader is initializing. Please wait.";
  STATUS_DESCRIPTIONS[STATUS_NOT_CONNECTED] = "No fingerprint reader detected. Connect the DigitalPersona 4500 reader to this workstation.";
  STATUS_DESCRIPTIONS[STATUS_DRIVER_MISSING] = "Reader detected but the Windows driver is missing or outdated. Install the DigitalPersona 4500 driver.";
  STATUS_DESCRIPTIONS[STATUS_SDK_UNAVAILABLE] = "Reader and driver are present but the DigitalPersona SDK is not installed or not running.";
  STATUS_DESCRIPTIONS[STATUS_PERMISSION_DENIED] = "Permission denied to access the fingerprint reader. Run the bridge as administrator.";
  STATUS_DESCRIPTIONS[STATUS_BUSY] = "The fingerprint reader is busy. Please wait and try again.";
  STATUS_DESCRIPTIONS[STATUS_COMMUNICATION_FAILURE] = "Communication with the fingerprint reader failed. Disconnect and reconnect the reader, then retry.";

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
      throw bridgeError(
        "The fingerprint bridge is not reachable at " + bridgeBase() + ". " +
        "Start the J-ONE Fingerprint Bridge on this workstation (Start → J-ONE Fingerprint Bridge), " +
        "then press Retry. If it is already running, confirm the bridge port and that the browser is " +
        "allowed to reach 127.0.0.1. Until it is running no fingerprint can be read here — a manager or " +
        "administrator can authorise a supervised fallback instead.",
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

  function normalizeBridgeStatus(payload) {
    var d = (payload && (payload.data && typeof payload.data === "object" ? payload.data : payload)) || {};
    var status = String(d.status || "").toLowerCase();
    if (!STATUS_DESCRIPTIONS[status]) status = STATUS_NOT_CONNECTED;
    return {
      status: status,
      detail: d.detail || STATUS_DESCRIPTIONS[status],
      connected: status === STATUS_CONNECTED || status === STATUS_BUSY ? true : !!d.connected,
      device_serial: d.device_serial || "",
      capture_in_progress: !!d.capture_in_progress,
      driver_installed: d.driver_installed === undefined ? null : !!d.driver_installed,
      sdk_loaded: d.sdk_loaded === undefined ? null : !!d.sdk_loaded,
      bridge_url: bridgeBase(),
      bridge_reachable: true
    };
  }

  function unreachableStatus(err) {
    var code = (err && err.code) || "BRIDGE_UNREACHABLE";
    return {
      status: code === "BRIDGE_TIMEOUT" ? STATUS_COMMUNICATION_FAILURE : STATUS_NOT_CONNECTED,
      detail: (err && err.message) || "The fingerprint bridge is not reachable on this workstation.",
      connected: false,
      device_serial: "",
      capture_in_progress: false,
      driver_installed: null,
      sdk_loaded: null,
      bridge_url: bridgeBase(),
      bridge_reachable: false,
      bridge_error_code: code
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

  /* Query reader status. `retries` allows the page to distinguish "the bridge
     is still starting" from "the bridge is not installed": a short bounded
     back-off (0.4s, 0.8s, 1.6s …) is used, never an unbounded poll loop. */
  async function getReaderStatus(options) {
    options = options || {};
    var retries = options.retries === undefined ? 1 : Math.max(0, Math.min(5, options.retries));
    var lastError = null;
    for (var attempt = 0; attempt <= retries; attempt++) {
      try {
        return normalizeBridgeStatus(await bridgeRequest("/status", { timeout: STATUS_TIMEOUT_MS }));
      } catch (err) {
        lastError = err;
        if (err && err.code === "BRIDGE_UNREACHABLE" && attempt < retries) {
          await new Promise(function (resolve) { setTimeout(resolve, 400 * Math.pow(2, attempt)); });
          continue;
        }
        break;
      }
    }
    return unreachableStatus(lastError);
  }

  // Initialize (or re-initialize) the fingerprint reader via the bridge.
  async function initializeReader(options) {
    options = options || {};
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

  // Capture a minutiae probe. Tries the workstation bridge, then the lab
  // simulation (when explicitly configured), or throws an actionable error.
  async function captureMinutiaeProbe(options) {
    options = options || {};

    // 1. A vendor SDK installed directly in the page (highest precedence: it
    //    is already talking to the attached device).
    var points = await captureFromLegacyBridge(options);
    if (points) return points;

    // 2. The workstation bridge service (the supported path on Windows 11).
    var lastFailure = null;
    try {
      points = await captureFromHttpBridge(options);
      if (points) return points;
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
      return simulateMinutiae(options.seed);
    }

    // No capture source produced a template. The reader error is the honest
    // answer; it names the bridge origin and what the operator must do.
    if (lastFailure) throw lastFailure;

    throw FingerprintReaderError(
      "No fingerprint reader is available to this browser. Start the J-ONE Fingerprint Bridge on this " +
      "workstation (" + bridgeBase() + "), confirm the DigitalPersona 4500 is connected, and try again — " +
      "or ask a manager or administrator to authorise a supervised fallback.",
      "READER_NOT_AVAILABLE"
    );
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
    STATUS_DRIVER_MISSING: STATUS_DRIVER_MISSING,
    STATUS_SDK_UNAVAILABLE: STATUS_SDK_UNAVAILABLE,
    STATUS_PERMISSION_DENIED: STATUS_PERMISSION_DENIED,
    STATUS_BUSY: STATUS_BUSY,
    STATUS_COMMUNICATION_FAILURE: STATUS_COMMUNICATION_FAILURE,
    STATUS_DESCRIPTIONS: STATUS_DESCRIPTIONS,
    captureMinutiaeProbe: captureMinutiaeProbe,
    getReaderStatus: getReaderStatus,
    initializeReader: initializeReader,
    getBridgeUrl: bridgeBase,
    bridgeCapturedThrough: "workstation-bridge",
    requestEnrollment: requestEnrollment,
    issueTemporaryWorkstationToken: issueTemporaryWorkstationToken,
    getReceptionTerminalStatus: getReceptionTerminalStatus,
    executeReceptionTerminalAction: executeReceptionTerminalAction,
    identifierPayload: identifierPayload,
    attendanceNote: attendanceNote
  };
})(window);
