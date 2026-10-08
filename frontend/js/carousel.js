/* js/carousel.js */
/* ==========================================================================
   carousel.js — homepage hero slideshow.
   Auto-advances with a slow cross-fade, pauses on hover/focus/tab-hidden,
   supports arrows, dots, touch swipe, and honors prefers-reduced-motion.
   ========================================================================== */

(function () {
  "use strict";

  const AUTOPLAY_MS = 6500;

  function build(root) {
    const slides = Array.from(root.querySelectorAll(".hero-slide"));
    if (slides.length === 0) return;

    const media = root.querySelector(".hero-media");
    let dotsWrap = root.querySelector(".hero-dots");
    const prevBtn = root.querySelector(".hero-arrow.prev");
    const nextBtn = root.querySelector(".hero-arrow.next");

    // Single-slide hero degrades to a static hero (no controls, no timer).
    if (slides.length < 2) {
      slides.forEach((s, i) => s.classList.toggle("is-active", i === 0));
      if (dotsWrap) dotsWrap.remove();
      if (prevBtn) prevBtn.remove();
      if (nextBtn) nextBtn.remove();
      return;
    }

    // Dots are generated from the slides so they can never fall out of sync.
    if (!dotsWrap) {
      dotsWrap = document.createElement("div");
      dotsWrap.className = "hero-dots";
      dotsWrap.setAttribute("role", "tablist");
      dotsWrap.setAttribute("aria-label", "Hero images");
      root.appendChild(dotsWrap);
    }
    dotsWrap.innerHTML = "";
    const dots = slides.map((_, i) => {
      const b = document.createElement("button");
      b.type = "button";
      b.setAttribute("role", "tab");
      b.setAttribute("aria-label", "Show image " + (i + 1) + " of " + slides.length);
      b.addEventListener("click", () => { go(i); restart(); });
      dotsWrap.appendChild(b);
      return b;
    });

    let index = 0;
    let timer = null;
    let paused = false;
    const reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    function go(i) {
      index = (i + slides.length) % slides.length;
      slides.forEach((s, n) => {
        s.classList.toggle("is-active", n === index);
        s.setAttribute("aria-hidden", n === index ? "false" : "true");
      });
      dots.forEach((d, n) => {
        d.classList.toggle("is-active", n === index);
        d.setAttribute("aria-selected", n === index ? "true" : "false");
      });
    }

    function stop() { if (timer) { clearInterval(timer); timer = null; } }
    function start() {
      stop();
      if (reduced || paused || document.hidden) return;
      timer = setInterval(() => go(index + 1), AUTOPLAY_MS);
    }
    // Any deliberate interaction resumes autoplay (clears a lingering
    // focus/hover pause so the show never stalls after using the controls).
    function restart() { paused = false; start(); }

    if (prevBtn) prevBtn.addEventListener("click", () => { go(index - 1); restart(); });
    if (nextBtn) nextBtn.addEventListener("click", () => { go(index + 1); restart(); });

    // Pause while the visitor is reading / interacting.
    root.addEventListener("mouseenter", () => { paused = true; stop(); });
    root.addEventListener("mouseleave", () => { paused = false; start(); });
    root.addEventListener("focusin", () => { paused = true; stop(); });
    root.addEventListener("focusout", () => { paused = false; start(); });
    document.addEventListener("visibilitychange", () => { document.hidden ? stop() : start(); });

    // Touch swipe (also covers trackpads that emit touch events).
    let touchX = null;
    root.addEventListener("touchstart", (e) => { touchX = e.touches[0] ? e.touches[0].clientX : null; }, { passive: true });
    root.addEventListener("touchend", (e) => {
      if (touchX == null || !e.changedTouches[0]) return;
      const dx = e.changedTouches[0].clientX - touchX;
      if (Math.abs(dx) > 48) { go(dx < 0 ? index + 1 : index - 1); start(); }
      touchX = null;
    }, { passive: true });

    // Keyboard arrows when the carousel contains focus.
    root.addEventListener("keydown", (e) => {
      if (e.key === "ArrowLeft") { go(index - 1); start(); }
      if (e.key === "ArrowRight") { go(index + 1); start(); }
    });

    go(0);
    start();
    if (media) media.setAttribute("aria-live", "off");
  }

  function init(root) {
    (root ? [root] : document.querySelectorAll("[data-hero-carousel]")).forEach(build);
  }

  window.JONE = window.JONE || {};
  window.JONE.carousel = { init };
  document.addEventListener("DOMContentLoaded", () => init());
})();
