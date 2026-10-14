/* js/biometrics.js */
/* ==========================================================================
   J-ONE HOTEL & LODGE — Fingerprint biometric capture client.

   Capture contract (audited 2026-10-14):
   the browser NEVER invents a fingerprint. A probe comes from one of two
   places only:

     1. the department fingerprint reader bridge, exposed by the workstation as
        `window.JoneFingerprintReader.capture({fingerPosition, timeout})`
        (USB/HID/WebUSB reader SDK, installed by the deployment — see the
        hardware notes in the audit report); or
     2. an explicitly configured lab/demo simulation
        (`__APP_CONFIG__.BIOMETRIC_READER_MODE = "simulated"`), which is
        announced on the console and must never be enabled in production.

   With neither present the helper raises a FingerprintReaderError carrying an
   actionable message ("connect the reader / use the supervised fallback")
   instead of fabricating minutiae that would look like a verified fingerprint
   to the server. MANUAL_FALLBACK never captures at all: it exists because no
   capture was possible, and the server records it as an audited override.
   ========================================================================== */
(function (window) {
  "use strict";

  var MIN_CAPTURE_POINTS = 10;
  var READER_MODE_SIMULATED = "simulated";
  var DEFAULT_TIMEOUT_MS = 30000;

  function FingerprintReaderError(message) {
    var error = new Error(message);
    error.name = "FingerprintReaderError";
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

  async function captureFromReader(options) {
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
        ((err && err.message) || "the reader reported an error") + ". Try again or use the supervised fallback."
      );
    }
    if (capture && capture.cancelled) {
      throw FingerprintReaderError("The fingerprint capture was cancelled before a finger was read.");
    }
    var raw = capture && (capture.minutiae || capture.points || capture.template);
    var points = normalizePoints(raw);
    if (!points) {
      throw FingerprintReaderError(
        "The fingerprint reader returned an incomplete capture (" +
        (Array.isArray(raw) ? raw.length + " points" : "no template") +
        "). Clean the sensor and try again, or use the supervised fallback."
      );
    }
    return points;
  }

  // Capture a minutiae probe. Returns the reader's template, the lab simulation
  // when explicitly configured, or throws FingerprintReaderError.
  async function captureMinutiaeProbe(options) {
    options = options || {};
    var points = await captureFromReader(options);
    if (points) return points;

    if (config().mode === READER_MODE_SIMULATED) {
      if (window.console && window.console.warn) {
        window.console.warn(
          "J-ONE biometrics: BIOMETRIC_READER_MODE=simulated — using a simulated minutiae template. " +
          "This is a lab/demo setting and must not be enabled in production."
        );
      }
      return simulateMinutiae(options.seed);
    }

    throw FingerprintReaderError(
      "No fingerprint reader is available on this workstation. Connect the department reader, " +
      "or ask a manager or administrator to authorise a supervised fallback."
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
    var deskKey = window.Auth && window.Auth.receptionistDesktopKey ? window.Auth.receptionistDesktopKey() : "";
    var headers = deskKey ? { "X-JONE-Desktop-Key": deskKey } : {};
    var res = await window.API.post(window.APP_CONFIG.API_ENDPOINTS.temporaryCredential, {
      email: email,
      fingerprint_probe: probe
    }, { auth: false, headers: headers });
    return res.data && res.data.temporary_token;
  }

  // Dedicated Reception Attendance Workstation status & actions
  async function getReceptionTerminalStatus() {
    var deskKey = window.Auth && window.Auth.receptionistDesktopKey ? window.Auth.receptionistDesktopKey() : "";
    var headers = deskKey ? { "X-JONE-Desktop-Key": deskKey } : {};
    var res = await window.API.get(window.APP_CONFIG.API_ENDPOINTS.receptionTerminal, { headers: headers });
    return res && res.data;
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
    var deskKey = window.Auth && window.Auth.receptionistDesktopKey ? window.Auth.receptionistDesktopKey() : "";
    var headers = deskKey ? { "X-JONE-Desktop-Key": deskKey } : {};
    var res = await window.API.post(window.APP_CONFIG.API_ENDPOINTS.receptionTerminal, data, { headers: headers });
    return res && res.data;
  }

  window.JONE = window.JONE || {};
  window.JONE.biometrics = {
    MIN_CAPTURE_POINTS: MIN_CAPTURE_POINTS,
    FingerprintReaderError: FingerprintReaderError,
    captureMinutiaeProbe: captureMinutiaeProbe,
    requestEnrollment: requestEnrollment,
    issueTemporaryWorkstationToken: issueTemporaryWorkstationToken,
    getReceptionTerminalStatus: getReceptionTerminalStatus,
    executeReceptionTerminalAction: executeReceptionTerminalAction
  };
})(window);
