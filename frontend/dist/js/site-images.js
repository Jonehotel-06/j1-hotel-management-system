/* js/site-images.js */
/* ==========================================================================
   site-images.js — admin-configured website images with graceful fallback.

   The public hotel payload (GET /api/hotel/) may carry optional admin-uploaded
   image URLs (hero_image_url, intro_image_url, experience_image_url,
   location_image_url, policy_image_url, contact_image_url). When one is
   present this module swaps it in; when it is null the page keeps its built-in
   static asset exactly as shipped — so the site always looks the same until an
   administrator configures a replacement.

   It listens for the "jone:hotel" event dispatched by js/hotel-data.js, which
   fires once from the cached payload (synchronously, before DOMContentLoaded)
   and again after a fresh fetch.

   Targets:
     [data-site-image="intro_image|experience_image|location_image"]
         plain <img> swap — built-in src stays when no custom URL exists
     [data-site-image-block="policy_image|contact_image"]
         a hidden block revealed only when a custom image is configured
     [data-hero-carousel]
         homepage hero — a configured hero image replaces the built-in
         slideshow with a single static slide
   ========================================================================== */

(function () {
  "use strict";

  function payload() {
    var h = window.HOTEL;
    return (h && h.raw) || {};
  }

  /* Plain <img> swaps: built-in src is the fallback, custom URL wins. */
  function applySwaps(s) {
    ["intro_image", "experience_image", "location_image"].forEach(function (key) {
      var url = s[key + "_url"];
      if (!url) return;
      document.querySelectorAll('[data-site-image="' + key + '"]').forEach(function (img) {
        if (img.getAttribute("src") !== url) img.setAttribute("src", url);
        // A failed custom upload must never leave a broken icon on the site:
        // fall back to the built-in asset this element shipped with.
        if (!img.dataset.siteFallback) {
          img.dataset.siteFallback = img.getAttribute("data-site-default") || "";
        }
        if (!img.dataset.siteSwapBound) {
          img.dataset.siteSwapBound = "1";
          // The inline onerror adds a "placeholder" look for a missing BUILT-IN
          // asset; this handler now owns error recovery (custom → built-in →
          // placeholder), so retire the inline one to avoid it firing for
          // failed custom URLs.
          img.removeAttribute("onerror");
          img.onerror = null;
          img.addEventListener("error", function () {
            var fallback = img.dataset.siteFallback;
            if (fallback && img.getAttribute("src") !== fallback) {
              img.setAttribute("src", fallback);
            } else if (img.parentNode) {
              img.parentNode.classList.add("placeholder");
            }
          });
        }
      });
    });
  }

  /* Hidden-until-configured blocks (policy / contact pages). */
  function applyReveals(s) {
    ["policy_image", "contact_image"].forEach(function (key) {
      var url = s[key + "_url"];
      if (!url) return;
      document.querySelectorAll('[data-site-image-block="' + key + '"]').forEach(function (block) {
        var img = block.querySelector("img");
        if (img) {
          if (img.getAttribute("src") !== url) img.setAttribute("src", url);
          if (!img.dataset.siteSwapBound) {
            img.dataset.siteSwapBound = "1";
            img.addEventListener("error", function () { block.style.display = "none"; });
          }
        }
        block.style.display = "";
      });
    });
  }

  /* Homepage hero: a configured image replaces the built-in slideshow with a
     single static slide. When the carousel script already ran, rebuild the
     section so its timers/listeners restart cleanly against the new markup. */
  function singleSlideMarkup(url) {
    return '<div class="hero-slide is-active"><img src="' + JONE.esc(url) +
      '" alt="J-ONE Hotel &amp; Lodge" fetchpriority="high"></div>';
  }

  function applyHero(s) {
    var url = s.hero_image_url;
    var hero = document.querySelector("[data-hero-carousel]");
    if (!hero) return;
    var media = hero.querySelector(".hero-media");
    if (!media) return;

    var slides = media.querySelectorAll(".hero-slide");
    var first = slides[0] && slides[0].querySelector("img");

    if (!url) return; // no custom hero: the built-in slideshow is the fallback
    if (slides.length === 1 && first && first.getAttribute("src") === url) return;

    var customFailed = function () {
      // Broken custom hero: restore the shipped slideshow on next navigation;
      // for now hide the broken <img> so the gradient backdrop shows instead.
      if (first) first.style.display = "none";
    };

    if (document.readyState === "loading") {
      // Carousel not initialised yet: mutate in place; js/carousel.js picks up
      // a single slide and degrades to a static hero (controls removed).
      media.innerHTML = singleSlideMarkup(url);
      first = media.querySelector(".hero-slide img");
      if (first) first.addEventListener("error", customFailed);
      return;
    }

    // Carousel already running: clone the section (drops its listeners), then
    // re-initialise against the single custom slide.
    var clone = hero.cloneNode(false);
    clone.innerHTML = hero.innerHTML;
    var cloneMedia = clone.querySelector(".hero-media");
    if (!cloneMedia) return;
    cloneMedia.innerHTML = singleSlideMarkup(url);
    var cloneImg = cloneMedia.querySelector(".hero-slide img");
    if (cloneImg) cloneImg.addEventListener("error", customFailed);
    hero.replaceWith(clone);
    if (window.JONE && JONE.carousel && typeof JONE.carousel.init === "function") {
      JONE.carousel.init(clone);
    }
  }

  function apply() {
    var s = payload();
    try {
      applySwaps(s);
      applyReveals(s);
      applyHero(s);
    } catch (_) { /* image config must never break the page */ }
  }

  document.addEventListener("jone:hotel", apply);
  // When this file loads after hydration already happened, apply immediately.
  if (window.HOTEL && window.HOTEL.raw) apply();

  window.JONE = window.JONE || {};
  window.JONE.siteImages = { apply: apply };
})();
