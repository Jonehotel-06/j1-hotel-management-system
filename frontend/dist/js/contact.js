/* js/contact.js */
/* ============================================================================
   contact.js — public contact/enquiry/cancellation-request submission path.

   Cancellation/refund requests are collected as structured enquiries only.
   Guests are never allowed to cancel paid/confirmed bookings or trigger refunds
   directly from the browser. The backend links references best-effort, staff
   review the request, and Paystack refund completion is provider-confirmed.
   ========================================================================== */

(function () {
  "use strict";

  var form = document.querySelector("[data-contact-form]");
  if (!form) return;

  var btn = form.querySelector("[type=submit]");
  var errBox = form.querySelector("[data-form-error]");
  var cancellationBox = form.querySelector("[data-cancellation-fields]");
  var subjectEl = form.querySelector("[name=subject]");
  var submitting = false;
  var NETWORK_MESSAGE = "We couldn't reach the hotel right now. Please try again.";

  function showError(msg) {
    if (errBox) {
      errBox.textContent = msg;
      errBox.style.display = "block";
      errBox.setAttribute("role", "alert");
    }
  }

  function clearError() {
    if (errBox) {
      errBox.textContent = "";
      errBox.style.display = "none";
      errBox.removeAttribute("role");
    }
  }

  function isCancellationSubject(value) {
    return /cancel|refund/i.test(String(value || ""));
  }

  function syncCancellationFields() {
    if (!cancellationBox || !subjectEl) return;
    var active = isCancellationSubject(subjectEl.value);
    cancellationBox.hidden = !active;
    cancellationBox.querySelectorAll("input, textarea, select").forEach(function (el) {
      el.disabled = !active;
    });
  }

  function prefillFromQuery() {
    var q = new URLSearchParams(window.location.search || "");
    var type = q.get("type") || "";
    if (/cancel|refund/i.test(type) && subjectEl) subjectEl.value = "Cancellation / refund request";
    var map = {
      name: "name",
      email: "email",
      phone: "phone",
      booking_reference: "booking_reference",
      payment_reference: "payment_reference",
      receipt_reference: "receipt_reference"
    };
    Object.keys(map).forEach(function (k) {
      var v = q.get(k);
      if (v == null) return;
      var el = form.querySelector('[name="' + map[k] + '"]');
      if (el) el.value = v;
    });
    syncCancellationFields();
  }

  /* The form carries `novalidate`, so the checks below are the real UX gate.
     The backend serializer remains authoritative and may return stricter field
     messages, which are surfaced unchanged. */
  function validate(values) {
    if (!values.name) return "Please enter your name.";
    if (!values.email) return "Please enter your email address.";
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(values.email)) return "Please enter a valid email address.";
    if (!values.subject) return "Please choose a subject.";
    if (values.enquiry_type === "CANCELLATION") {
      if (!values.booking_reference) return "Please enter your booking reference for a cancellation/refund request.";
      if (!values.cancellation_reason && !values.message) return "Please tell us why you want to cancel.";
    }
    if (!values.message) return "Please write your message.";
    if (values.message.length < 5) return "Please enter a more detailed message.";
    return null;
  }

  function setBusy(busy) {
    if (!btn) return;
    if (busy) {
      btn.dataset.originalLabel = btn.dataset.originalLabel || btn.textContent;
      btn.disabled = true;
      btn.setAttribute("aria-busy", "true");
      btn.classList.add("is-loading");
      btn.textContent = isCancellationSubject(subjectEl && subjectEl.value) ? "Submitting request…" : "Sending…";
    } else {
      btn.disabled = false;
      btn.removeAttribute("aria-busy");
      btn.classList.remove("is-loading");
      if (btn.dataset.originalLabel) btn.textContent = btn.dataset.originalLabel;
    }
  }

  function field(fd, name) { return (fd.get(name) || "").toString().trim(); }

  if (subjectEl) subjectEl.addEventListener("change", syncCancellationFields);
  prefillFromQuery();

  form.addEventListener("submit", async function (e) {
    e.preventDefault();
    if (submitting) return;
    clearError();

    var fd = new FormData(form);
    var cancellation = isCancellationSubject(field(fd, "subject"));
    var payload = {
      name: field(fd, "name"),
      email: field(fd, "email"),
      phone: field(fd, "phone"),
      subject: cancellation ? "Cancellation / refund request" : field(fd, "subject"),
      message: field(fd, "message"),
      website: field(fd, "website"),
      enquiry_type: cancellation ? "CANCELLATION" : "GENERAL"
    };
    if (cancellation) {
      payload.booking_reference = field(fd, "booking_reference");
      payload.payment_reference = field(fd, "payment_reference");
      payload.receipt_reference = field(fd, "receipt_reference");
      payload.cancellation_reason = field(fd, "cancellation_reason");
      payload.preferred_contact_method = field(fd, "preferred_contact_method") || "EMAIL";
      payload.refund_requested = !!fd.get("refund_requested");
    }

    var problem = validate(payload);
    if (problem) {
      showError(problem);
      var targetName = !payload.name ? "name" : !payload.email ? "email" : !payload.subject ? "subject" : (cancellation && !payload.booking_reference ? "booking_reference" : "message");
      var firstBad = form.querySelector('[name="' + targetName + '"]');
      if (firstBad && firstBad.focus) firstBad.focus();
      return;
    }

    submitting = true;
    setBusy(true);
    try {
      var res = cancellation
        ? await window.API.submitCancellationRequest(payload)
        : await window.API.submitEnquiry(payload);
      var data = (res && res.data) || {};
      if (cancellation) {
        if (data.status_url) {
          try { sessionStorage.setItem("jone.cancellation_status_url", data.status_url); } catch (_) {}
        }
        JONE.ui.toast("Cancellation request submitted", "success", {
          title: "Request received",
          message: "Your booking has not been cancelled yet. Staff will review it and update you."
        });
        if (data.status_url) {
          window.location.href = data.status_url;
          return;
        }
      } else {
        JONE.ui.toast("Message sent successfully", "success", {
          title: "Thank you",
          message: "We've received your message and will be in touch soon."
        });
      }
      form.reset();
      syncCancellationFields();
      clearError();
    } catch (err) {
      var status = err && err.status;
      var message = status === 0
        ? NETWORK_MESSAGE
        : ((err && err.message) || "We couldn't send your message. Please try again.");
      showError(message);
      if (status === 0) JONE.ui.toast(message, "error", { assertive: true });
    } finally {
      submitting = false;
      setBusy(false);
    }
  });
})();
