/* js/booking-stepper.js */
/* ==========================================================================
   Booking stepper — a reusable, accessible six-stage progress component for
   the J-ONE booking flow. Renders a polished horizontal stepper on desktop
   and a compact "step X of N" progress bar on mobile from ONE data source.

   It never changes booking state or talks to the API — it only visualises
   progress and lets guests jump BACK to completed steps. Forward jumps into
   incomplete steps are deliberately impossible (prevents inconsistent state).

   Usage:
     var stepper = JONE.stepper.mount(el, {
       current: 3,                 // 1-based active step
       criteria: {...},            // stay/room criteria used for back-nav URLs
       onNavigate: function (step, index) {
         // return true if handled in-page (panel switch); falsy => default
         // cross-page navigation using `criteria`.
       }
     });
     stepper.update(6);            // move the active step (e.g. after payment)
   ========================================================================== */
(function () {
  "use strict";

  var STEPS = [
    { key: "stay",         label: "Stay",         title: "Choose your stay",   page: "booking.html" },
    { key: "room",         label: "Room",         title: "Select a room",      page: "booking.html" },
    { key: "guest",        label: "Guest",        title: "Guest information",  page: "booking-review.html" },
    { key: "review",       label: "Review",       title: "Review your booking", page: "booking-review.html" },
    { key: "payment",      label: "Payment",      title: "Secure payment",     page: "booking-confirmation.html" },
    { key: "confirmation", label: "Confirmation", title: "Confirmation",       page: "booking-confirmation.html" }
  ];

  function checkSvg() {
    return (window.JONE && JONE.icons && JONE.icons.get) ? JONE.icons.get("check") : "\u2713";
  }

  // Build a back-navigation URL for a completed step, carrying the known
  // criteria so the destination page hydrates without losing information.
  function backUrl(step, criteria) {
    var c = criteria || {};
    var q = new URLSearchParams();
    if (c.check_in) q.set("check_in", c.check_in);
    if (c.check_out) q.set("check_out", c.check_out);
    if (c.adults != null) q.set("adults", c.adults);
    if (c.children != null) q.set("children", c.children);
    // Room quantity travels with the criteria so back-navigation never
    // silently drops a multi-room selection (only sent when > 1).
    if (c.rooms != null && Number(c.rooms) > 1) q.set("rooms", c.rooms);
    if (c.room) q.set("room", c.room);
    if (c.room_name) q.set("room_name", c.room_name);
    if (c.offer) q.set("offer", c.offer);

    if (step.key === "stay")   { return "booking.html?" + q.toString(); }
    if (step.key === "room")   { q.set("step", "room"); return "booking.html?" + q.toString(); }
    if (step.key === "guest")  { return "booking-review.html?" + q.toString(); }
    if (step.key === "review") { q.set("step", "review"); return "booking-review.html?" + q.toString(); }
    if (step.key === "payment"){ q.set("step", "payment"); return "booking-confirmation.html?" + q.toString(); }
    return step.page;
  }

  function mount(el, opts) {
    if (!el) return { update: function () {} };
    opts = opts || {};
    var current = Math.min(Math.max(1, opts.current || 1), STEPS.length);
    var criteria = opts.criteria || {};
    var onNavigate = typeof opts.onNavigate === "function" ? opts.onNavigate : null;

    // Persistent scaffold (built once); state is toggled via classes so the
    // DOM is never rebuilt on step changes.
    el.classList.add("bk-stepper-shell");
    el.setAttribute("aria-label", "Booking progress");

    var desktop = document.createElement("ol");
    desktop.className = "bk-stepper";
    desktop.setAttribute("role", "list");

    var nodes = [];
    STEPS.forEach(function (step, i) {
      if (i > 0) {
        var line = document.createElement("li");
        line.className = "bk-stepline";
        line.setAttribute("aria-hidden", "true");
        desktop.appendChild(line);
        nodes.push({ line: line });
      }
      var li = document.createElement("li");
      li.className = "bk-step";
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "bk-step-btn";
      btn.innerHTML =
        '<span class="bk-step-marker"><span class="bk-step-num">' + (i + 1) + '</span>' +
        '<span class="bk-step-check" aria-hidden="true">' + checkSvg() + '</span></span>' +
        '<span class="bk-step-label">' + step.label + '</span>';
      li.appendChild(btn);
      desktop.appendChild(li);
      var rec = nodes[nodes.length - 1] || {};
      // attach the step record after its (optional) preceding line
      nodes.push({ li: li, btn: btn, step: step, index: i + 1 });
      btn.addEventListener("click", function () { navigate(step, i + 1); });
    });

    // Compact mobile progress
    var mobile = document.createElement("div");
    mobile.className = "bk-progress";
    mobile.innerHTML =
      '<div class="bk-progress-top">' +
        '<span class="bk-progress-count"></span>' +
        '<span class="bk-progress-dots" aria-hidden="true"></span>' +
      '</div>' +
      '<div class="bk-progress-track"><span class="bk-progress-fill"></span></div>' +
      '<div class="bk-progress-title"></div>';

    el.appendChild(desktop);
    el.appendChild(mobile);

    function stepRecords() {
      return nodes.filter(function (n) { return n.btn; });
    }

    function navigate(step, index) {
      if (index >= current) return;                 // never jump forward / to self
      if (onNavigate && onNavigate(step, index)) return;
      window.location.href = backUrl(step, criteria);
    }

    function render() {
      stepRecords().forEach(function (rec) {
        var i = rec.index;
        var done = i < current;
        var active = i === current;
        rec.li.classList.toggle("is-done", done);
        rec.li.classList.toggle("is-active", active);
        rec.li.classList.toggle("is-upcoming", i > current);
        rec.btn.disabled = i >= current;            // upcoming + current are non-nav
        if (active) rec.btn.setAttribute("aria-current", "step");
        else rec.btn.removeAttribute("aria-current");
        var sr = done ? " (completed)" : active ? " (current step)" : " (upcoming)";
        rec.btn.setAttribute("aria-label", "Step " + i + ": " + rec.step.label + sr);
      });
      // connector lines: done when the step AFTER them is reached
      nodes.filter(function (n) { return n.line; }).forEach(function (n, idx) {
        n.line.classList.toggle("is-done", (idx + 1) < current);
      });

      var meta = STEPS[current - 1];
      mobile.querySelector(".bk-progress-count").textContent = "Step " + current + " of " + STEPS.length;
      mobile.querySelector(".bk-progress-title").textContent = meta.title;
      mobile.querySelector(".bk-progress-fill").style.width = ((current - 1) / (STEPS.length - 1) * 100) + "%";
      var dots = STEPS.map(function (s, i) {
        var n = i + 1;
        return n < current ? "\u2713" : n === current ? "\u25CF" : "\u25CB";
      }).join(" ");
      mobile.querySelector(".bk-progress-dots").textContent = dots;
    }

    render();

    return {
      update: function (n) { current = Math.min(Math.max(1, n || 1), STEPS.length); render(); },
      get current() { return current; },
      steps: STEPS
    };
  }

  window.JONE = window.JONE || {};
  window.JONE.stepper = { mount: mount, STEPS: STEPS };
})();
