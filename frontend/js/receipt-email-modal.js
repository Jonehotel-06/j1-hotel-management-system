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

   ---------------------------------------------------------------------------
   ONE CLICK = AT MOST ONE EMAIL (a guest must never receive a receipt twice)

   The send button is a send button ONLY until the send is settled. Once a send
   is confirmed the click listener is REMOVED and replaced by the close
   listener, so the button that reads "Done" cannot reach the send path at all.
   This is deliberately stronger than re-labelling plus an `if (done) return`
   guard: the earlier build re-labelled the button and overwrote `onclick`
   while the `addEventListener` send handler stayed attached, so a single click
   on "Done" closed the modal AND fired a second real email.

   Four independent defences, in order:

     1. the send listener is removed when the send settles (not merely
        guarded), and `removeEventListener` is used on a named function —
        never an inline handler that could coexist with a listener;
     2. `finished` / `inFlight` / `disabled` guards, so a queued second click,
        a double tap or a slow response can never start a second request;
     3. a page-session registry of bookings whose receipt has already gone out
        (SENT_THIS_SESSION). Re-opening the modal for such a booking never
        looks like a first send: it opens in an explicit "already sent" state
        whose button reads "Send another copy", so a deliberate press is
        required. It also neutralises a STALE modal — one opened before
        another instance sent — which would otherwise still be holding a live
        "Send receipt" button;
     4. a request idempotency key that is kept for EVERY attempt of the same
        send (including retries), so if a previous attempt actually reached
        the mail server but the answer was lost, the retry is recognised as a
        duplicate server-side instead of mailing the guest twice.

   The page-session registry is per page load on purpose: it stops accidental
   repeats caused by leaving a modal open / re-opening it, without ever
   preventing staff from deliberately sending a fresh copy after a reload.
   ========================================================================== */

(function () {
  "use strict";

  window.JONE = window.JONE || {};

  /* Bookings whose receipt has already been confirmed sent in THIS page load:
     { bookingReference: recipientEmail }. */
  var SENT_THIS_SESSION = Object.create(null);

  function open(opts) {
    opts = opts || {};
    var bookingReference = opts.bookingReference || "";
    var guestName = opts.guestName || "Guest";
    var guestEmail = opts.guestEmail || "";
    if (!bookingReference || !guestEmail) return;

    /* Opening for a booking that already had its receipt sent in this session
       is a resend, and says so — it never masquerades as a first send. */
    var alreadySentEmail = SENT_THIS_SESSION[bookingReference] || "";
    var resendIntent = !!alreadySentEmail;

    var status = JONE.el("p", {
      class: "muted", role: "status", "aria-live": "polite",
      textContent: alreadySentEmail
        ? "A receipt was already sent to " + alreadySentEmail + " in this session."
        : "Ready to send."
    });
    var sendBtn = JONE.el("button", {
      class: "btn btn-accent", type: "button",
      textContent: alreadySentEmail ? "Send another copy" : "Send receipt"
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
      size: "modal-md",
      /* Dismissing the dialog (Esc / X / backdrop / Close) unhooks the send
         path immediately — a closed modal can never be clicked by accident. */
      onClose: function () {
        sendBtn.removeEventListener("click", onSend);
        sendBtn.removeEventListener("click", onDone);
      }
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

    var inFlight = false;
    var finished = false;      // a send for this dialog is settled — never send again
    function newIdempotencyKey() {
      return "rem-" + bookingReference + "-" + Date.now() + "-" + Math.random().toString(36).slice(2, 10);
    }
    /* ONE key per attempt-series: it is kept across retries so a retry after a
       lost answer is de-duplicated by the backend instead of re-mailing the
       guest. A genuinely new send (a fresh modal) gets a fresh key. */
    var idempotencyKey = newIdempotencyKey();

    /* The settled state: "Done" closes the dialog and is not wired to send. */
    function switchToDone(label) {
      finished = true;
      inFlight = false;
      sendBtn.removeEventListener("click", onSend);
      sendBtn.addEventListener("click", onDone);
      sendBtn.disabled = false;
      closeBtn.disabled = false;
      sendBtn.removeAttribute("aria-busy");
      sendBtn.textContent = label || "Done";
    }

    /* An honest failure stays retryable — the same button sends again, on
       purpose, with the same idempotency key. */
    function stayRetryable() {
      inFlight = false;
      sendBtn.disabled = false;
      closeBtn.disabled = false;
      sendBtn.removeAttribute("aria-busy");
      sendBtn.textContent = "Retry";
    }

    function succeed() {
      SENT_THIS_SESSION[bookingReference] = guestEmail;
      status.textContent = "Receipt sent — the mail server accepted the message for " + guestEmail + ".";
      JONE.ui.toast("Email sent successfully.", "success");
    }

    function fail(err) {
      var reason = (err && err.message) || "the email service could not be reached. Please try again.";
      status.textContent = /\.$/.test(reason) ? reason : "The email could not be sent. Please retry.";
      JONE.ui.toast("Email could not be sent. Please retry.", "error", { assertive: true });
    }

    function onDone() {
      JONE.ui.modal.close();
    }

    async function onSend() {
      if (finished || inFlight || sendBtn.disabled) return; // duplicate-submit guard
      /* A stale dialog: this one opened as a first send, but another instance
         already mailed the guest during this session. Never send again — say
         so and turn into the close-only ("Done") state. */
      if (!resendIntent && SENT_THIS_SESSION[bookingReference]) {
        var sentTo = SENT_THIS_SESSION[bookingReference];
        status.textContent = "A receipt was already sent to " + sentTo + " in this session.";
        JONE.ui.toast("This receipt was already emailed — no second copy was sent.", "info");
        switchToDone("Done");
        return;
      }
      inFlight = true;
      setBusy(true);
      status.textContent = "Sending receipt…";
      try {
        var res = await window.API.sendReceipt(bookingReference, { idempotency_key: idempotencyKey });
        var payload = (res && res.data) || {};
        if (payload.status === "SENT") {
          succeed();
          switchToDone("Done");
        } else if (payload.status === "IN_PROGRESS") {
          // The backend already has this receipt in flight (another device or a
          // previous click): nothing new was mailed, so this is settled too.
          SENT_THIS_SESSION[bookingReference] = guestEmail;
          status.textContent = "A receipt for this booking is already being processed.";
          switchToDone("Done");
        } else {
          fail({ message: payload.error || "delivery failed at the mail server." });
          stayRetryable();
        }
      } catch (err) {
        fail(err);
        stayRetryable();
      }
    }

    sendBtn.addEventListener("click", onSend);
  }

  window.JONE.receiptEmailModal = { open: open };
})();
