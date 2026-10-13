
/* js/thermal-receipt.js */
/* ==========================================================================
   J-ONE HOTEL & LODGE — thermal receipt printing (58 mm and 80 mm).

   Compact logo, centered hotel branding, responsive thermal layout, and
   right-aligned monetary columns.

   New receipts do not show tax. Historical receipts retain their original
   tax breakdown when tax was charged.
   ========================================================================== */

(function (window) {
  "use strict";

  var PAPER = {
    "58": { chars: 32, mm: 58 },
    "80": { chars: 48, mm: 80 }
  };

  var STORAGE_KEY = "jone.thermal.width";

  var DEFAULT_LOGO_SRC = "../assets/icons/logo-official.png";
  var LOGO_ALT = "J-ONE Hotel & Lodge";

  /* Compact logo, maintaining the official portrait aspect ratio. */
  var LOGO_SIZE = {
    "58": { width: 18, height: 32 },
    "80": { width: 24, height: 43 }
  };

  function logoSrc() {
    var configured = null;

    try {
      configured =
        (window.APP_CONFIG && window.APP_CONFIG.RECEIPT_LOGO_SRC) ||
        (window.JONE &&
          window.JONE.branding &&
          window.JONE.branding.logo) ||
        null;
    } catch (e) {
      configured = null;
    }

    return String(configured || DEFAULT_LOGO_SRC);
  }

  function getWidth() {
    try {
      var saved = String(
        (window.localStorage &&
          window.localStorage.getItem(STORAGE_KEY)) || ""
      );

      if (PAPER[saved]) return saved;
    } catch (e) {
      /* Storage unavailable. */
    }

    return "80";
  }

  function setWidth(width) {
    width = String(width || "");

    if (!PAPER[width]) {
      throw new Error("Unsupported thermal paper width: " + width);
    }

    try {
      if (window.localStorage) {
        window.localStorage.setItem(STORAGE_KEY, width);
      }
    } catch (e) {
      /* Storage unavailable; continue without persistence. */
    }

    return width;
  }

  /* Character-grid helpers. */

  function naira(amount) {
    var n = Number(amount || 0);

    return "\u20a6" +
      n.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  function padLine(left, right, chars) {
    left = String(left == null ? "" : left);
    right = String(right == null ? "" : right);

    var space = chars - left.length - right.length;

    if (space < 1) {
      left = left.slice(
        0,
        Math.max(0, chars - right.length - 1)
      );

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
        if (current) {
          lines.push(current);
          current = "";
        }

        lines.push(word.slice(0, chars));
        word = word.slice(chars);
      }

      if (!current) {
        current = word;
      } else if ((current + " " + word).length <= chars) {
        current += " " + word;
      } else {
        lines.push(current);
        current = word;
      }
    });

    if (current) lines.push(current);

    return lines.length ? lines : [""];
  }

  function centerLine(text, chars) {
    text = String(text == null ? "" : text).trim();

    if (text.length >= chars) return text;

    var leftPadding = Math.floor((chars - text.length) / 2);

    return new Array(leftPadding + 1).join(" ") + text;
  }

  function rule(chars, ch) {
    return new Array(chars + 1).join(ch || "-");
  }

  /* Receipt item rows. */

  function itemRows(item, chars) {
    var rows = [];
    var moneyWidth = Math.max(22, chars);

    var quantity = Number(item.quantity || 1);
    var unitPrice = Number(item.unit_price || 0);

    var lineTotal = item.line_total != null
      ? Number(item.line_total)
      : quantity * unitPrice;

    var totals = padLine(
      quantity + " x " + naira(unitPrice),
      naira(lineTotal),
      moneyWidth
    );

    wrapText(item.name, chars).forEach(function (line) {
      rows.push(line);
    });

    rows.push(totals.slice(totals.length - chars));

    return rows;
  }

  /* Receipt text rendering. */

  function renderLines(receipt, width) {
    var chars = (PAPER[width] || PAPER[getWidth()]).chars;
    var lines = [];
    var hotel = receipt.hotel || {};

    /* Center the hotel identity. */
    wrapText(
      hotel.name || "J-ONE HOTEL & LODGE",
      chars
    ).forEach(function (line) {
      lines.push(centerLine(line, chars));
    });

    if (hotel.address) {
      wrapText(hotel.address, chars).forEach(function (line) {
        lines.push(centerLine(line, chars));
      });
    }

    if (hotel.phone) {
      wrapText("Tel: " + hotel.phone, chars).forEach(function (line) {
        lines.push(centerLine(line, chars));
      });
    }

    lines.push(rule(chars));

    lines.push(
      padLine(
        "Receipt: " + (receipt.reference || "-"),
        receipt.issued_at
          ? String(receipt.issued_at).slice(0, 16)
          : "",
        chars
      )
    );

    if (receipt.cashier) {
      lines.push(
        padLine("Cashier: " + receipt.cashier, "", chars).replace(/\s+$/, "")
      );
    }

    if (receipt.workstation) {
      lines.push(
        padLine("Terminal: " + receipt.workstation, "", chars).replace(/\s+$/, "")
      );
    }

    if (receipt.mode) {
      lines.push(
        padLine("Type: " + receipt.mode, "", chars).replace(/\s+$/, "")
      );
    }

    lines.push(rule(chars));

    /* Items. */
    (receipt.items || []).forEach(function (item) {
      itemRows(item, chars).forEach(function (line) {
        lines.push(line);
      });
    });

    lines.push(rule(chars));

    lines.push(padLine("Subtotal", naira(receipt.subtotal), chars));

    if (Number(receipt.discount || 0) > 0) {
      lines.push(
        padLine("Discount", "-" + naira(receipt.discount), chars)
      );
    }

    /* Preserve tax only on historical receipts where it was charged. */
    if (Number(receipt.tax || 0) > 0) {
      lines.push(
        padLine("Tax (as charged)", naira(receipt.tax), chars)
      );
    }

    lines.push(rule(chars, "="));
    lines.push(padLine("TOTAL", naira(receipt.total), chars));
    lines.push(rule(chars, "="));

    if (receipt.paid != null) {
      lines.push(padLine("Paid", naira(receipt.paid), chars));
    }

    if (Number(receipt.change || 0) > 0) {
      lines.push(padLine("Change", naira(receipt.change), chars));
    }

    if (Number(receipt.balance_due || 0) > 0) {
      lines.push(
        padLine("Balance due", naira(receipt.balance_due), chars)
      );
    }

    if (receipt.payment_method) {
      lines.push(
        padLine("Method: " + receipt.payment_method, "", chars)
          .replace(/\s+$/, "")
      );
    }

    if (receipt.payment_reference) {
      wrapText("Ref: " + receipt.payment_reference, chars).forEach(
        function (line) {
          lines.push(line);
        }
      );
    }

    lines.push(rule(chars));

    wrapText("Thank you for staying with us", chars).forEach(
      function (line) {
        lines.push(centerLine(line, chars));
      }
    );

    lines.push("");

    return lines;
  }

  /* Escape HTML attributes safely. */

  function escapeAttr(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  /* HTML preview. */

  function renderHTML(receipt, width, options) {
    width = width || getWidth();
    options = options || {};

    var lines = renderLines(receipt, width);

    var escaped = lines.map(function (line) {
      return line
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
    }).join("\n");

    var brand = "";

    if (options.logo !== false) {
      var size = LOGO_SIZE[width] || LOGO_SIZE[getWidth()];

      brand =
        '<div class="thermal-brand">' +
          '<img class="thermal-logo" src="' +
            escapeAttr(logoSrc()) +
            '" alt="' + LOGO_ALT + '"' +
            ' width="' + size.width + '"' +
            ' height="' + size.height + '"' +
          '>' +
        '</div>';
    }

    return (
      '<div class="thermal-receipt thermal-' + width + '"' +
        ' data-paper-mm="' + PAPER[width].mm + '">' +
        brand +
        '<pre class="thermal-body">' + escaped + '</pre>' +
      '</div>'
    );
  }

  /* Wait for the logo without blocking printing indefinitely. */

  function whenImageSettled(doc, callback, timeoutMs) {
    var img = doc.querySelector &&
      doc.querySelector("img.thermal-logo");

    if (!img) {
      callback();
      return;
    }

    var done = false;

    function finish() {
      if (done) return;
      done = true;
      callback();
    }

    if (img.complete) {
      if (img.naturalWidth === 0 && img.parentNode) {
        img.parentNode.removeChild(img);
      }

      finish();
      return;
    }

    img.addEventListener("load", finish);

    img.addEventListener("error", function () {
      if (img.parentNode) {
        img.parentNode.removeChild(img);
      }

      finish();
    });

    setTimeout(finish, timeoutMs || 1500);
  }

  /* Print receipt. */

  function print(receipt, options) {
    options = options || {};

    var width = String(options.width || getWidth());

    if (!PAPER[width]) {
      throw new Error("Unsupported thermal paper width: " + width);
    }

    var frame = window.document.createElement("iframe");

    frame.setAttribute("aria-hidden", "true");

    frame.style.cssText =
      "position:fixed;right:0;bottom:0;width:0;height:0;border:0;";

    window.document.body.appendChild(frame);

    var doc = frame.contentWindow.document;

    doc.open();

    doc.write(
      "<!DOCTYPE html><html><head>" +
      "<meta charset=\"utf-8\">" +
      "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">" +
      "<title>Receipt " +
        String(receipt.reference || "").replace(/[<>&]/g, "") +
      "</title><style>" +

      "@page { size: " + PAPER[width].mm + "mm auto; margin: 1.5mm; }" +

      "html,body {" +
        "margin:0;padding:0;width:100%;" +
        "-webkit-print-color-adjust:exact;print-color-adjust:exact;" +
      "}" +

      "*,*::before,*::after { box-sizing:border-box; }" +

      ".thermal-receipt {" +
        "width:100%;max-width:100%;margin:0 auto;" +
      "}" +

      ".thermal-brand {" +
        "display:flex;width:100%;justify-content:center;" +
        "align-items:center;text-align:center;" +
        "margin:0 auto 2mm;padding:0;" +
      "}" +

      ".thermal-logo {" +
        "display:block;width:" + LOGO_SIZE[width].width + "px;" +
        "height:" + LOGO_SIZE[width].height + "px;" +
        "max-width:100%;object-fit:contain;margin:0 auto;" +
      "}" +

      "pre.thermal-body {" +
        "display:block;font-family:'Courier New',monospace;" +
        "font-size:" + (width === "58" ? "10px" : "10px") + ";" +
        "line-height:1.3;width:100%;max-width:100%;" +
        "margin:0;padding:0;white-space:pre;" +
        "overflow:visible;text-align:left;" +
      "}" +

      "@media print {" +
        "html,body { margin:0 !important;padding:0 !important; }" +
        ".thermal-receipt { break-inside:avoid; page-break-inside:avoid; }" +
        ".thermal-brand,.thermal-logo { break-inside:avoid; }" +
      "}" +

      "</style></head><body>" +
      renderHTML(receipt, width, { logo: options.logo }) +
      "</body></html>"
    );

    doc.close();

    var cleanup = function () {
      setTimeout(function () {
        if (frame.parentNode) {
          frame.parentNode.removeChild(frame);
        }
      }, 1000);
    };

    whenImageSettled(
      doc,
      function () {
        try {
          frame.contentWindow.focus();
          frame.contentWindow.print();
        } finally {
          cleanup();
        }
      },
      options.logoTimeoutMs
    );
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
    centerLine: centerLine,
    itemRows: itemRows,
    renderLines: renderLines,
    renderHTML: renderHTML,
    whenImageSettled: whenImageSettled,
    print: print
  };

})(window);
