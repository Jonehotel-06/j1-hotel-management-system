/* js/receipt.js */
/* ========================================================================== 
   receipt.js — Access-style J-ONE receipt rendering and receipt-only exports.

   Used by the staff receipt page and guest booking confirmation page. The
   renderer accepts only backend-supplied booking/payment/receipt data; it never
   recalculates totals. Image/PDF downloads rasterize only the receipt element,
   not the surrounding page chrome.
   ========================================================================== */

(function () {
  "use strict";

  var RECEIPT_WIDTH = 720;
  var EXPORT_SCALE_MAX = 3;
  var EXPORT_SCALE_MIN = 2;

  var FALLBACK_HOTEL = {
    name: "J-ONE HOTEL & LODGE",
    phone: "+234 803 211 2874",
    email: "jonathanonu76@gmail.com",
    address: "Plot 566 Mgbowo Street, off Ezike Street, Enugu State."
  };

  /* Exact-colour export CSS. It intentionally does not depend on CSS custom
     properties so the same look survives inside the SVG foreignObject used for
     PNG/PDF generation. */
  var EXPORT_CSS = [
    "*{box-sizing:border-box}",
    "body{margin:0;background:#fff;color:#17202a;font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;}",
    ".receipt-sheet.rc-access{position:relative;width:720px;max-width:none;margin:0;background:#fff;color:#17202a;border:0;border-radius:0;box-shadow:none;padding:52px 60px 62px;overflow:hidden;isolation:isolate;font-size:14px;line-height:1.42;}",
    ".rc-access *{box-sizing:border-box}",
    ".rc-head{display:flex;align-items:flex-start;justify-content:flex-start;gap:24px;padding:0;border:0;}",
    ".rc-logo-lockup{display:flex;align-items:center;gap:12px;min-width:0;}",
    ".rc-logo{width:28px;height:52px;max-width:28px;object-fit:contain;flex:none;}",
    ".rc-wordmark{font-size:30px;line-height:.9;font-weight:800;letter-spacing:-.04em;color:#143d69;text-transform:uppercase;white-space:nowrap;}",
    ".rc-wordmark-sub{margin-top:5px;font-size:9px;font-weight:800;letter-spacing:.28em;color:#6c6e70;text-transform:uppercase;white-space:nowrap;}",
    ".rc-title{margin:18px 0 0;text-align:center;font-size:30px;line-height:1.12;font-weight:800;letter-spacing:-.02em;color:#053e75;}",
    ".rc-generated{margin:18px 0 0;text-align:center;font-size:13px;color:#7b828a;}",
    ".rc-generated strong{color:#6c6e70;font-weight:800;}",
    ".rc-access-table{margin-top:30px;border-top:0;}",
    ".rc-access-row{display:grid;grid-template-columns:225px minmax(0,1fr);gap:18px;min-height:53px;align-items:center;border-bottom:1px solid #e2e6ea;padding:11px 8px 11px 6px;}",
    ".rc-access-label{font-size:15px;line-height:1.25;font-weight:800;color:#a66d10;}",
    ".rc-access-value{font-size:15px;line-height:1.35;font-weight:650;color:#123f70;word-break:break-word;overflow-wrap:anywhere;}",
    ".rc-access-value.rc-amount-main{font-size:16px;font-weight:800;}",
    ".rc-value-line{display:block;margin:0 0 4px;}",
    ".rc-value-line:last-child{margin-bottom:0;}",
    ".rc-value-muted{color:#65707a;font-weight:600;}",
    ".rc-access-status{font-weight:800;color:#0b6b3a;}",
    ".rc-access-status.is-warning{color:#8a5a0a;}",
    ".rc-access-status.is-danger{color:#a72f28;}",
    ".rc-access-foot{margin-top:20px;padding-top:0;font-size:12px;line-height:1.35;color:#707780;border:0;}",
    ".rc-access-foot p{margin:0 0 6px;}",
    ".rc-access-foot a{color:#063f79;text-decoration:underline;}",
    ".rc-access-channels{margin-top:28px;font-size:12px;color:#80868d;}",
    ".receipt-watermark,.receipt-content{position:static;}",
    ".loading-block,.empty-state{min-height:180px;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:10px;color:#65707a;}"
  ].join("\n");

  function esc(value) {
    if (window.JONE && typeof JONE.esc === "function") return JONE.esc(value == null ? "" : String(value));
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function clean(value) {
    if (value == null) return "";
    var s = String(value).trim();
    return s === "—" || s === "--" ? "" : s;
  }

  function first() {
    for (var i = 0; i < arguments.length; i += 1) {
      var v = arguments[i];
      if (v !== undefined && v !== null && clean(v) !== "") return v;
    }
    return "";
  }

  function asNumber(value) {
    var n = Number(value);
    return Number.isFinite(n) ? n : 0;
  }

  function money(value) {
    if (window.JONE && typeof JONE.formatNaira === "function") return JONE.formatNaira(asNumber(value));
    return "₦" + Math.round(asNumber(value)).toLocaleString("en-NG");
  }

  function labelize(value) {
    return clean(value).replace(/_/g, " ").replace(/\s+/g, " ").trim();
  }

  function titleCase(value) {
    var s = labelize(value).toLowerCase();
    return s.replace(/\b\w/g, function (m) { return m.toUpperCase(); });
  }

  function parseDate(value) {
    if (!value) return null;
    var d = new Date(value);
    if (!Number.isNaN(d.getTime())) return d;
    if (window.JONE && typeof JONE.parseISO === "function") {
      var parsed = JONE.parseISO(value);
      if (parsed && !Number.isNaN(parsed.getTime())) return parsed;
    }
    return null;
  }

  function pad(n) { return String(n).padStart(2, "0"); }

  function accessDateTime(value) {
    var d = parseDate(value) || new Date();
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " +
      pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  function generatedDateTime(value) {
    var d = parseDate(value) || new Date();
    return pad(d.getDate()) + "/" + pad(d.getMonth() + 1) + "/" + String(d.getFullYear()).slice(-2) + " " +
      pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  function dateOnly(value) {
    if (!value) return "";
    if (window.JONE && typeof JONE.formatDate === "function") return JONE.formatDate(value, "mid");
    var d = parseDate(value);
    return d ? (pad(d.getDate()) + " " + pad(d.getMonth() + 1) + " " + d.getFullYear()) : clean(value);
  }

  function formatNights(n) {
    if (n == null || clean(n) === "") return "";
    var count = Number(n);
    if (!Number.isFinite(count)) return clean(n);
    return count + (count === 1 ? " night" : " nights");
  }

  function compactList(parts, sep) {
    return parts.map(clean).filter(Boolean).join(sep || ", ");
  }

  function roomNumbers(data) {
    if (Array.isArray(data.room_numbers) && data.room_numbers.length) return data.room_numbers.join(", ");
    if (Array.isArray(data.room_assignments) && data.room_assignments.length) {
      var nums = data.room_assignments.map(function (a) { return a && a.room_number; }).filter(Boolean);
      if (nums.length) return nums.join(", ");
    }
    if (data.rooms || data.number_of_rooms) {
      var count = data.rooms || data.number_of_rooms;
      return count + " room" + (Number(count) === 1 ? "" : "s");
    }
    return "";
  }

  function latestPayment(data) {
    if (data && data.latest_payment) return data.latest_payment;
    if (data && Array.isArray(data.payments) && data.payments.length) return data.payments[data.payments.length - 1];
    return null;
  }

  function hotelFrom(data) {
    var h = (data && data.hotel) || (window.HOTEL || null) || FALLBACK_HOTEL;
    return {
      name: first(h.name, h.hotel_name, h.display_name, FALLBACK_HOTEL.name),
      phone: first(h.phone, h.phone_number, h.telephone, FALLBACK_HOTEL.phone),
      email: first(h.email, h.contact_email, FALLBACK_HOTEL.email),
      address: compactList([
        first(h.address, h.street_address, FALLBACK_HOTEL.address),
        h.city, h.state, h.country
      ], ", ")
    };
  }

  function guestFrom(data) {
    var g = (data && data.guest) || {};
    return {
      name: first(g.name, g.full_name, compactList([g.first_name, g.last_name], " "), "Guest"),
      phone: first(g.phone, g.phone_number),
      email: first(g.email),
      address: compactList([g.address, g.city, g.state, g.country], ", ")
    };
  }

  function statusText(data, latest) {
    var raw = String(first(data && data.payment_status, latest && latest.status, data && data.status)).toUpperCase();
    if (raw === "PAID" || raw === "SUCCESS") return "Successful";
    if (raw === "PARTIALLY_PAID") return "Partially paid";
    if (raw === "UNPAID" || raw === "PENDING") return "Awaiting payment";
    if (raw === "PARTIALLY_REFUNDED") return "Partially refunded";
    if (raw === "REFUNDED") return "Refunded";
    if (raw === "FAILED") return "Failed";
    if (raw === "CANCELLED") return "Cancelled";
    return titleCase(raw || "Processed");
  }

  function statusClass(label) {
    var l = String(label || "").toLowerCase();
    if (l.indexOf("failed") !== -1 || l.indexOf("cancelled") !== -1) return " is-danger";
    if (l.indexOf("awaiting") !== -1 || l.indexOf("partial") !== -1 || l.indexOf("pending") !== -1) return " is-warning";
    return "";
  }

  function isPaystack(latest) {
    if (!latest) return false;
    var p = String(first(latest.provider, latest.provider_label)).toUpperCase();
    return p.indexOf("PAYSTACK") !== -1;
  }

  function paymentType(latest) {
    if (!latest) return "BOOKING CONFIRMATION";
    var provider = String(first(latest.provider_label, latest.provider)).toUpperCase().replace(/_/g, " ");
    var channel = String(first(latest.channel)).toUpperCase().replace(/_/g, " ");
    if (isPaystack(latest)) return "ONLINE PAYMENT" + (channel ? " / " + channel : "");
    // Offline records store the provider as the channel too (cash → "cash"),
    // so drop the channel when the provider already contains it
    // ("CASH / CASH", "POS TERMINAL / POS") — nothing is displayed twice.
    var normChannel = channel.replace(/[^A-Z0-9]/g, "");
    if (normChannel && provider.replace(/[^A-Z0-9]/g, "").indexOf(normChannel) !== -1) channel = "";
    return provider + (channel ? " / " + channel : "");
  }

  function normalize(data, opts) {
    data = data || {};
    opts = opts || {};
    var latest = latestPayment(data);
    var hotel = hotelFrom(data);
    var guest = guestFrom(data);
    var total = first(data.total, data.total_amount);
    var paid = first(data.amount_paid);
    var due = first(data.amount_due);
    var amount = latest && latest.amount != null ? latest.amount : first(paid && asNumber(paid) > 0 ? paid : "", due && asNumber(due) > 0 ? due : "", total);
    var bookingRef = first(data.booking_reference, data.reference);
    var paymentRef = first(latest && latest.reference, data.receipt_reference, data.payment_reference, bookingRef);
    // Gateway session/transaction id: only a real provider value may appear
    // here. Offline (cash/POS/transfer) payments have no gateway session —
    // never substitute the payment reference and never invent one.
    var txId = isPaystack(latest)
      ? first(latest && latest.transaction_id, data.transaction_id, latest && latest.provider_reference, paymentRef)
      : first(latest && latest.transaction_id);
    var roomType = first(data.room_type, data.room_type_name, opts.room_type);
    var rooms = roomNumbers(data);
    var stayLine = compactList([roomType, rooms ? "Room " + rooms : ""], " · ");
    var stayDates = compactList([
      data.check_in ? "Check-in " + dateOnly(data.check_in) : "",
      data.check_out ? "Check-out " + dateOnly(data.check_out) : "",
      formatNights(data.nights)
    ], " · ");
    var guestCount = first(data.number_of_guests, (asNumber(data.adults) + asNumber(data.children)) || "");
    var stayGuests = guestCount ? guestCount + " guest" + (Number(guestCount) === 1 ? "" : "s") : "";
    var status = statusText(data, latest);
    // Offline (front-desk) payments carry the staff member who recorded them;
    // Paystack rows carry the payer, which is not "recorded by" information.
    var recordedBy = latest && !isPaystack(latest) ? first(latest.recorded_by) : "";
    var notes = latest ? first(latest.notes) : "";
    return {
      hotel: hotel,
      guest: guest,
      latest: latest,
      recordedBy: recordedBy,
      notes: notes,
      sourceLabel: first(opts.sourceLabel, opts.generatedFrom, "J-ONE"),
      documentTitle: first(opts.documentTitle, "Transaction Receipt"),
      amount: amount,
      transactionType: paymentType(latest),
      transactionDate: accessDateTime(first(latest && latest.paid_at, data.issued_at, data.updated_at, data.created_at)),
      generatedAt: generatedDateTime(first(data.issued_at, latest && latest.paid_at, data.updated_at, data.created_at)),
      senderLines: [guest.name, guest.phone, guest.email].filter(Boolean),
      beneficiaryLines: [hotel.name, hotel.address, hotel.phone, hotel.email].filter(Boolean),
      remarkLines: [
        "Hotel booking payment" + (bookingRef ? " for " + bookingRef : ""),
        compactList([stayLine, stayDates, stayGuests], " · ")
      ].filter(Boolean),
      bookingReference: bookingRef,
      paymentReference: paymentRef,
      sessionId: txId,
      status: status,
      statusClass: statusClass(status),
      summaryLines: [
        total !== "" ? "Booking total: " + money(total) : "",
        paid !== "" ? "Amount paid: " + money(paid) : "",
        due !== "" ? "Outstanding balance: " + money(due) : ""
      ].filter(Boolean)
    };
  }

  function logoSrc(opts) {
    opts = opts || {};
    return (opts.assetPrefix || "") + "assets/icons/logo-official.svg";
  }

  function detailRows(d) {
    return [
      { label: "Transaction Amount", value: money(d.amount), opts: { valueClass: "rc-amount-main", amount: true } },
      { label: "Transaction Type", value: d.transactionType },
      { label: "Transaction Date", value: d.transactionDate },
      { label: "Sender", value: d.senderLines, opts: { mutedRest: true } },
      { label: "Beneficiary", value: d.beneficiaryLines, opts: { mutedRest: true } },
      { label: "Booking Details", value: d.remarkLines.slice(1), opts: { mutedRest: true } },
      { label: "Remark", value: d.remarkLines[0] || "Hotel booking payment" },
      { label: "Booking Reference", value: d.bookingReference },
      { label: "Transaction Reference", value: d.paymentReference },
      // Gateway session id only exists for online payments; a cash/POS/bank
      // transfer receipt must not display a fabricated one.
      d.sessionId ? { label: "Session Id", value: d.sessionId } : null,
      d.recordedBy ? { label: "Recorded By", value: d.recordedBy } : null,
      d.notes ? { label: "Notes", value: d.notes } : null,
      { label: "Transaction Status", value: d.status, opts: { valueClass: "rc-access-status" + d.statusClass, statusClass: d.statusClass } },
      d.summaryLines.length ? { label: "Payment Summary", value: d.summaryLines, opts: { mutedRest: true } } : null
    ].filter(Boolean);
  }

  function linesHtml(value, opts) {
    opts = opts || {};
    var lines = Array.isArray(value) ? value : String(value == null ? "" : value).split("\n");
    lines = lines.map(clean).filter(Boolean);
    if (!lines.length) lines = ["—"];
    return lines.map(function (line, idx) {
      var cls = "rc-value-line" + (idx > 0 && opts.mutedRest ? " rc-value-muted" : "");
      return '<span class="' + cls + '">' + esc(line) + "</span>";
    }).join("");
  }

  function row(label, value, opts) {
    opts = opts || {};
    var cls = opts.className ? " " + opts.className : "";
    return '<div class="rc-access-row' + cls + '">' +
      '<div class="rc-access-label">' + esc(label) + "</div>" +
      '<div class="rc-access-value' + (opts.valueClass ? " " + opts.valueClass : "") + '">' + linesHtml(value, opts) + "</div>" +
    "</div>";
  }

  function buildHtml(data, opts) {
    opts = opts || {};
    var d = normalize(data, opts);
    var logo = logoSrc(opts);
    var logoMarkup = '<img class="rc-logo rc-official-logo" src="' + esc(logo) + '" alt="J-ONE logo" width="28" height="52">';
    var contact = [];
    if (d.hotel.phone) {
      var phone = String(d.hotel.phone);
      contact.push('<a href="tel:' + esc(phone.replace(/\s+/g, "")) + '">' + esc(phone) + "</a>");
    }
    if (d.hotel.email) contact.push('<a href="mailto:' + esc(d.hotel.email) + '">' + esc(d.hotel.email) + "</a>");

    return '' +
      '<header class="rc-head" aria-label="Receipt brand">' +
        '<div class="rc-logo-lockup">' +
          logoMarkup +
          '<div class="rc-wordmark-wrap"><div class="rc-wordmark">J-ONE</div><div class="rc-wordmark-sub">Hotel &amp; Lodge</div></div>' +
        "</div>" +
      "</header>" +
      '<h2 class="rc-title">' + esc(d.documentTitle) + "</h2>" +
      '<p class="rc-generated">Generated from <strong>' + esc(d.sourceLabel) + "</strong> on " + esc(d.generatedAt) + "</p>" +
      '<div class="rc-access-table" role="table" aria-label="Receipt details">' +
        detailRows(d).map(function (r) { return row(r.label, r.value, r.opts || {}); }).join("") +
      "</div>" +
      '<footer class="rc-access-foot">' +
        '<p>If you have any questions or would like more information, please call ' + contact.join(" or send an email to ") + ".</p>" +
        '<p>Thank you for choosing ' + esc(d.hotel.name) + ".</p>" +
        '<div class="rc-access-channels">J-ONE HOTEL &amp; LODGE: Rooms | Bookings | Online Payment | Contact centre</div>' +
      "</footer>";
  }

  function render(element, data, opts) {
    if (!element) return;
    opts = opts || {};
    element.classList.add("rc-access");
    element.innerHTML = buildHtml(data, opts);
    element.dataset.receiptFilename = filenameFor(data, "").replace(/\.$/, "");
  }

  function filenameFor(data, ext) {
    var d = data || {};
    var latest = latestPayment(d) || {};
    var ref = first(d.receipt_reference, latest.reference, d.booking_reference, "receipt");
    ref = String(ref).replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "receipt";
    return "J-ONE-receipt-" + ref + (ext ? "." + ext.replace(/^\./, "") : "");
  }

  function downloadBlob(blob, filename) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1500);
  }

  function canvasFont(weight, size) {
    return weight + " " + size + "px ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif";
  }

  function valueLines(value) {
    var lines = Array.isArray(value) ? value : String(value == null ? "" : value).split("\n");
    lines = lines.map(clean).filter(Boolean);
    return lines.length ? lines : ["—"];
  }

  function wrapCanvasText(ctx, text, maxWidth) {
    text = clean(text) || "—";
    var words = text.split(/\s+/);
    var lines = [];
    var line = "";
    function pushLongWord(word) {
      var chunk = "";
      for (var i = 0; i < word.length; i += 1) {
        var next = chunk + word.charAt(i);
        if (chunk && ctx.measureText(next).width > maxWidth) {
          lines.push(chunk);
          chunk = word.charAt(i);
        } else {
          chunk = next;
        }
      }
      if (chunk) line = chunk;
    }
    words.forEach(function (word) {
      if (!word) return;
      var test = line ? line + " " + word : word;
      if (ctx.measureText(test).width <= maxWidth) {
        line = test;
      } else if (line) {
        lines.push(line);
        if (ctx.measureText(word).width > maxWidth) pushLongWord(word);
        else line = word;
      } else if (ctx.measureText(word).width > maxWidth) {
        pushLongWord(word);
      } else {
        line = word;
      }
    });
    if (line) lines.push(line);
    return lines.length ? lines : ["—"];
  }

  function loadCanvasImage(src) {
    return new Promise(function (resolve, reject) {
      var img = new Image();
      img.onload = function () { resolve(img); };
      img.onerror = reject;
      img.src = src;
    });
  }

  async function drawOfficialLogo(ctx, x, y, width, height, opts) {
    try {
      var img = await loadCanvasImage(logoSrc(opts));
      ctx.drawImage(img, x, y, width, height);
    } catch (_) {
      // Last-resort text fallback only if the asset fails to load; the normal
      // path always uses frontend/assets/icons/logo-official.svg.
      ctx.fillStyle = "#123f70";
      ctx.font = canvasFont("800", 22);
      ctx.fillText("J-ONE", x, y + 32);
    }
  }

  function drawWrapped(ctx, lines, x, y, maxWidth, lineHeight) {
    var cursor = y;
    lines.forEach(function (line) {
      wrapCanvasText(ctx, line, maxWidth).forEach(function (wrapped) {
        ctx.fillText(wrapped, x, cursor);
        cursor += lineHeight;
      });
    });
    return cursor;
  }

  function preparedRows(ctx, rows, labelWidth, valueWidth) {
    return rows.map(function (r) {
      ctx.font = canvasFont("800", 15);
      var labels = wrapCanvasText(ctx, r.label, labelWidth);
      ctx.font = canvasFont((r.opts && r.opts.amount) ? "800" : "650", (r.opts && r.opts.amount) ? 16 : 15);
      var values = [];
      valueLines(r.value).forEach(function (line, sourceIdx) {
        wrapCanvasText(ctx, line, valueWidth).forEach(function (wrapped) {
          values.push({ text: wrapped, muted: sourceIdx > 0 && r.opts && r.opts.mutedRest });
        });
      });
      var h = Math.max(53, Math.max(labels.length * 18, values.length * 20) + 22);
      return { raw: r, labels: labels, values: values, height: h };
    });
  }

  async function drawReceiptCanvas(data, opts) {
    opts = opts || {};
    var d = normalize(data || {}, opts);
    var rows = detailRows(d);
    var width = RECEIPT_WIDTH;
    var padX = 60;
    var contentWidth = width - padX * 2;
    var labelWidth = 225;
    var gap = 18;
    var valueWidth = contentWidth - labelWidth - gap;
    var scratch = document.createElement("canvas");
    var measure = scratch.getContext("2d");
    var rowLayouts = preparedRows(measure, rows, labelWidth, valueWidth);

    measure.font = canvasFont("400", 12);
    var contactText = "If you have any questions or would like more information, please call " +
      (d.hotel.phone || FALLBACK_HOTEL.phone) + (d.hotel.email ? " or send an email to " + d.hotel.email : "") + ".";
    var footerLines = wrapCanvasText(measure, contactText, contentWidth)
      .concat(wrapCanvasText(measure, "Thank you for choosing " + d.hotel.name + ".", contentWidth));
    var tableTop = 232;
    var tableHeight = rowLayouts.reduce(function (sum, r) { return sum + r.height; }, 0);
    var footerTop = tableTop + tableHeight + 22;
    var footerHeight = footerLines.length * 16 + 44;
    var height = Math.ceil(footerTop + footerHeight + 36);
    var scale = Math.min(EXPORT_SCALE_MAX, Math.max(EXPORT_SCALE_MIN, window.devicePixelRatio || EXPORT_SCALE_MIN));
    var canvas = document.createElement("canvas");
    canvas.width = Math.ceil(width * scale);
    canvas.height = Math.ceil(height * scale);
    canvas.joneCssWidth = width;
    canvas.joneCssHeight = height;
    var ctx = canvas.getContext("2d");
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, width, height);

    // Official J-ONE logo watermark is embedded in the exported pixels (not
    // merely CSS), so downloaded mobile receipts retain the branding.
    ctx.save();
    ctx.globalAlpha = 0.055;
    await drawOfficialLogo(ctx, width / 2 - 105, height / 2 - 195, 210, 390, opts);
    ctx.restore();

    // Header lockup uses the official J-ONE logo asset.
    await drawOfficialLogo(ctx, padX, 52, 28, 52, opts);
    ctx.fillStyle = "#143d69";
    ctx.font = canvasFont("800", 30);
    ctx.fillText("J-ONE", padX + 42, 80);
    ctx.fillStyle = "#6c6e70";
    ctx.font = canvasFont("800", 9);
    ctx.letterSpacing = "2px";
    ctx.fillText("HOTEL & LODGE", padX + 42, 98);
    ctx.letterSpacing = "0px";

    ctx.fillStyle = "#053e75";
    ctx.font = canvasFont("800", 30);
    ctx.textAlign = "center";
    ctx.fillText(d.documentTitle, width / 2, 168);
    ctx.font = canvasFont("400", 13);
    ctx.fillStyle = "#7b828a";
    ctx.fillText("Generated from " + d.sourceLabel + " on " + d.generatedAt, width / 2, 206);
    ctx.textAlign = "left";

    var y = tableTop;
    rowLayouts.forEach(function (r) {
      var labelY = y + Math.max(16, (r.height - r.labels.length * 18) / 2 + 13);
      var valueY = y + Math.max(16, (r.height - r.values.length * 20) / 2 + 14);
      ctx.font = canvasFont("800", 15);
      ctx.fillStyle = "#a66d10";
      r.labels.forEach(function (line, idx) { ctx.fillText(line, padX + 6, labelY + idx * 18); });
      r.values.forEach(function (line, idx) {
        var isAmount = r.raw.opts && r.raw.opts.amount;
        var isStatus = r.raw.opts && r.raw.opts.statusClass !== undefined;
        ctx.font = canvasFont(isAmount || isStatus ? "800" : "650", isAmount ? 16 : 15);
        if (isStatus && String(r.raw.opts.statusClass || "").indexOf("danger") !== -1) ctx.fillStyle = "#a72f28";
        else if (isStatus && String(r.raw.opts.statusClass || "").indexOf("warning") !== -1) ctx.fillStyle = "#8a5a0a";
        else if (isStatus) ctx.fillStyle = "#0b6b3a";
        else ctx.fillStyle = line.muted ? "#65707a" : "#123f70";
        ctx.fillText(line.text, padX + labelWidth + gap, valueY + idx * 20);
      });
      ctx.strokeStyle = "#e2e6ea";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padX, y + r.height + 0.5);
      ctx.lineTo(width - padX, y + r.height + 0.5);
      ctx.stroke();
      y += r.height;
    });

    ctx.fillStyle = "#707780";
    ctx.font = canvasFont("400", 12);
    drawWrapped(ctx, footerLines, padX, footerTop + 12, contentWidth, 16);
    ctx.fillStyle = "#80868d";
    ctx.font = canvasFont("400", 12);
    ctx.fillText("J-ONE HOTEL & LODGE: Rooms | Bookings | Online Payment | Contact centre", padX, height - 32);
    return canvas;
  }

  function canvasToBlob(canvas, type, quality) {
    return new Promise(function (resolve, reject) {
      canvas.toBlob(function (blob) {
        if (blob) resolve(blob);
        else reject(new Error("Unable to export this receipt in the selected format."));
      }, type, quality);
    });
  }

  function base64ToBytes(base64) {
    var binary = atob(base64);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return bytes;
  }

  function concatBytes(parts) {
    var total = parts.reduce(function (sum, part) { return sum + part.length; }, 0);
    var out = new Uint8Array(total);
    var offset = 0;
    parts.forEach(function (part) { out.set(part, offset); offset += part.length; });
    return out;
  }

  function textBytes(text) { return new TextEncoder().encode(text); }

  function jpegPdfBlob(dataUrl, imageWidth, imageHeight, pageWidthPx, pageHeightPx) {
    var jpegBytes = base64ToBytes(dataUrl.split(",")[1]);
    var pageWidth = Math.round((pageWidthPx || imageWidth) * 0.75 * 100) / 100;
    var pageHeight = Math.round((pageHeightPx || imageHeight) * 0.75 * 100) / 100;
    var content = "q\n" + pageWidth + " 0 0 " + pageHeight + " 0 0 cm\n/Im0 Do\nQ\n";
    var objects = [
      [textBytes("1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")],
      [textBytes("2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n")],
      [textBytes("3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 " + pageWidth + " " + pageHeight + "] /Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>\nendobj\n")],
      [
        textBytes("4 0 obj\n<< /Type /XObject /Subtype /Image /Width " + imageWidth + " /Height " + imageHeight + " /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length " + jpegBytes.length + " >>\nstream\n"),
        jpegBytes,
        textBytes("\nendstream\nendobj\n")
      ],
      [textBytes("5 0 obj\n<< /Length " + textBytes(content).length + " >>\nstream\n" + content + "endstream\nendobj\n")]
    ];

    var header = textBytes("%PDF-1.3\n%\xE2\xE3\xCF\xD3\n");
    var parts = [header];
    var offsets = [];
    var pos = header.length;
    objects.forEach(function (segments) {
      var objectBytes = concatBytes(segments);
      offsets.push(pos);
      parts.push(objectBytes);
      pos += objectBytes.length;
    });
    var xrefPos = pos;
    var xref = "xref\n0 6\n0000000000 65535 f \n" + offsets.map(function (offset) {
      return String(offset).padStart(10, "0") + " 00000 n ";
    }).join("\n") + "\ntrailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + xrefPos + "\n%%EOF\n";
    parts.push(textBytes(xref));
    return new Blob(parts, { type: "application/pdf" });
  }

  async function createImageBlob(data, opts) {
    var canvas = await drawReceiptCanvas(data || {}, opts || {});
    return canvasToBlob(canvas, "image/png");
  }

  async function createPDFBlob(data, opts) {
    var canvas = await drawReceiptCanvas(data || {}, opts || {});
    var dataUrl = canvas.toDataURL("image/jpeg", 0.95);
    return jpegPdfBlob(dataUrl, canvas.width, canvas.height, canvas.joneCssWidth, canvas.joneCssHeight);
  }

  async function downloadImage(element, data, opts) {
    opts = opts || {};
    var blob = await createImageBlob(data, opts);
    downloadBlob(blob, opts.filename || filenameFor(data, "png"));
  }

  async function downloadPDF(element, data, opts) {
    opts = opts || {};
    var blob = await createPDFBlob(data, opts);
    downloadBlob(blob, opts.filename || filenameFor(data, "pdf"));
  }

  async function shareBlob(blob, filename, data, opts) {
    opts = opts || {};
    var title = opts.shareTitle || "J-ONE payment receipt";
    var text = opts.shareText || "J-ONE HOTEL & LODGE payment receipt";
    if (navigator.share && typeof File !== "undefined") {
      var file = new File([blob], filename, { type: blob.type || "application/octet-stream" });
      var payload = { title: title, text: text, files: [file] };
      if (!navigator.canShare || navigator.canShare(payload)) {
        await navigator.share(payload);
        return { shared: true };
      }
    }
    downloadBlob(blob, filename);
    return { shared: false, downloaded: true };
  }

  async function shareImage(element, data, opts) {
    opts = opts || {};
    var blob = await createImageBlob(data, opts);
    return shareBlob(blob, opts.filename || filenameFor(data, "png"), data, opts);
  }

  async function sharePDF(element, data, opts) {
    opts = opts || {};
    var blob = await createPDFBlob(data, opts);
    return shareBlob(blob, opts.filename || filenameFor(data, "pdf"), data, opts);
  }

  function receiptOnlyDocument(element, data, opts) {
    opts = opts || {};
    var title = esc((data && (data.receipt_reference || data.booking_reference)) || "receipt");
    var clone = element.cloneNode(true);
    clone.classList.add("rc-access");
    var baseHref = esc(location.href.replace(/[^/]*$/, ""));
    return "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"UTF-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><base href=\"" + baseHref + "\"><title>Receipt " + title + "</title><style>" + EXPORT_CSS + "@page{margin:0;}body{display:flex;justify-content:center;align-items:flex-start;background:#fff;}</style></head><body>" + clone.outerHTML + "</body></html>";
  }

  function printReceipt(element, data, opts) {
    var win = window.open("", "_blank", "noopener,noreferrer,width=820,height=920");
    if (!win) {
      window.print();
      return;
    }
    win.document.open();
    win.document.write(receiptOnlyDocument(element, data, opts));
    win.document.close();
    setTimeout(function () { win.focus(); win.print(); }, 350);
  }

  window.JONE = window.JONE || {};
  window.JONE.receipts = {
    buildHtml: buildHtml,
    render: render,
    normalize: normalize,
    filenameFor: filenameFor,
    createImageBlob: createImageBlob,
    createPDFBlob: createPDFBlob,
    downloadImage: downloadImage,
    downloadPDF: downloadPDF,
    shareImage: shareImage,
    sharePDF: sharePDF,
    printReceipt: printReceipt,
    exportCss: function () { return EXPORT_CSS; }
  };
})();
