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

  /* Homepage hero. The four hero slots (hero, hero_2, hero_3, hero_4) are
     managed independently in the dashboard. Whichever of them carry an image
     become the slideshow, in that order. When none is configured the built-in
     slideshow in index.html stays untouched. A single configured slot gives a
     static hero, exactly as before. */
  var HERO_SLOTS = ["hero", "hero_2", "hero_3", "hero_4"];
  var HERO_DEFAULT_ALT = "J-ONE Hotel & Lodge";
  // Pristine copy of the shipped hero (this script loads before carousel.js and
  // before any rebuild), used to restore a built-in image when an uploaded one
  // fails, and to rebuild the section without leftover controls or timers.
  var heroTemplate = (function () {
    var el = document.querySelector("[data-hero-carousel]");
    return el ? el.cloneNode(true) : null;
  })();
  var builtInHeroImages = heroTemplate
    ? Array.prototype.map.call(heroTemplate.querySelectorAll(".hero-slide img"), function (img) {
        return img.getAttribute("src") || "";
      })
    : [];

  function heroSlideMarkup(item, index) {
    var attrs = ' width="1600" height="900" decoding="async"' +
      (index === 0 ? ' fetchpriority="high"' : "");
    // Only the first slide is requested immediately; the carousel promotes
    // data-src to src for the active and the next slide (see carousel.js).
    var source = index === 0 ? ' src="' + JONE.esc(item.url) + '"' : ' data-src="' + JONE.esc(item.url) + '"';
    return '<div class="hero-slide' + (index === 0 ? " is-active" : "") + '"><img' + source +
      ' alt="' + JONE.esc(item.alt || HERO_DEFAULT_ALT) + '"' + attrs + "></div>";
  }

  /* A broken uploaded slide falls back to the shipped image for its position,
     and is hidden (the gradient backdrop shows) if that also fails. */
  function bindHeroErrors(root) {
    Array.prototype.forEach.call(root.querySelectorAll(".hero-slide img"), function (img, index) {
      img.addEventListener("error", function () {
        var fallback = builtInHeroImages[index];
        if (fallback && !img.dataset.heroFellBack && img.getAttribute("src") !== fallback) {
          img.dataset.heroFellBack = "1";
          img.setAttribute("src", fallback);
          return;
        }
        img.style.display = "none";
      });
    });
  }

  function heroListFromSlots(slots) {
    return HERO_SLOTS.map(function (key) { return slots && slots[key]; })
      .filter(function (item) { return item && item.image_url; })
      .map(function (item) { return { url: item.image_url, alt: item.alt_text }; });
  }

  function renderHero(list) {
    var hero = document.querySelector("[data-hero-carousel]");
    if (!hero || !heroTemplate) return;
    var signature = list.map(function (item) { return item.url + "|" + (item.alt || ""); }).join("\n");
    if (hero.dataset.heroSignature === signature) return; // already showing this set

    var markup = list.map(heroSlideMarkup).join("");
    if (document.readyState === "loading") {
      // Carousel not initialised yet: mutate in place; js/carousel.js picks up
      // the slides when the DOM is ready.
      var media = hero.querySelector(".hero-media");
      if (!media) return;
      media.innerHTML = markup;
      hero.dataset.heroSignature = signature;
      bindHeroErrors(hero);
      return;
    }

    // Carousel already running: replace the section from the pristine template
    // (drops its listeners and leftover controls), then re-initialise it.
    var clone = heroTemplate.cloneNode(true);
    var cloneMedia = clone.querySelector(".hero-media");
    if (!cloneMedia) return;
    cloneMedia.innerHTML = markup;
    clone.dataset.heroSignature = signature;
    bindHeroErrors(clone);
    hero.replaceWith(clone);
    if (window.JONE && JONE.carousel && typeof JONE.carousel.init === "function") {
      JONE.carousel.init(clone);
    }
  }

  function applyHero(s) {
    var list = gallerySlots ? heroListFromSlots(gallerySlots) : [];
    if (!list.length && s && s.hero_image_url) {
      list = [{ url: s.hero_image_url, alt: s.hero_alt_text }];
    }
    if (!list.length) return; // no custom hero: the built-in slideshow is the fallback
    renderHero(list);
  }

  function apply() {
    var s = payload();
    try {
      applySwaps(s);
      applyReveals(s);
      applyHero(s);
      if (gallerySlots) applyGallery(gallerySlots);
    } catch (_) { /* image config must never break the page */ }
  }

  // Gallery slots are fetched independently of hotel settings; no deployment is
  // needed after an administrator replaces a photo.
  var gallerySlots = null;
  function applyGallery(slots) {
    gallerySlots = slots;
    var mapping = {welcome: 'intro_image', experience: 'experience_image', find_us: 'location_image'};
    Object.keys(mapping).forEach(function(section){
      var item=slots[section];
      if (item && item.image_url) {
        var key=mapping[section];
        var swap={}; swap[key+'_url']=item.image_url;
        applySwaps(swap);
        document.querySelectorAll('[data-site-image="'+key+'"]').forEach(function(img){
          if(item.alt_text) img.alt=item.alt_text;
          img.removeAttribute('srcset');
        });
      }
    });
    applyHero({});
  }
  function loadGallery(){
    if(!window.API || !window.API.getHomepageGallery) return;
    window.API.getHomepageGallery({cache:"no-store"}).then(function(result){
      applyGallery(result.data || {});
    }).catch(function(){ /* retain built-in assets on network failure */ });
  }
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', loadGallery);
  else loadGallery();

  document.addEventListener("jone:hotel", apply);
  // When this file loads after hydration already happened, apply immediately.
  if (window.HOTEL && window.HOTEL.raw) apply();

  window.JONE = window.JONE || {};
  window.JONE.siteImages = { apply: apply };
})();
