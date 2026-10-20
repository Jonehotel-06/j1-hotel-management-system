/* js/theme.js */
/* ==========================================================================
   Theme system — light / dark, persisted, flash-guarded, system-aware.
   ========================================================================== */

(function () {
  "use strict";

  const KEY = (window.APP_CONFIG && window.APP_CONFIG.STORAGE.THEME) || "jone.theme";
  const root = document.documentElement;

  function resolve() {
    const saved = JONE.storage.get(KEY, null);
    if (saved === "dark" || saved === "light") return saved;
    if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) return "dark";
    return "light";
  }

  function apply(theme) {
    root.setAttribute("data-theme", theme);
    JONE.storage.set(KEY, theme);
    // Keep toggle icon state consistent via a [data-theme-icon] attribute/aria.
    const toggles = document.querySelectorAll(".theme-toggle");
    toggles.forEach((t) => {
      t.setAttribute("aria-label", theme === "dark" ? "Switch to light mode" : "Switch to dark mode");
      t.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
    });
  }

  function toggle(btn) {
    const current = root.getAttribute("data-theme") || resolve();
    apply(current === "dark" ? "light" : "dark");
    // Polished one-rotation cue on the pressed icon.
    if (btn) {
      btn.classList.remove("spin");
      void btn.offsetWidth;           // restart the animation
      btn.classList.add("spin");
      setTimeout(() => btn.classList.remove("spin"), 520);
    }
  }

  function init() {
    // Remove preload guard (set in <head> to avoid flash) then apply stored/system theme.
    root.classList.remove("preload-theme");
    const t = resolve();
    root.setAttribute("data-theme", t);

    document.addEventListener("click", (e) => {
      const btn = e.target.closest(".theme-toggle");
      if (btn) toggle(btn);
    });

    if (window.matchMedia) {
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
        // Only follow the OS when user hasn't explicitly chosen.
        if (!JONE.storage.get(KEY, null)) apply(e.matches ? "dark" : "light");
      });
    }
  }

  window.JONE.theme = { init, apply, toggle, resolve };
})();
