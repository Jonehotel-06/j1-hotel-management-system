/* js/room-card.js */
/* ==========================================================================
   J-ONE HOTEL & LODGE — the ONE public room card.

   Why this file exists
   --------------------
   The public site rendered room cards in three different places (the home page
   "featured rooms", the rooms listing and, in a different shape again, the
   room-details hero). They drifted: one showed a single photo with no gallery,
   another printed amenities as a dot-separated string, and a third had no
   price. Task 06 of the targeted fixes asks for ONE design language across
   every room, driven by the database, so the markup lives here exactly once
   and every page calls it.

   What a card contains (all values come from the API — nothing is hardcoded):
     * a large rounded photo gallery with previous/next arrows, a "1 / 5"
       counter in the upper-left corner and the room category badge in the
       upper-right corner. Every image the hotel uploaded for that room is
       reachable, in the order the backend returns them (Task 05).
     * room name, short description and the from-price per night.
     * the room's real amenities as icon chips (Task 04 resolver, so an
       amenity whose stored icon key is odd still renders a glyph).
     * a "Room features" block with a check icon per real feature.
     * a "Check availability" button that goes through the EXISTING booking
       flow (booking.html?room=<slug>) — no new booking mechanism.

   Contract used (verified against apps.rooms serializers):
     { id, name, slug, short_description, base_price: "25000.00", max_guests,
       bed_type, bed_count, room_size, view, is_featured,
       primary_image_url, images: [{ id, image_url, alt_text, caption,
       display_order, is_primary }], image_count, amenities: [{name, icon}] }

   Images are lazy-loaded: only the first slide of each card is requested up
   front; the rest carry data-src and are fetched when the guest navigates to
   them (or when they scroll into view via the browser's own lazy loading).
   ========================================================================== */
