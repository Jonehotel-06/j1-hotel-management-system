/* js/site.js */
/* ==========================================================================
   site.js — public-site page controllers (homepage, rooms, gallery, etc.)
   Shared DOM hooks that most public pages rely on. Kept lean.
   ========================================================================== */

(function () {
  "use strict";

  /* -------------------- Booking date field validation --------------------- */
  // Enforce check-out > check-in and min stay on paired date inputs.
  function initDatePair(checkInSel, checkOutSel, errors) {
    const ci = document.querySelector(checkInSel);
    const co = document.querySelector(checkOutSel);
    if (!ci) return;
    const applyMin = () => {
      if (!co || !ci.value) return;
      const min = (JONE.APP_CONFIG && JONE.APP_CONFIG.MIN_STAY_NIGHTS) || 1;
      const start = JONE.hotelTodayISO(0);
      ci.min = start;
      // checkout min = checkin + min nights
      const d = JONE.parseISO(ci.value);
      if (d) {
        d.setUTCDate(d.getUTCDate() + min);
        co.min = d.toISOString().slice(0, 10);
      }
    };
    ci.addEventListener("change", () => {
      applyMin();
      const min = (JONE.APP_CONFIG && JONE.APP_CONFIG.MIN_STAY_NIGHTS) || 1;
      if (co && co.value && JONE.nightsBetween(ci.value, co.value) < min) {
        co.value = "";
        if (typeof errors === "function") errors("Check-out must be after check-in.");
      }
    });
    applyMin();

    const sync = () => {
      if (ci && co && ci.value && co.value && JONE.nightsBetween(ci.value, co.value) < 1) {
        if (typeof errors === "function") errors("Check-out must be after check-in.");
      }
    };
    co && co.addEventListener("change", sync);
  }

  /* ------------------------- Availability search hook --------------------- */
  // data-availability-form: on submit, read fields, delegate to API, then
  // navigate to booking.html with the query preserved (room selection step).
  function initAvailabilityForms() {
    document.querySelectorAll("[data-availability-form]").forEach((form) => {
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        const btn = form.querySelector("[type=submit]");
        let params = {};
        const read = (name) => {
          const el = form.querySelector(`[name=${name}]`);
          return el ? el.value : "";
        };
        // Availability is authoritative backend data — never answered offline.
        if (window.JONE && JONE.pwa && !JONE.pwa.requireOnline("booking")) return;
        const ci = read("check_in"), co = read("check_out");
        if (!ci || !co) {
          JONE.ui.toast("Please choose your check-in and check-out dates.", "warning");
          return;
        }
        if (JONE.nightsBetween(ci, co) < 1) {
          JONE.ui.toast("Check-out must be a later date than check-in.", "error");
          return;
        }
        params = {
          check_in: ci,
          check_out: co,
          adults: read("adults") || JONE.APP_CONFIG.DEFAULT_ADULTS,
          children: read("children") || JONE.APP_CONFIG.DEFAULT_CHILDREN
        };
        const roomType = read("room_type");
        if (roomType) params.room_type = roomType;

        if (btn && JONE.guardSubmit && !JONE.guardSubmit(btn)) return;

        // Persist a lightweight search draft so booking pre-fills it.
        JONE.storage.set(JONE.APP_CONFIG.STORAGE.BOOKING, params);
        const qs = new URLSearchParams(params).toString();
        setTimeout(() => { location.href = "booking.html?" + qs; }, 250);
      });
    });
  }

  /* --------------------------- Reveal on scroll --------------------------- */
  let observers = null;
  function initReveal() {
    if (!("IntersectionObserver" in window)) return;
    observers = new IntersectionObserver((entries) => {
      entries.forEach((en) => {
        if (en.isIntersecting) {
          en.target.classList.add("is-visible");
          observers.unobserve(en.target);
        }
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
    document.querySelectorAll("[data-reveal]").forEach((n) => observers.observe(n));
  }

  /* -------------------------------- Boot ---------------------------------- */
  // NOTE: the contact/enquiry form is owned by js/contact.js (the page loads
  // it explicitly). Binding it here as well attached a SECOND submit listener
  // to the same form, so one click fired two POSTs and created two enquiries.
  // There is exactly one enquiry submission path — see js/contact.js.
  document.addEventListener("DOMContentLoaded", () => {
    initAvailabilityForms();
    initReveal();
    initDatePair("[name=check_in]", "[name=check_out]");
  });
})();
