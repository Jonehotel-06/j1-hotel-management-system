/* sw.js */
/* ==========================================================================
   J-ONE HOTEL & LODGE — service worker.

   DESIGN RULES (deliberately conservative — this is a live booking system):

   1. The Django backend is the ONLY source of truth. No /api/ request is ever
      cached, read from cache, or answered from cache. All /api/ traffic is
      passed straight through to the network (network-only, no fallback that
      could look like a successful response).
     2. Nothing authenticated or sensitive is stored. The dashboard (/dashboard/),
      verified guest portal (/portal/), and bearer-link QR request page are
      network-only for navigations, so private/staff data or bearer-bearing
      forms are never replayed from shared Cache Storage.
   3. Only immutable-ish, public, static assets are cached: our own CSS, JS,
      icons and a small set of local images — plus previously visited public
      HTML pages (network-first) and the offline fallback page.
   4. Caches are versioned; every activation deletes every cache that does not
      belong to the current version.
   5. The new worker does NOT call skipWaiting() on its own. It waits until the
      page explicitly tells it to (JONE.pwa only does so when the user is idle
      and not in a booking/payment/form flow), so nobody is reloaded mid-payment.

   CACHE_VERSION is kept in lockstep with the release version by
   frontend/build.py (--bump/--version) — release with that, never by hand.
   ========================================================================== */

"use strict";

const CACHE_VERSION = "jone-v1.1.11";
const PRECACHE = `${CACHE_VERSION}-precache`;
const PAGES_CACHE = `${CACHE_VERSION}-pages`;
const IMAGES_CACHE = `${CACHE_VERSION}-images`;
const CURRENT_CACHES = new Set([PRECACHE, PAGES_CACHE, IMAGES_CACHE]);

const OFFLINE_URL = "/offline.html";

/* Small, deliberate precache: the offline page, the shared stylesheet graph,
   the shared JS bootstrap and the install/launch icons. Nothing else — we do
   NOT precache every page or every hotel photograph. */
const PRECACHE_URLS = [
  OFFLINE_URL,
  "/css/main.css",
  "/css/variables.css",
  "/css/themes.css",
  "/css/typography.css",
  "/css/components.css",
  "/css/public.css",
  "/css/booking.css",
  "/css/dashboard.css",
  "/css/responsive.css",
  "/css/audit-fixes.css",
  "/js/config.js",
  "/js/utils.js",
  "/js/icons.js",
  "/js/api.js",
  "/js/theme.js",
  "/js/ui.js",
  "/js/hotel-data.js",
  "/js/navigation.js",
  "/js/pwa.js",
  "/js/version.js",
  "/js/update-checker.js",
  "/favicon/favicon-32.png",
  "/favicon/favicon-96.png",
  "/favicon/apple-touch-icon.png",
  "/favicon/icon-192.png",
  "/favicon/icon-512.png",
  "/assets/icons/logo-official.svg",
  "/manifest.webmanifest"
];

/* Bounded runtime image cache (public marketing imagery only). */
const IMAGE_CACHE_LIMIT = 40;
const PAGES_CACHE_LIMIT = 25;

/* Paths the service worker must never cache or serve from cache:
     /api/        every backend call — availability, bookings, payments, auth, admin
     /media/      backend-served uploads proxied on the same origin
     /dashboard/  the authenticated staff console (network-only)
     /portal/     verified-email guest portal pages (network-only)
     /qr-service.html bearer-link guest form (network-only; API always needs connectivity)
   These are enforced explicitly in the fetch handler below. */

/* ------------------------------- Install --------------------------------- */
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(PRECACHE).then((cache) =>
      // addAll() is all-or-nothing; add individually so one missing optional
      // asset can never break the whole installation.
      Promise.all(
        PRECACHE_URLS.map((url) =>
          cache.add(new Request(url, { cache: "reload" })).catch(() => null)
        )
      )
    )
  );
  // NOTE: no self.skipWaiting() here — see the update strategy above.
});

/* ------------------------------- Activate -------------------------------- */
self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(keys.map((k) => (CURRENT_CACHES.has(k) ? null : caches.delete(k))));
      if (self.registration.navigationPreload) {
        await self.registration.navigationPreload.enable().catch(() => {});
      }
      await self.clients.claim();
    })()
  );
});

/* -------------------------- Page-driven messages -------------------------- */
self.addEventListener("message", (event) => {
  const data = event.data || {};
  // The page decides WHEN it is safe to swap in a new version.
  if (data.type === "SKIP_WAITING") self.skipWaiting();
  if (data.type === "GET_VERSION" && event.source) {
    event.source.postMessage({ type: "VERSION", version: CACHE_VERSION });
  }
});

/* ------------------------------ Cache helpers ----------------------------- */
async function trimCache(cacheName, maxEntries) {
  try {
    const cache = await caches.open(cacheName);
    const keys = await cache.keys();
    if (keys.length <= maxEntries) return;
    // FIFO: oldest insertions first.
    await Promise.all(keys.slice(0, keys.length - maxEntries).map((k) => cache.delete(k)));
  } catch (_) {
    /* cache pressure / quota — never fatal */
  }
}

function isPrecachedAsset(url) {
  return PRECACHE_URLS.includes(url.pathname);
}

/* ------------------------------- Strategies ------------------------------- */

/* Cache-first — used only for the offline fallback page. */
async function cacheFirst(request, cacheName) {
  const cached = await caches.match(request, { ignoreVary: true });
  if (cached) return cached;
  const response = await fetch(request);
  if (response && response.ok && response.type === "basic") {
    const cache = await caches.open(cacheName);
    cache.put(request, response.clone()).catch(() => {});
  }
  return response;
}

