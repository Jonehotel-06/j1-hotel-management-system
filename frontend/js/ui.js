/* js/ui.js */
/* ==========================================================================
   UI layer — toast, modal, confirmation, lightbox, accordions, drop-in icons.
   ========================================================================== */

(function () {
  "use strict";

  /* -------------------------------- Toast --------------------------------- */
  function ensureToastRegion() {
    let region = document.querySelector(".toast-region");
    if (!region) {
      region = JONE.el("div", { class: "toast-region" });
      region.setAttribute("role", "status");
      region.setAttribute("aria-live", "polite");
      document.body.appendChild(region);
    }
    return region;
  }

  const TOAST_ICONS = { success: "checkCircle", error: "alertCircle", warning: "alertTriangle", info: "info" };

  function toast(message, type = "info", opts = {}) {
    const region = ensureToastRegion();
    const icon = JONE.icons.get(TOAST_ICONS[type] || "info");

    // Optional richer layout: a bold title above the message. Used by the
    // booking flow for the "Payment successful" confirmation — same component,
    // same region, just an extra line. Falls back to the plain single-line
    // toast when no title is supplied.
    let body;
    if (opts.title) {
      body = JONE.el("span", { class: "toast-body" },
        JONE.el("span", { class: "toast-title", textContent: opts.title }),
        message ? JONE.el("span", { class: "toast-message", textContent: message }) : null
      );
    } else {
      body = JONE.el("span", { class: "toast-message", textContent: message });
    }

    const node = JONE.el("div", { class: `toast ${type}${opts.title ? " toast-rich" : ""}` },
      JONE.el("span", { class: "toast-icon", innerHTML: icon }),
      body,
      JONE.el("button", {
        class: "toast-close", "aria-label": "Dismiss",
        innerHTML: JONE.icons.get("x")
      })
    );
    // Assertive announcements (e.g. payment success/failure) are read out
    // immediately by screen readers.
    if (opts.assertive) node.setAttribute("aria-live", "assertive");
    node.querySelector(".toast-close").addEventListener("click", () => dismiss());
    region.appendChild(node);

    let timer;
    function dismiss() {
      node.classList.add("hide");
      setTimeout(() => node.remove(), 220);
      clearTimeout(timer);
    }
    const auto = opts.duration ?? (type === "success" ? 4200 : 6000);
    if (auto > 0) timer = setTimeout(dismiss, auto);
    return { node, dismiss };
  }

  /* ------------------------------- Modal ---------------------------------- */
  const modal = {
    focusSentinel: null,
    overlay: null,
    panel: null,
    lastFocus: null,
    open({ title, body, size = "", footer = "", onClose }) {
      this.close(true);
      this.lastFocus = document.activeElement;

      const overlay = JONE.el("div", { class: "modal-backdrop", "data-close": "" });
      const closeBtn = JONE.el("button", {
        class: "modal-close", "aria-label": "Close dialog",
        innerHTML: JONE.icons.get("x")
      });
      // The dialog's accessible name comes from its heading: generate a unique
      // id for the h3 and point aria-labelledby at it.
      const headingId = "modal-title-" + Math.random().toString(36).slice(2, 9);
      const panel = JONE.el("div", {
        class: `modal-panel ${size}`,
        role: "dialog", "aria-modal": "true", "aria-labelledby": headingId, tabindex: "-1"
      },
        JONE.el("div", { class: "modal-head" },
          JONE.el("h3", { class: "h4", id: headingId, textContent: title }),
          closeBtn),
        JONE.el("div", { class: "modal-body" }, body),
        footer ? JONE.el("div", { class: "modal-foot no-print", style: "padding:1.25rem 1.5rem" }, footer) : null
      );

      const wrapper = JONE.el("div", { class: "modal" }, overlay, panel);
      document.body.appendChild(wrapper);
      requestAnimationFrame(() => wrapper.classList.add("open"));
      document.body.classList.add("modal-open");

      const close = () => this.close(false);
      overlay.addEventListener("click", close);
      closeBtn.addEventListener("click", close);

      const keydown = (e) => {
        if (e.key === "Escape") { e.stopPropagation(); close(); return; }
        if (e.key === "Tab") {
          const focusables = panel.querySelectorAll("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])");
          if (!focusables.length) return;
          const first = focusables[0], last = focusables[focusables.length - 1];
          if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
          else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
          else if (!panel.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
        }
      };
      document.addEventListener("keydown", keydown);

      this.wrapper = wrapper;
      this.onClose = onClose;
      this.keydown = keydown;
      (typeof window.joneModalCleanup === "function") && window.joneModalCleanup.focus();
      closeBtn.focus();
    },
    close(silent = false) {
      if (!this.wrapper) return;
      const wrapper = this.wrapper;          // capture: this.wrapper is nulled below
      this.wrapper = null;
      wrapper.classList.remove("open");
      document.body.classList.remove("modal-open");
      document.removeEventListener("keydown", this.keydown);
      setTimeout(() => wrapper.remove(), 200);
      if (!silent && this.onClose) this.onClose();
      if (this.lastFocus && this.lastFocus.focus) this.lastFocus.focus();
    }
  };

  /* --------------------------- Confirmation ------------------------------- */
  function confirm({ title = "Are you sure?", message = "", confirmText = "Confirm", cancelText = "Back", danger = false }) {
    return new Promise((resolve) => {
      let decided = false;   // a button decision wins; onClose only settles dismissals
      const confirmBtn = JONE.el("button", {
        class: `btn ${danger ? "btn-danger" : ""}`, textContent: confirmText
      });
      const cancelBtn = JONE.el("button", { class: "btn btn-outline", textContent: cancelText });
      const body = JONE.el("div", { class: "confirm-content" },
        JONE.el("div", { class: "confirm-icon", innerHTML: JONE.icons.get(danger ? "alertTriangle" : "checkCircle") }),
        message ? JONE.el("p", { textContent: message }) : null,
        JONE.el("div", { class: "confirm-actions" }, cancelBtn, confirmBtn)
      );
      modal.open({ title, body, onClose: () => { if (!decided) resolve(false); } });
      cancelBtn.addEventListener("click", () => { decided = true; modal.close(); resolve(false); });
      confirmBtn.addEventListener("click", () => { decided = true; modal.close(); resolve(true); });
    });
  }

  /* ------------------------------ Lightbox -------------------------------- */
  const lightbox = {
    items: [],
    index: 0,
    wrap: null,
    open(items, index = 0) {
      this.items = items;
      this.index = index;
      this.render();
    },
    render() {
      if (!this.wrap || !this.wrap.parentNode) {
        if (this.wrap) this.wrap.remove();
        this.wrap = this.build();
        document.body.appendChild(this.wrap);   // build() returns a detached node
      }
      const item = this.items[this.index];
      this.wrap.classList.add("open");
      document.body.classList.add("modal-open");

      let media;
      if (item.type === "video") {
        media = JONE.el("video", { src: item.src, controls: "", autoplay: "", playsinline: "" });
      } else {
        media = JONE.el("img", { src: item.src, alt: item.caption || "" });
        // Broken/missing images degrade gracefully (icon + note) instead of a
        // torn-image glyph; Previous/Next keeps working for the rest.
        media.addEventListener("error", function () {
          const fallback = JONE.el("div", { class: "lightbox-broken", role: "img",
            "aria-label": item.caption || "Image unavailable" },
            JONE.el("span", { class: "lightbox-broken-icon", innerHTML: JONE.icons.get("image") }),
            JONE.el("p", { textContent: "This image couldn't be loaded." })
          );
          if (media.parentNode) media.replaceWith(fallback);
        }, { once: true });
      }
      const stage = this.wrap.querySelector(".lightbox-stage");
      stage.innerHTML = "";
      stage.appendChild(media);
      this.wrap.querySelector(".lightbox-caption").textContent = item.caption || "";
      this.wrap.querySelector(".lightbox-counter").textContent = this.items.length > 1
        ? `${this.index + 1} / ${this.items.length}` : "";
      this.wrap.querySelector(".lightbox-nav.prev").style.display = this.items.length > 1 ? "" : "none";
      this.wrap.querySelector(".lightbox-nav.next").style.display = this.items.length > 1 ? "" : "none";
      this.wrap.querySelector(".lightbox-close").focus();
    },
    build() {
      const closeBtn = JONE.el("button", { class: "lightbox-btn lightbox-close", "aria-label": "Close", innerHTML: JONE.icons.get("x") });
      const prevBtn = JONE.el("button", { class: "lightbox-btn lightbox-nav prev", "aria-label": "Previous", innerHTML: JONE.icons.get("chevronLeft") });
      const nextBtn = JONE.el("button", { class: "lightbox-btn lightbox-nav next", "aria-label": "Next", innerHTML: JONE.icons.get("chevronRight") });
      const wrap = JONE.el("div", { class: "lightbox" },
        JONE.el("div", { class: "lightbox-top" },
          JONE.el("span", { class: "lightbox-counter" }),
          closeBtn),
        JONE.el("div", { class: "lightbox-stage" }),
        JONE.el("div", { class: "lightbox-caption" })
      );
      wrap.appendChild(prevBtn);
      wrap.appendChild(nextBtn);

      const self = this;
      closeBtn.addEventListener("click", () => self.close());
      prevBtn.addEventListener("click", () => self.nav(-1));
      nextBtn.addEventListener("click", () => self.nav(1));
      wrap.addEventListener("click", (e) => { if (e.target === wrap) self.close(); });
      const keydown = (e) => {
        if (e.key === "Escape") { self.close(); }
        else if (e.key === "ArrowLeft") self.nav(-1);
        else if (e.key === "ArrowRight") self.nav(1);
      };
      document.addEventListener("keydown", keydown);
      wrap._keydown = keydown;
      return wrap;
    },
    nav(dir) {
      if (this.items.length < 2) return;
      this.index = (this.index + dir + this.items.length) % this.items.length;
      this.render();
    },
    close() {
      if (this.wrap) {
        this.wrap.classList.remove("open");
        document.body.classList.remove("modal-open");
        document.removeEventListener("keydown", this.wrap._keydown);
        setTimeout(() => this.wrap.remove(), 200);
        this.wrap = null;
      }
    }
  };

  /* ------------------------------ Accordions ------------------------------ */
  function initAccordions(root = document) {
    root.querySelectorAll(".accordion").forEach((acc) => {
      acc.querySelectorAll(".accordion-item").forEach((item) => {
        const btn = item.querySelector(".accordion-btn");
        const panel = item.querySelector(".accordion-panel");
        if (!btn || !panel) return;
        btn.setAttribute("aria-expanded", item.classList.contains("open") ? "true" : "false");
        if (item.classList.contains("open")) panel.style.maxHeight = panel.scrollHeight + "px";
        btn.addEventListener("click", () => {
          const isOpen = item.classList.contains("open");
          acc.querySelectorAll(".accordion-item.open").forEach((o) => {
            o.classList.remove("open");
            const p = o.querySelector(".accordion-panel");
            p.style.maxHeight = "";
            o.querySelector(".accordion-btn").setAttribute("aria-expanded", "false");
          });
          if (!isOpen) {
            item.classList.add("open");
            panel.style.maxHeight = panel.scrollHeight + "px";
            btn.setAttribute("aria-expanded", "true");
          }
        });
      });
    });
  }

  /* --------------------------- Shared data loader ------------------------- */
  // Central component-injection used at the bottom of every page.
  function initChrome() {
    JONE.icons.inject(document);
    JONE.theme.init();
    initAccordions(document);
    return { theme: JONE.theme, icons: JONE.icons, modal, toast };
  }

  window.JONE.ui = { toast, modal, confirm, lightbox, initAccordions, initChrome };
})();
