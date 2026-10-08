/* js/receipt-email-modal.js */
/* ==========================================================================
   J-ONE HOTEL & LODGE — shared "Email guest" receipt modal.

   ONE implementation of the staff receipt-send experience, used by both
   dashboard/receipt-details.html and dashboard/receipts.html so the flow is
   identical everywhere:

     Click "Email Guest"
       → modal opens IMMEDIATELY (never a silent or delayed reaction)
       → guest / booking details are displayed
       → "Send receipt" → visible "Sending receipt…" state + spinner
       → duplicate submission disabled while in flight
       → API request — the backend now delivers SYNCHRONOUSLY: it performs
         the SMTP send during the request and answers only with the REAL
         outcome
       → "Receipt sent successfully" (only when the mail server actually
         accepted the message) or an honest failure with a Retry button

   Delivery status wording is deliberately precise:
     SENT    → "Receipt sent"
     FAILED  → "The email could not be sent. Please retry."
   (There is no "queued" state in this flow any more.)
   ========================================================================== */

(function () {
  "use strict";

  window.JONE = window.JONE || {};

  function open(opts) {
    opts = opts || {};
    var bookingReference = opts.bookingReference || "";
    var guestName = opts.guestName || "Guest";
    var guestEmail = opts.guestEmail || "";
    if (!bookingReference || !guestEmail) return;

    var status = JONE.el("p", {
      class: "muted", role: "status", "aria-live": "polite",
      textContent: "Ready to send."
    });
    var sendBtn = JONE.el("button", {
      class: "btn btn-accent", type: "button", textContent: "Send receipt"
    });
    var closeBtn = JONE.el("button", {
      class: "btn btn-outline", type: "button", textContent: "Close"
    });

    var summary = JONE.el("dl", { class: "receipt-send-summary" },
      JONE.el("dt", { textContent: "Guest" }),
      JONE.el("dd", { textContent: guestName }),
      JONE.el("dt", { textContent: "Email" }),
      JONE.el("dd", { textContent: guestEmail }),
      JONE.el("dt", { textContent: "Booking" }),
      JONE.el("dd", { textContent: bookingReference }),
      JONE.el("dt", { textContent: "Document" }),
      JONE.el("dd", { textContent: "Payment receipt (PDF attached)" })
    );

    var body = JONE.el("div", { class: "receipt-send-dialog" },
      JONE.el("p", { textContent: "Send payment receipt" }),
      summary,
      status
    );
    var footer = JONE.el("div", { class: "modal-actions" }, closeBtn, sendBtn);

    JONE.ui.modal.open({
      title: "Send payment receipt",
      body: body,
      footer: footer,
      size: "modal-md"
    });

    closeBtn.addEventListener("click", function () { JONE.ui.modal.close(); });

    function setBusy(busy, label) {
      sendBtn.disabled = busy;
      closeBtn.disabled = busy;
      if (busy) {
        sendBtn.setAttribute("aria-busy", "true");
        sendBtn.innerHTML = '<span class="spinner" aria-hidden="true"></span> ' + (label || "Sending receipt…");
      } else {
        sendBtn.removeAttribute("aria-busy");
      }
    }

    function succeed() {
      status.textContent = "Receipt sent — the mail server accepted the message for " + guestEmail + ".";
      sendBtn.onclick = function () { JONE.ui.modal.close(); };
      JONE.ui.toast("Email sent successfully.", "success");
    }

    function fail(err) {
      var reason = (err && err.message) || "the email service could not be reached. Please try again.";
      status.textContent = /\.$/.test(reason) ? reason : "The email could not be sent. Please retry.";
      JONE.ui.toast("Email could not be sent. Please retry.", "error", { assertive: true });
    }

    sendBtn.addEventListener("click", async function () {
      if (sendBtn.disabled) return;             // duplicate-submit guard
      setBusy(true);
      status.textContent = "Sending receipt…";
      var finalLabel = "Send receipt";
      try {
        var res = await window.API.sendReceipt(bookingReference);
        var payload = (res && res.data) || {};
        if (payload.status === "SENT") {
          succeed();
          finalLabel = "Done";
        } else if (payload.status === "IN_PROGRESS") {
          status.textContent = "A receipt for this booking is already being processed.";
          sendBtn.textContent = "Done";
          sendBtn.onclick = function () { JONE.ui.modal.close(); };
          finalLabel = "Done";
        } else {
          fail({ message: payload.error || "delivery failed at the mail server." });
          finalLabel = "Retry";
        }
      } catch (err) {
        fail(err);
        finalLabel = "Retry";
      }
      // Always end in a usable state — never a stuck disabled modal.
      sendBtn.disabled = false;
      closeBtn.disabled = false;
      sendBtn.removeAttribute("aria-busy");
      sendBtn.textContent = finalLabel;
    });
  }

  window.JONE.receiptEmailModal = { open: open };
})();