/* Stale-while-revalidate for our own CSS/JS/fonts/manifest. Serves the cached
   copy instantly for speed, but ALWAYS re-fetches in the background and stores
   the new version, so an edit you deploy shows up on the very next load — no
   manual cache clear and no waiting for an expiry. This is what makes updates
   propagate automatically. */
async function assetSWR(request, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request, { ignoreVary: true });
  const network = fetch(request)
    .then((response) => {
      if (response && response.ok && response.type === "basic") {
        cache.put(request, response.clone()).catch(() => {});
      }
      return response;
    })
    .catch(() => null);
  // If we have a cached copy, serve it now and update in the background.
  // Otherwise wait for the network (first visit / newly added file).
  return cached || (await network) || Response.error();
}

/* Stale-while-revalidate for local public images (hero/gallery art). */
async function staleWhileRevalidate(request, cacheName, limit) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request, { ignoreVary: true });
  const network = fetch(request)
    .then((response) => {
      if (response && response.ok && response.type === "basic") {
        cache.put(request, response.clone()).then(() => trimCache(cacheName, limit)).catch(() => {});
      }
      return response;
    })
    .catch(() => null);
  return cached || (await network) || Response.error();
}

/* Authenticated staff and verified guest portal navigations: network-ONLY.
   Responses are never read from or written to Cache Storage, so one person's
   operational or portal data cannot be replayed for another person. */
async function protectedNavigationHandler(event, area) {
  try {
    const preload = event.preloadResponse ? await event.preloadResponse : null;
    return preload || (await fetch(event.request));
  } catch (_) {
    const offline = await caches.match(OFFLINE_URL);
    if (offline) {
      return new Response(offline.body, {
        status: 503,
        statusText: "Offline",
        headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" }
      });
    }
    return new Response(
      `An internet connection is required to use the ${area}.`,
      { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" } }
    );
  }
}

/* Network-first for public HTML navigations, with the previously visited copy
   as a fallback and offline.html as the last resort. Never index.html — this
   is a multi-page site, not an SPA. */
async function navigationHandler(event) {
  const request = event.request;
  try {
    const preload = event.preloadResponse ? await event.preloadResponse : null;
    const response = preload || (await fetch(request));
    if (response && response.ok && response.type === "basic") {
      const cache = await caches.open(PAGES_CACHE);
      cache.put(request, response.clone()).then(() => trimCache(PAGES_CACHE, PAGES_CACHE_LIMIT)).catch(() => {});
    }
    return response;
  } catch (_) {
    const cached = await caches.match(request, { ignoreSearch: true, ignoreVary: true });
    if (cached) return cached;
    const offline = await caches.match(OFFLINE_URL);
    if (offline) return offline;
    return new Response(
      "You are offline and this page has not been saved for offline use.",
      { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" } }
    );
  }
}

/* --------------------------------- Fetch ---------------------------------- */
self.addEventListener("fetch", (event) => {
  const request = event.request;

  // Only ever touch GET. POST/PUT/PATCH/DELETE (bookings, payments, logins)
  // go straight to the network, untouched and unqueued.
  if (request.method !== "GET") return;

  let url;
  try {
    url = new URL(request.url);
  } catch (_) {
    return;
  }

  // Cross-origin (Paystack, Google Maps, fonts, or an independently hosted
  // API): do not intercept at all.
  if (url.origin !== self.location.origin) return;

  // Range requests (video/audio seeking) — let the browser handle them.
  if (request.headers.has("range")) return;

  // API and backend media: completely untouched — no interception at all, so a
  // cached response can never stand in for authoritative booking/payment data.
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/media/")) return;

  // version.json is the deployment source of truth for the update checker.
  // runtime-config.js is deployment-owned and may change independently of a
  // release. Both must ALWAYS come from the network: a stale configuration
  // must never point a freshly deployed frontend at the wrong API.
  if (url.pathname === "/version.json" || url.pathname === "/js/runtime-config.js") return;

  // Staff, verified portal, and bearer-link QR navigations are network-only.
  // They are never written to nor read from shared Cache Storage.
  const isDashboardPath = url.pathname === "/dashboard" || url.pathname.startsWith("/dashboard/");
  const isPortalPath = url.pathname === "/portal" || url.pathname.startsWith("/portal/");
  const isQrServicePath = url.pathname === "/qr-service.html";
  if (isDashboardPath || isPortalPath || isQrServicePath) {
    if (request.mode === "navigate") {
      const area = isDashboardPath ? "staff console" : isPortalPath ? "guest portal" : "guest-service QR form";
      event.respondWith(protectedNavigationHandler(event, area));
    }
    return;
  }

  // Anything carrying credentials or an Authorization header: pass through.
  if (request.headers.has("authorization")) return;

  const dest = request.destination;

  if (request.mode === "navigate") {
    event.respondWith(navigationHandler(event));
    return;
  }

  if (dest === "style" || dest === "script" || dest === "font" || dest === "manifest") {
    // Stale-while-revalidate: instant from cache, but always refreshed in the
    // background so a new deploy is picked up on the next load automatically.
    event.respondWith(assetSWR(request, PRECACHE));
    return;
  }

  if (dest === "image") {
    // Icons/logos are part of the precache; other local imagery is bounded SWR.
    event.respondWith(staleWhileRevalidate(request, IMAGES_CACHE, IMAGE_CACHE_LIMIT));
    return;
  }

  // Everything else (e.g. XHR/fetch to static JSON): straight to the network.
});