(function (window) {
  "use strict";

  var PLACEHOLDER_ICON = "bed";

  function esc(value) {
    if (window.JONE && typeof window.JONE.esc === "function") return window.JONE.esc(value);
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function money(value) {
    if (window.JONE && typeof window.JONE.formatNaira === "function") return window.JONE.formatNaira(value);
    var n = Number(value || 0);
    return "\u20A6" + (isFinite(n) ? n.toLocaleString("en-NG") : "0");
  }

  function iconName(value, fallback) {
    if (window.JONE && window.JONE.icons && typeof window.JONE.icons.resolveName === "function") {
      return window.JONE.icons.resolveName(value, fallback || PLACEHOLDER_ICON);
    }
    return value || fallback || PLACEHOLDER_ICON;
  }

  /* ------------------------------------------------------------------
     Image collection.

     Accepts the list-row shape (images[]) and, defensively, a detail row that
     only carries primary_image_url. URLs are de-duplicated so the same photo
     attached twice is shown once (task 05, requirement 10) and an image with
     no URL is skipped rather than rendered as a broken <img>.
     ------------------------------------------------------------------ */
  function imagesOf(room) {
    room = room || {};
    var list = Array.isArray(room.images) ? room.images : [];
    var out = [], seen = Object.create(null);
    list.forEach(function (image) {
      if (!image) return;
      var url = image.image_url || image.url || image.image || "";
      if (!url || seen[url]) return;
      seen[url] = true;
      out.push({
        src: url,
        alt: image.alt_text || room.name || "Room photo",
        caption: image.caption || "",
        primary: !!image.is_primary,
      });
    });
    if (!out.length && room.primary_image_url) {
      out.push({ src: room.primary_image_url, alt: room.name || "Room photo", caption: "", primary: true });
    }
    // Cover first, everything else keeps the backend's order.
    if (out.length > 1) {
      var primaryIndex = -1;
      for (var i = 0; i < out.length; i++) { if (out[i].primary) { primaryIndex = i; break; } }
      if (primaryIndex > 0) out.unshift(out.splice(primaryIndex, 1)[0]);
    }
    return out;
  }

  function slugOf(room) {
    return room && (room.slug || room.id) ? String(room.slug || room.id) : "";
  }

  function detailHref(room) {
    var key = slugOf(room);
    return "room-details.html?room=" + encodeURIComponent(key);
  }

  function bookingHref(room) {
    // The existing availability/booking flow: booking.html?room=<slug>.
    var key = slugOf(room);
    return "booking.html?room=" + encodeURIComponent(key);
  }

  /* ------------------------------------------------------------------
     Gallery markup. Each card is its own carousel instance; data-room-card
     attributes are read by enhance() below after injection.
     ------------------------------------------------------------------ */
  function galleryMarkup(room, index, images) {
    if (!images.length) {
      return '<div class="rc-media rc-media-empty" role="img" aria-label="' + esc(room.name || "Room") + ' photo coming soon">' +
        '<span class="rc-placeholder" data-icon="' + esc(PLACEHOLDER_ICON) + '" data-size="34"></span>' +
        '</div>';
    }
    var slides = images.map(function (image, i) {
      // Only the first slide is fetched eagerly; the others are promoted on
      // demand, so a page of ten rooms with five photos each still makes ten
      // image requests on load, not fifty.
      var load = i === 0
        ? ' src="' + esc(image.src) + '" loading="' + (index < 3 ? "eager" : "lazy") + '" fetchpriority="' + (index === 0 && i === 0 ? "high" : "low") + '"'
        : ' data-src="' + esc(image.src) + '"';
      return '<figure class="rc-slide' + (i === 0 ? " is-active" : "") + '" data-rc-slide="' + i + '"' + (i === 0 ? "" : " hidden") + '>' +
        '<img' + load + ' alt="' + esc(image.alt) + '" decoding="async">' +
        '</figure>';
    }).join("");

    var arrows = images.length > 1
      ? '<button type="button" class="rc-nav prev" data-rc-prev aria-label="Previous photo of ' + esc(room.name || "room") + '"><span data-icon="chevronLeft" data-size="18"></span></button>' +
        '<button type="button" class="rc-nav next" data-rc-next aria-label="Next photo of ' + esc(room.name || "room") + '"><span data-icon="chevronRight" data-size="18"></span></button>'
      : "";

    return '<div class="rc-media" data-rc-gallery data-rc-count="' + images.length + '">' +
      '<div class="rc-slides" data-rc-slides>' + slides + '</div>' +
      /* Counter, upper-left. Hidden from assistive tech: the arrows already
         announce position, and a live region per card would be noisy. */
      '<span class="rc-counter" data-rc-counter aria-hidden="true">1 / ' + images.length + '</span>' +
      arrows +
      '<button type="button" class="rc-expand" data-rc-expand aria-label="Open all photos of ' + esc(room.name || "room") + ' in a larger view"><span data-icon="maximize" data-size="16"></span></button>' +
      '</div>';
  }

  function badgeMarkup(room) {
    // The "category badge" in the reference design names the room category.
    // It is the room type's own name — never a hardcoded label.
    if (!room.name) return "";
    return '<span class="rc-badge">' + esc(room.name) + '</span>';
  }

  function amenityStrip(room) {
    var amenities = Array.isArray(room.amenities) ? room.amenities : [];
    if (!amenities.length) return "";
    var chips = amenities.slice(0, 6).map(function (amenity) {
      var name = amenity && (amenity.name || amenity);
      if (!name) return "";
      var icon = iconName(amenity && amenity.icon, "sparkle");
      return '<li class="rc-amenity"><span class="rc-amenity-icon" data-icon="' + esc(icon) + '" data-size="16"></span>' +
        '<span>' + esc(name) + '</span></li>';
    }).join("");
    return chips ? '<ul class="rc-amenities" aria-label="Room amenities">' + chips + '</ul>' : "";
  }

  /* "Room features" — derived from the room type's own catalogue fields, so
     every line is a fact about THIS room and nothing is invented. */
  function featuresOf(room) {
    var features = [];
    if (room.max_guests) features.push(Number(room.max_guests) + (Number(room.max_guests) === 1 ? " guest" : " guests"));
    if (room.bed_type) features.push((room.bed_count && Number(room.bed_count) > 1 ? room.bed_count + " \u00D7 " : "") + room.bed_type);
    if (room.room_size) features.push(room.room_size);
    if (room.view) features.push(room.view + " view");
    return features;
  }

  function featuresMarkup(room, options) {
    var features = featuresOf(room);
    if (!features.length) return "";
    var items = features.map(function (feature) {
      return '<li><span class="rc-check" data-icon="check" data-size="14"></span><span>' + esc(feature) + '</span></li>';
    }).join("");
    // The heading is a requirement of the design reference ("Room features").
    // `options.heading === false` lets the room-details page reuse the same
    // list under its own heading instead of repeating the words.
    var heading = options && options.heading === false ? "" : '<h4 class="rc-features-title">Room features</h4>';
    return '<div class="rc-features">' + heading + '<ul>' + items + '</ul></div>';
  }

  /**
   * Full room card markup.
   *
   * @param {object} room  A row from GET /api/rooms/ (or /api/rooms/{slug}/).
   * @param {object} [options]
   *   index        {number}  position on the page (lazy-load priority)
   *   actions      {string}  "both" (default) | "book" | "details" | "none"
   *   bookingLabel {string}  defaults to "Check availability"
   */
  function markup(room, options) {
    options = options || {};
    room = room || {};
    var index = Number(options.index || 0);
    var images = imagesOf(room);
    var actions = options.actions || "both";
    var bookingLabel = options.bookingLabel || "Check availability";

    var buttons = "";
    if (actions === "both") {
      buttons = '<a class="btn btn-outline rc-btn" href="' + detailHref(room) + '">Details</a>' +
        '<a class="btn btn-accent rc-btn" href="' + bookingHref(room) + '">' + esc(bookingLabel) + '</a>';
    } else if (actions === "book") {
      buttons = '<a class="btn btn-accent rc-btn rc-btn-wide" href="' + bookingHref(room) + '">' + esc(bookingLabel) + '</a>';
    } else if (actions === "details") {
      buttons = '<a class="btn btn-outline rc-btn rc-btn-wide" href="' + detailHref(room) + '">' + esc(bookingLabel) + '</a>';
    }

    return '<article class="room-card rc-card" data-rc-room="' + esc(slugOf(room)) + '">' +
      '<div class="rc-media-wrap">' + galleryMarkup(room, index, images) + badgeMarkup(room) + '</div>' +
      '<div class="rc-body">' +
        '<h3 class="rc-title">' + esc(room.name || "Room") + '</h3>' +
        (room.short_description ? '<p class="rc-desc">' + esc(room.short_description) + '</p>' : "") +
        '<p class="rc-price"><span class="rc-from">From</span> <strong>' + money(room.base_price) + '</strong> <span class="rc-per">/ night</span></p>' +
        amenityStrip(room) +
        featuresMarkup(room) +
        (buttons ? '<div class="rc-actions">' + buttons + '</div>' : "") +
      '</div>' +
    '</article>';
  }

  /* ------------------------------------------------------------------
     Behaviour: gallery navigation, swipe, and the shared lightbox.
     ------------------------------------------------------------------ */
  function slideAt(gallery, index) {
    var slides = gallery.querySelectorAll("[data-rc-slide]");
    return slides && slides[index] ? slides[index] : null;
  }

  function hydrateSlide(slide) {
    if (!slide) return;
    var img = slide.querySelector("img");
    if (!img) return;
    var deferred = img.getAttribute("data-src");
    if (deferred) {
      img.setAttribute("src", deferred);
      img.removeAttribute("data-src");
    }
  }

  function setSlide(gallery, next) {
    var slides = gallery.querySelectorAll("[data-rc-slide]");
    var total = slides ? slides.length : 0;
    if (!total) return 0;
    var index = ((next % total) + total) % total;
    for (var i = 0; i < total; i++) {
      var active = i === index;
      slides[i].classList.toggle("is-active", active);
      if (active) {
        slides[i].removeAttribute("hidden");
        hydrateSlide(slides[i]);
      } else {
        slides[i].setAttribute("hidden", "");
      }
    }
    var counter = gallery.querySelector("[data-rc-counter]");
    if (counter) counter.textContent = (index + 1) + " / " + total;
    gallery.setAttribute("data-rc-index", String(index));
    return index;
  }

  function currentIndex(gallery) {
    var index = parseInt(gallery.getAttribute("data-rc-index") || "0", 10);
    return isFinite(index) ? index : 0;
  }

  function galleryImages(gallery, room) {
    // Prefer the room object's own image list (captions included); fall back
    // to the DOM so a re-rendered card still opens the right viewer.
    var fromRoom = room ? imagesOf(room) : [];
    if (fromRoom.length) return fromRoom;
    var slides = gallery.querySelectorAll("[data-rc-slide]") || [];
    var items = [];
    for (var i = 0; i < slides.length; i++) {
      var img = slides[i].querySelector("img");
      if (!img) continue;
      items.push({ src: img.getAttribute("data-src") || img.getAttribute("src") || "", alt: img.getAttribute("alt") || "" });
    }
    return items;
  }

  function openLightbox(gallery, room, at) {
    var items = galleryImages(gallery, room).filter(function (item) { return !!item.src; });
    if (!items.length) return;
    var index = typeof at === "number" ? at : currentIndex(gallery);
    if (window.JONE && window.JONE.ui && window.JONE.ui.lightbox) {
      window.JONE.ui.lightbox.open(items.map(function (item) {
        return { src: item.src, caption: item.caption || item.alt || "" };
      }), Math.max(0, Math.min(index, items.length - 1)));
    }
  }

  /**
   * Wire a bare gallery element (one produced by galleryFor(), i.e. without
   * the surrounding .rc-card — the room-details hero). Same behaviour as a
   * card gallery: arrows, counter, keyboard, swipe and the shared lightbox.
   */
  function bindGalleryRoot(root, room) {
    var gallery = root.querySelector("[data-rc-gallery]");
    if (!gallery) return;
    gallery.setAttribute("data-rc-index", "0");
    var total = parseInt(gallery.getAttribute("data-rc-count") || "0", 10) || 0;

    var prev = root.querySelector("[data-rc-prev]");
    var next = root.querySelector("[data-rc-next]");
    if (prev) {
      prev.addEventListener("click", function (event) {
        event.preventDefault();
        event.stopPropagation();
        setSlide(gallery, currentIndex(gallery) - 1);
      });
    }
    if (next) {
      next.addEventListener("click", function (event) {
        event.preventDefault();
        event.stopPropagation();
        setSlide(gallery, currentIndex(gallery) + 1);
      });
    }
    var expand = root.querySelector("[data-rc-expand]");
    if (expand) {
      expand.addEventListener("click", function (event) {
        event.preventDefault();
        event.stopPropagation();
        openLightbox(gallery, room, currentIndex(gallery));
      });
    }

    // Clicking the photo itself opens the large view (the whole set).
    var media = root.querySelector(".rc-media");
    if (media) {
      media.addEventListener("click", function (event) {
        if (event.target.closest("button")) return; // arrows handled above
        openLightbox(gallery, room, currentIndex(gallery));
      });
      media.classList.add("is-zoomable");
    }

    // Keyboard: arrows move between photos while the gallery has focus.
    if (media) {
      media.setAttribute("tabindex", "0");
      media.addEventListener("keydown", function (event) {
        if (event.key === "ArrowLeft" && total > 1) { event.preventDefault(); setSlide(gallery, currentIndex(gallery) - 1); }
        else if (event.key === "ArrowRight" && total > 1) { event.preventDefault(); setSlide(gallery, currentIndex(gallery) + 1); }
        else if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openLightbox(gallery, room, currentIndex(gallery)); }
      });
    }

    // Mobile swipe (pointer events, so a mouse drag cannot hijack the page).
    if (total > 1 && window.PointerEvent) {
      var startX = null, startY = null;
      media.addEventListener("pointerdown", function (event) {
        if (event.pointerType === "mouse") return;
        startX = event.clientX; startY = event.clientY;
      });
      media.addEventListener("pointerup", function (event) {
        if (startX === null) return;
        var dx = event.clientX - startX;
        var dy = event.clientY - startY;
        startX = null; startY = null;
        if (Math.abs(dx) < 40 || Math.abs(dx) < Math.abs(dy)) return; // a scroll, not a swipe
        setSlide(gallery, currentIndex(gallery) + (dx < 0 ? 1 : -1));
      });
      media.addEventListener("pointercancel", function () { startX = null; startY = null; });
    }
  }

  /**
   * Wire every card inside `root` (defaults to the document).
   *
   * @param {Element|Document} root
   * @param {Array} rooms  The same array the markup was generated from.
   */
  function enhance(root, rooms) {
    root = root || document;
    var cards = root.querySelectorAll(".rc-card");
    var bySlug = Object.create(null);
    (rooms || []).forEach(function (room) {
      var key = slugOf(room);
      if (key && !(key in bySlug)) bySlug[key] = room;
    });
    Array.prototype.forEach.call(cards, function (card) {
      if (card.getAttribute("data-rc-bound") === "1") return;
      card.setAttribute("data-rc-bound", "1");
      bindGalleryRoot(card, bySlug[card.getAttribute("data-rc-room")] || null);
    });
    // The room-details hero is a bare gallery (no .rc-card wrapper): bind it
    // to the same handlers so navigation behaves identically there.
    if (!cards.length && root && root.querySelector) {
      var bare = root.querySelector("[data-rc-gallery]");
      if (bare && root.getAttribute && root.getAttribute("data-rc-bound") !== "1") {
        if (root.setAttribute) root.setAttribute("data-rc-bound", "1");
        bindGalleryRoot(root, (rooms && rooms[0]) || null);
      }
    }
    if (window.JONE && window.JONE.icons) window.JONE.icons.inject(root);
  }

  /** Render a whole list into a container and wire it, in one call. */
  function render(container, rooms, options) {
    if (!container) return;
    options = options || {};
    var list = Array.isArray(rooms) ? rooms : [];
    container.innerHTML = list.map(function (room, index) {
      return markup(room, {
        index: index,
        actions: options.actions,
        bookingLabel: options.bookingLabel,
      });
    }).join("");
    enhance(container, list);
  }

  window.JONE = window.JONE || {};
  window.JONE.roomCard = {
    markup: markup,
    render: render,
    enhance: enhance,
    imagesOf: imagesOf,
    featuresOf: featuresOf,
    featuresMarkup: featuresMarkup,
    amenityStrip: amenityStrip,
    // Reusable pieces, for pages that need the gallery on its own
    // (the room-details hero).
    gallery: function (room, options) {
      options = options || {};
      var images = imagesOf(room || {});
      return '<div class="rc-media-wrap">' +
        galleryMarkup(room || {}, Number(options.index || 0), images) +
        badgeMarkup(room || {}) +
        '</div>';
    },
    bookingHref: bookingHref,
    detailHref: detailHref,
  };
})(window);
