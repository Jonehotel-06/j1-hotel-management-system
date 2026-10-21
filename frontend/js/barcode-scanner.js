/* js/barcode-scanner.js */
/* ==========================================================================
   J-ONE HOTEL & LODGE — USB HID barcode scanner + printable Code 128 labels.

   Scanner: USB HID scanners behave like a fast keyboard and terminate with
   Enter (some are configured for Tab). This utility distinguishes that
   rapid burst from ordinary typing, never dispatches a scan typed into an
   unrelated form control, de-duplicates the burst a single physical scan
   can produce, and returns a handle whose destroy() removes the listener.

   Labels: generateBarcodeSvg emits a genuine Code 128 symbol (subset B,
   modulo-103 checksum, stop pattern) — the same payload a physical scanner
   reads back — not a decorative barcode-like graphic.
   ========================================================================== */
(function (window) {
  "use strict";

  var SCAN_TIMEOUT_MS = 60; // Max delay between characters for hardware scanner
  var MIN_BARCODE_LENGTH = 3;
  var DEDUPE_MS = 350;

  /* Code 128 element widths for symbol values 0..106 (stop has 7 elements). */
  var CODE128_PATTERNS = [
    "212222", "222122", "222221", "121223", "121322", "131222", "122213", "122312",
    "132212", "221213", "221312", "231212", "112232", "122132", "122231", "113222",
    "123122", "123221", "223211", "221132", "221231", "213212", "223112", "312131",
    "311222", "321122", "321221", "312212", "322112", "322211", "212123", "212321",
    "232121", "111323", "131123", "131321", "112313", "132113", "132311", "211313",
    "231113", "231311", "112133", "112331", "132131", "113123", "113321", "133121",
    "313121", "211331", "231131", "213113", "213311", "213131", "311123", "311321",
    "331121", "312113", "312311", "332111", "314111", "221411", "431111", "111224",
    "111422", "121124", "121421", "141122", "141221", "112214", "112412", "122114",
    "122411", "142112", "142211", "241211", "221114", "413111", "241112", "134111",
    "111242", "121142", "121241", "114212", "124112", "124211", "411212", "421112",
    "421211", "212141", "214121", "412121", "111143", "111341", "131141", "114113",
    "114311", "411113", "411311", "113141", "114131", "311141", "411131", "211412",
    "211214", "211232", "2331112",
  ];
  var START_B = 104;
  var STOP = 106;

  function encodeCode128(code) {
    code = String(code == null ? "" : code);
    if (!code) throw new Error("A barcode value is required.");
    var values = [START_B];
    for (var i = 0; i < code.length; i++) {
      var ascii = code.charCodeAt(i);
      if (ascii < 32 || ascii > 126) {
        throw new Error("Barcode contains characters Code 128 cannot encode.");
      }
      values.push(ascii - 32);
    }
    var checksum = START_B;
    for (var j = 1; j < values.length; j++) checksum += values[j] * j;
    values.push(checksum % 103);
    values.push(STOP);
    return values;
  }

  function initScanner(options) {
    options = options || {};
    var onScan = typeof options.onScan === "function" ? options.onScan : function () {};
    var minLength = options.minLength || MIN_BARCODE_LENGTH;
    var timeoutMs = options.interKeyTimeoutMs || SCAN_TIMEOUT_MS;
    var dedupeMs = options.dedupeMs == null ? DEDUPE_MS : options.dedupeMs;
    var buffer = "";
    var lastKeyTime = 0;
    var lastScanCode = "";
    var lastScanTime = 0;
    var destroyed = false;

    function isBarcodeTarget(target) {
      return !!(target && target.dataset && target.dataset.barcodeTarget != null);
    }
    function isFormTarget(target) {
      return !!(target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT" || target.isContentEditable));
    }

    function dispatch(code, target) {
      var scanned = String(code || "").trim();
      if (scanned.length < minLength) return false;
      var now = Date.now();
      if (dedupeMs > 0 && scanned === lastScanCode && now - lastScanTime < dedupeMs) return false;
      lastScanCode = scanned;
      lastScanTime = now;
      onScan(scanned, { target: target || null });
      return true;
    }

    function onKeyDown(event) {
      if (destroyed) return;
      var target = event.target;
      var barcodeTarget = isBarcodeTarget(target);
      var terminator = event.key === "Enter" || (event.key === "Tab" && (barcodeTarget || buffer.length >= minLength));

      if (terminator) {
        // A dedicated barcode field is authoritative (manual typing is
        // slower than the inter-key window, so the burst buffer alone
        // would lose it). Other form controls keep their own Enter/Tab.
        if (barcodeTarget) {
          var value = target.value != null && String(target.value).trim() ? target.value : buffer;
          if (dispatch(value, target)) {
            event.preventDefault();
            try { target.value = ""; } catch (_) {}
          }
          buffer = "";
          return;
        }
        if (isFormTarget(target)) { buffer = ""; return; }
        if (buffer.trim().length >= minLength) {
          if (dispatch(buffer, target)) event.preventDefault();
        }
        buffer = "";
        return;
      }

      var now = Date.now();
      if (now - lastKeyTime > timeoutMs) buffer = "";
      lastKeyTime = now;
      if (event.key && event.key.length === 1 && !event.ctrlKey && !event.altKey && !event.metaKey) {
        buffer += event.key;
      } else if (event.key === "Backspace") {
        buffer = buffer.slice(0, -1);
      }
    }

    window.addEventListener("keydown", onKeyDown);
    return {
      destroy: function () {
        if (destroyed) return;
        destroyed = true;
        window.removeEventListener("keydown", onKeyDown);
        buffer = "";
      },
      // Test/embedding hook: feed one code through the same dedupe rules.
      scan: function (code) { return dispatch(code, null); },
    };
  }

  // Genuine Code 128 (subset B) SVG for label & receipt printing.
  function generateBarcodeSvg(code, options) {
    options = options || {};
    var height = options.height || 40;
    var barWidth = options.barWidth || 2;
    var showText = options.showText !== false;
    code = String(code || "").trim();
    var values = encodeCode128(code);

    var modules = 0;
    values.forEach(function (value) {
      var pattern = CODE128_PATTERNS[value];
      for (var i = 0; i < pattern.length; i++) modules += Number(pattern[i]);
    });
    var quietModules = 10;
    var width = (modules + quietModules * 2) * barWidth;
    var rects = [];
    var x = quietModules * barWidth;
    values.forEach(function (value) {
      var pattern = CODE128_PATTERNS[value];
      for (var i = 0; i < pattern.length; i++) {
        var w = Number(pattern[i]) * barWidth;
        if (i % 2 === 0) {
          rects.push('<rect x="' + x + '" y="5" width="' + w + '" height="' + height + '" fill="#000"/>');
        }
        x += w;
      }
    });

    var esc = (window.JONE && JONE.esc) ? JONE.esc : function (v) { return String(v).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); };
    var textElement = showText
      ? '<text x="' + (width / 2) + '" y="' + (height + 18) + '" font-family="monospace" font-size="12" text-anchor="middle" fill="#000">' + esc(code) + "</text>"
      : "";

    var totalHeight = height + (showText ? 24 : 10);
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + width + " " + totalHeight + '" width="' + width + '" height="' + totalHeight + '" role="img" aria-label="Code 128 barcode ' + esc(code) + '">' +
      rects.join("") + textElement + "</svg>";
  }

  window.JONE = window.JONE || {};
  window.JONE.barcode = {
    initScanner: initScanner,
    generateBarcodeSvg: generateBarcodeSvg,
    encodeCode128: encodeCode128,
    CODE128_PATTERNS: CODE128_PATTERNS,
  };
})(window);
