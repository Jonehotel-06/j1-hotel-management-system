/* js/thermal-receipt.js */
/* ==========================================================================
   J-ONE HOTEL & LODGE — thermal receipt printing (58 mm and 80 mm).

   POS/restaurant/bar sales print on thermal paper roll printers via the
   browser print dialog. Paper width is configurable (58 or 80 mm) and
   persisted; layouts are responsive to both.

   Customer-billing tax removal (owner policy 2026-10-11): new receipts NEVER
   show a tax line. A historical receipt (one whose stored tax is > 0) keeps
   its original breakdown and prints the tax line exactly as charged then.

   Monetary columns are right-aligned at fixed character widths so totals line
   up on cheap printers; long item names wrap instead of clipping.
   ========================================================================== */
(function (window) {
  "use strict";

  var PAPER = {
    "58": { chars: 32, mm: 58 },
    "80": { chars: 48, mm: 80 }
  };
  var STORAGE_KEY = "jone.thermal.width";

  /* ---- shared branding -------------------------------------------------- */

  /* The ONE place the printed receipt gets its logo from. Receipts render from
     /dashboard/*, so the default resolves to the same official asset the staff
     console, the emailed receipt and the PDF receipt all use. A deployment can
     override it once (window.APP_CONFIG.RECEIPT_LOGO_SRC or
     window.JONE.branding.logo) instead of repeating an <img> per template. */
  var DEFAULT_LOGO_SRC = "../assets/icons/logo-official.png";
  var LOGO_ALT = "J-ONE Hotel & Lodge";

  /* Printed size per paper width. The mark is portrait (507x900), so the pair
     is kept at that ratio — a square box would stretch it. Sizes are small on
     purpose: the header must not eat half the roll. */
  var LOGO_SIZE = {
    "58": { width: 34, height: 60 },
    "80": { width: 42, height: 75 }
  };

  function logoSrc() {
    var configured = null;
    try {
      configured = (window.APP_CONFIG && window.APP_CONFIG.RECEIPT_LOGO_SRC) ||
        (window.JONE && window.JONE.branding && window.JONE.branding.logo) || null;
    } catch (e) { configured = null; }
    return String(configured || DEFAULT_LOGO_SRC);
  }

  function getWidth() {
    try {
      var saved = String((window.localStorage && window.localStorage.getItem(STORAGE_KEY)) || "");
      if (PAPER[saved]) return saved;
    } catch (e) { /* storage unavailable — fall through */ }
    return "80";
  }

  function setWidth(width) {
    width = String(width || "");
    if (!PAPER[width]) throw new Error("Unsupported thermal paper width: " + width);
    try {
      window.localStorage && window.localStorage.setItem(STORAGE_KEY, width);
    } catch (e) { /* non-fatal */ }
    return width;
  }

  /* ---- character-grid helpers ------------------------------------------ */

  function naira(amount) {
    var n = Number(amount || 0);
    return "\u20a6" + n.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  function padLine(left, right, chars) {
    left = String(left == null ? "" : left);
    right = String(right == null ? "" : right);
    var space = chars - left.length - right.length;
    if (space < 1) {
      // Left is too long: truncate the label side, keep the amount visible.
      left = left.slice(0, Math.max(0, chars - right.length - 1));
      space = chars - left.length - right.length;
    }
    return left + new Array(space + 1).join(" ") + right;
  }

  function wrapText(text, chars) {
    text = String(text == null ? "" : text);
    if (text.length <= chars) return [text];
    var words = text.split(/\s+/);
    var lines = [];
    var current = "";
    words.forEach(function (word) {
      while (word.length > chars) {
        if (current) { lines.push(current); current = ""; }
        lines.push(word.slice(0, chars));
        word = word.slice(chars);
      }
      if (!current) current = word;
      else if ((current + " " + word).length <= chars) current = current + " " + word;
      else { lines.push(current); current = word; }
    });
    if (current) lines.push(current);
    return lines.length ? lines : [""];
  }

  function rule(chars, ch) {
    return new Array(chars + 1).join(ch || "-");
  }

  /* ---- item lines ------------------------------------------------------ */

  /* One cart line as printed grid rows: name(s) wrapped, then qty/price/total
     aligned. A long name wraps ABOVE the numbers so nothing is clipped. */
  function itemRows(item, chars) {
    var rows = [];
    var moneyWidth = 22; // qty x unit ......... amount
    var totals = padLine(
      (item.quantity || 1) + " x " + naira(item.unit_price),
      naira(item.line_total != null ? item.line_total : (item.quantity || 1) * (item.unit_price || 0)),
      Math.max(moneyWidth, chars)
    );
    wrapText(item.name, chars).forEach(function (line) { rows.push(line); });
    rows.push(totals.slice(totals.length - chars));
    return rows;
  }

  /* ---- receipt rendering ----------------------------------------------- */

  /*
   * receipt = {
   *   hotel: { name, address, phone },
   *   reference, issued_at, cashier, workstation, mode,
   *   items: [{ name, quantity, unit_price, line_total }],
   *   subtotal, discount, tax, total, paid, change, balance_due,
   *   payment_method, payment_reference
   * }
   * Returns an array of plain-text lines sized for the chosen paper.
   */
  function renderLines(receipt, width) {
    var chars = (PAPER[width] || PAPER[getWidth()]).chars;
    var lines = [];
    var hotel = receipt.hotel || {};

    // Identity block
    wrapText(hotel.name || "J-ONE HOTEL & LODGE", chars).forEach(function (l) { lines.push(l); });
    if (hotel.address) wrapText(hotel.address, chars).forEach(function (l) { lines.push(l); });
    if (hotel.phone) wrapText("Tel: " + hotel.phone, chars).forEach(function (l) { lines.push(l); });
    lines.push(rule(chars));
    lines.push(padLine("Receipt: " + (receipt.reference || "-"),
      receipt.issued_at ? String(receipt.issued_at).slice(0, 16) : "", chars));
    if (receipt.cashier) lines.push(padLine("Cashier: " + receipt.cashier, "", chars).replace(/\s+$/, ""));
    if (receipt.workstation) lines.push(padLine("Terminal: " + receipt.workstation, "", chars).replace(/\s+$/, ""));
    if (receipt.mode) lines.push(padLine("Type: " + receipt.mode, "", chars).replace(/\s+$/, ""));
    lines.push(rule(chars));

    // Items
    (receipt.items || []).forEach(function (item) {
      itemRows(item, chars).forEach(function (l) { lines.push(l); });
    });

    lines.push(rule(chars));
    lines.push(padLine("Subtotal", naira(receipt.subtotal), chars));
    if (Number(receipt.discount || 0) > 0) {
      lines.push(padLine("Discount", "-" + naira(receipt.discount), chars));
    }
    // Customer-billing tax removal: new receipts carry tax = 0 and must not
    // imply an added tax. Historical receipts (> 0) keep their breakdown.
    if (Number(receipt.tax || 0) > 0) {
      lines.push(padLine("Tax (as charged)", naira(receipt.tax), chars));
    }
    lines.push(rule(chars, "="));
    lines.push(padLine("TOTAL", naira(receipt.total), chars));
    lines.push(rule(chars, "="));
    if (receipt.paid != null) lines.push(padLine("Paid", naira(receipt.paid), chars));
    if (Number(receipt.change || 0) > 0) lines.push(padLine("Change", naira(receipt.change), chars));
    if (Number(receipt.balance_due || 0) > 0) lines.push(padLine("Balance due", naira(receipt.balance_due), chars));
    if (receipt.payment_method) lines.push(padLine("Method: " + receipt.payment_method, "", chars).replace(/\s+$/, ""));
    if (receipt.payment_reference) {
      wrapText("Ref: " + receipt.payment_reference, chars).forEach(function (l) { lines.push(l); });
    }
    lines.push(rule(chars));
    lines.push(padLine("Thank you for staying with us", "", chars).replace(/\s+$/, ""));
    lines.push("");
    return lines;
  }

  /* HTML mirror of renderLines for browser print-preview. */
  function renderHTML(receipt, width, options) {
    width = width || getWidth();
    options = options || {};
    var lines = renderLines(receipt, width);
    var escaped = lines.map(function (l) {
      return l.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }).join("\n");
    var brand = "";
    if (options.logo !== false) {
      var size = LOGO_SIZE[width] || LOGO_SIZE[getWidth()];
      brand =
        '<div class="thermal-brand" style="text-align:center; margin:0 0 3px;">' +
        '<img class="thermal-logo" src="' + escapeAttr(logoSrc()) + '"' +
        ' alt="' + LOGO_ALT + '" width="' + size.width + '" height="' + size.height + '"' +
        ' style="display:block; margin:0 auto; width:' + size.width + "px; height:" + size.height +
        'px; image-rendering:-webkit-optimize-contrast;">' +
        "</div>";
    }
    return (
      '<div class="thermal-receipt thermal-' + width + '" data-paper-mm="' + PAPER[width].mm + '">' +
      brand +
      '<pre class="thermal-body">' + escaped + "</pre></div>"
    );
  }

  function escapeAttr(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/"/g, "&quot;")
      .replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  /* Wait for the logo before printing, but never hang on it.
     A thermal receipt that prints with no logo is acceptable; one that never
     prints because an image stalled is not. Whichever of load/error/timeout
     happens first wins, and a failed image is removed so the printer never
     emits a broken-image box. */
  function whenImageSettled(doc, callback, timeoutMs) {
    var img = doc.querySelector && doc.querySelector("img.thermal-logo");
    if (!img) { callback(); return; }
    var done = false;
    function finish() {
      if (done) { return; }
      done = true;
      callback();
    }
    if (img.complete) {
      if (img.naturalWidth === 0) { img.parentNode && img.parentNode.removeChild(img); }
      finish();
      return;
    }
    img.addEventListener("load", finish);
    img.addEventListener("error", function () {
      if (img.parentNode) { img.parentNode.removeChild(img); }
      finish();
    });
    setTimeout(finish, timeoutMs || 1500);
  }

  /* Injects a print sheet and opens the browser print dialog. */
  function print(receipt, options) {
    options = options || {};
    var width = options.width || getWidth();
    var frame = window.document.createElement("iframe");
    frame.setAttribute("aria-hidden", "true");
    frame.style.cssText = "position:fixed;right:0;bottom:0;width:0;height:0;border:0;";
    window.document.body.appendChild(frame);
    var doc = frame.contentWindow.document;
    doc.open();
    doc.write(
      "<!DOCTYPE html><html><head><title>Receipt " +
      String(receipt.reference || "").replace(/[<>&]/g, "") +
      "</title><style>" +
      "@page { size: " + PAPER[width].mm + "mm auto; margin: 2mm; }" +
      "html,body { margin:0; padding:0; }" +
      "pre.thermal-body { font-family: 'Courier New', ui-monospace, monospace;" +
      " font-size: 11px; line-height: 1.35; width: " + PAPER[width].mm + "mm;" +
      " margin: 0; white-space: pre; overflow: hidden; }" +
      ".thermal-brand { width: " + PAPER[width].mm + "mm; }" +
      ".thermal-logo { max-width: 100%; height: auto; }" +
      "</style></head><body>" +
      renderHTML(receipt, width, { logo: options.logo }) +
      "</body></html>"
    );
    doc.close();

    var cleanup = function () {
      setTimeout(function () {
        if (frame.parentNode) { frame.parentNode.removeChild(frame); }
      }, 1000);
    };
    whenImageSettled(doc, function () {
      try {
        frame.contentWindow.focus();
        frame.contentWindow.print();
      } finally {
        cleanup();
      }
    }, options.logoTimeoutMs);
  }

  window.JONE = window.JONE || {};
  window.JONE.ThermalReceipt = {
    PAPER: PAPER,
    LOGO_SIZE: LOGO_SIZE,
    getWidth: getWidth,
    setWidth: setWidth,
    logoSrc: logoSrc,
    naira: naira,
    padLine: padLine,
    wrapText: wrapText,
    itemRows: itemRows,
    renderLines: renderLines,
    renderHTML: renderHTML,
    whenImageSettled: whenImageSettled,
    print: print
  };
})(window);
