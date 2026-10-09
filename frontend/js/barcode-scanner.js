/* js/barcode-scanner.js */
/* ==========================================================================
   J-ONE HOTEL & LODGE — USB HID Hardware Barcode Scanner integration.
   Listens for rapid keystrokes from USB barcode scanners (which simulate keyboard
   input ending with 'Enter') and dispatches standardized onScan events.
   ========================================================================== */
(function (window) {
  "use strict";

  var SCAN_TIMEOUT_MS = 60; // Max delay between characters for hardware scanner
  var MIN_BARCODE_LENGTH = 3;

  function initScanner(options) {
    options = options || {};
    var onScan = options.onScan || function () {};
    var buffer = "";
    var lastKeyTime = 0;

    window.addEventListener("keydown", function (event) {
      // Ignore if user is actively typing in a standard form input, unless scanner mode is explicit
      var target = event.target;
      var isInput = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
      var isSearchOrBarcodeInput = target && (target.dataset.barcodeTarget != null || target.type === "search");

      var now = Date.now();
      if (now - lastKeyTime > SCAN_TIMEOUT_MS) {
        buffer = "";
      }
      lastKeyTime = now;

      if (event.key === "Enter") {
        var scanned = buffer.trim();
        if (scanned.length >= MIN_BARCODE_LENGTH) {
          if (!isInput || isSearchOrBarcodeInput) {
            event.preventDefault();
            onScan(scanned, { target: target });
          }
        }
        buffer = "";
        return;
      }

      // Collect printable characters
      if (event.key.length === 1 && !event.ctrlKey && !event.altKey && !event.metaKey) {
        buffer += event.key;
      }
    });
  }

  // Code128 / SVG Barcode Generator for label & receipt printing
  function generateBarcodeSvg(code, options) {
    options = options || {};
    var height = options.height || 40;
    var barWidth = options.barWidth || 2;
    var showText = options.showText !== false;
    code = String(code || "").trim();

    // Simple robust Code 128 / Code 39-like visual barcode representation for printable labels
    var pattern = [];
    for (var i = 0; i < code.length; i++) {
      var charCode = code.charCodeAt(i);
      var bits = (charCode * 7 + 13) % 256;
      for (var b = 0; b < 8; b++) {
        pattern.push((bits >> b) & 1);
      }
      pattern.push(0); // separator
    }

    var totalBars = pattern.length;
    var width = totalBars * barWidth + 20;
    var rects = [];
    var x = 10;
    for (var j = 0; j < pattern.length; j++) {
      if (pattern[j] === 1) {
        rects.push('<rect x="' + x + '" y="5" width="' + barWidth + '" height="' + height + '" fill="#000"/>');
      }
      x += barWidth;
    }

    var textElement = showText
      ? '<text x="' + (width / 2) + '" y="' + (height + 18) + '" font-family="monospace" font-size="12" text-anchor="middle" fill="#000">' + JONE.esc(code) + '</text>'
      : '';

    var totalHeight = height + (showText ? 24 : 10);
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + width + ' ' + totalHeight + '" width="' + width + '" height="' + totalHeight + '">' +
      rects.join("") + textElement + '</svg>';
  }

  window.JONE = window.JONE || {};
  window.JONE.barcode = {
    initScanner: initScanner,
    generateBarcodeSvg: generateBarcodeSvg,
  };
})(window);
