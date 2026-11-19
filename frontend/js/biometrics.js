/* js/biometrics.js */
/* ==========================================================================
   J-ONE HOTEL & LODGE — Fingerprint biometric capture client.

   Capture contract (audited 2026-10-14):
   the browser NEVER invents a fingerprint. A probe comes from one of three
   places only:

     1. the department fingerprint reader bridge, exposed by the workstation as
        `window.JoneFingerprintReader.capture({fingerPosition, timeout})`
        (USB/HID/WebUSB reader SDK, installed by the deployment); or
     2. the local HTTP bridge API (Windows workstation bridge service at
        /api/admin/staff-operations/biometrics/bridge/*), which delegates to
        the local WebSocket bridge running on the workstation; or
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

  // Resolve the bridge API base URL from the same origin as the API.
  function bridgeApiBase() {
    var base = window.APP_CONFIG && window.APP_CONFIG.API_BASE_URL ? window.APP_CONFIG.API_BASE_URL : "";
    return base.replace(/\/$/, "") + "/api/admin/staff-operations/biometrics/bridge";
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

  // Capture via the HTTP bridge API (delegates to the local WebSocket bridge).
  async function captureFromHttpBridge(options) {
    var base = bridgeApiBase();
    var body = {
      timeout_ms: options.timeout || DEFAULT_TIMEOUT_MS,
      finger_position: options.fingerPosition || "RIGHT_INDEX"
    };
    var res = await window.API.post(base + "/capture/", body, { auth: false });
    if (!res || !res.success) {
      var msg = (res && res.data && res.data.message) || "Bridge capture failed.";
      var code = (res && res.data && res.data.error) || "BRIDGE_CAPTURE_ERROR";
      throw FingerprintReaderError(msg, code);
    }
    var data = res.data;
    if (!data.success) {
      throw FingerprintReaderError(data.message || "Capture failed.", data.error || "BRIDGE_CAPTURE_ERROR");
    }
    var points = normalizePoints(data.minutiae || []);
    if (!points) {
      throw FingerprintReaderError(
        "The bridge returned an incomplete fingerprint capture.",
        "BRIDGE_INCOMPLETE_CAPTURE"
      );
    }
    return points;
  }

  // Query the reader status from the HTTP bridge API.
  async function getReaderStatus() {
    var base = bridgeApiBase();
    var res = await window.API.get(base + "/status/", { auth: false });
    if (!res || !res.data) {
      return { status: STATUS_COMMUNICATION_FAILURE, detail: "Could not reach the fingerprint reader bridge." };
    }
    var d = res.data;
    return {
      status: d.status || STATUS_NOT_CONNECTED,
      detail: d.detail || STATUS_DESCRIPTIONS[d.status || STATUS_NOT_CONNECTED] || "",
      connected: !!d.connected,
      device_serial: d.device_serial || "",
      capture_in_progress: !!d.capture_in_progress,
    };
  }

  // Initialize (or re-initialize) the fingerprint reader via the bridge.
  async function initializeReader() {
    var base = bridgeApiBase();
    var res = await window.API.post(base + "/initialize/", {}, { auth: false });
    if (!res || !res.data) {
      return { status: STATUS_COMMUNICATION_FAILURE, detail: "Could not reach the fingerprint reader bridge." };
    }
    var d = res.data;
    return {
      status: d.status || STATUS_NOT_CONNECTED,
      detail: d.detail || STATUS_DESCRIPTIONS[d.status || STATUS_NOT_CONNECTED] || "",
      connected: !!d.connected,
      device_serial: d.device_serial || "",
      capture_in_progress: !!d.capture_in_progress,
    };
  }

  // Capture a minutiae probe. Tries the legacy bridge, then the HTTP bridge,
  // then the lab simulation (when explicitly configured), or throws.
  async function captureMinutiaeProbe(options) {
    options = options || {};

    // 1. Try the legacy in-browser bridge (window.JoneFingerprintReader).
    var points = await captureFromLegacyBridge(options);
    if (points) return points;

    // 2. Try the HTTP bridge API (local workstation bridge service).
    try {
      points = await captureFromHttpBridge(options);
      if (points) return points;
    } catch (err) {
      // Fall through — the error will be re-thrown if no other source works.
      if (!(err instanceof FingerprintReaderError)) {
        err = FingerprintReaderError("Bridge communication error: " + (err && err.message ? err.message : err), "BRIDGE_UNREACHABLE");
      }
      // Remember the bridge error to surface it if nothing else works.
      var bridgeError = err;
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

    // No capture source available. Surface the most specific error we have.
    if (bridgeError) throw bridgeError;

    throw FingerprintReaderError(
      "No fingerprint reader is available to this browser: the fingerprint capture bridge is not loaded. " +
      "A connected USB device alone is not enough; contact an administrator to install and verify the reader integration, " +
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
    requestEnrollment: requestEnrollment,
    issueTemporaryWorkstationToken: issueTemporaryWorkstationToken,
    getReceptionTerminalStatus: getReceptionTerminalStatus,
    executeReceptionTerminalAction: executeReceptionTerminalAction,
    identifierPayload: identifierPayload,
    attendanceNote: attendanceNote
  };
})(window);
