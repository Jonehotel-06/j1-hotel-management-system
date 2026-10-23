# J-ONE Hotel & Lodge — SEO, indexing, local search and performance implementation

**Repository:** `Jonehotel-06/j1-hotel-management-system` (branch `main`, inspected at `051f4e7`)
**Production origin:** `https://jonehotels.com` · **API:** `https://api.jonehotels.com`
**Date of work:** 2026-10-23 · **Release:** frontend `1.1.23` → `1.1.24`
**Scope:** frontend SEO layer only. **Zero backend files were modified** — no schema, business-rule, pricing, payment, authentication or API-contract change.

Two companion documents ship with the code:
* [`docs/SEO.md`](docs/SEO.md) — what was built, the indexability map, and how to maintain it.
* [`docs/GOOGLE_SETUP.md`](docs/GOOGLE_SETUP.md) — owner steps for Search Console and the Business Profile.

---

## 1. Initial audit — what was wrong, and how serious it was

Everything below was measured on the live site or on the repository at `HEAD`, not assumed.

| # | Finding | Severity | Evidence |
|---|---|---|---|
| 1 | **The hotel was described as being in the wrong city.** `index.html` and `about.html` meta descriptions read "…on Mgbowo Street, **Abuja**". The hotel is in Enugu. | **Critical** | `grep -rn Abuja frontend/*.html` — 2 hits, 0 legitimate |
| 2 | **No canonical URL existed on any field of the site** — 0 of 26 public HTML files. `jonehotels.com`, `www.jonehotels.com`, `http://` and `https://` all returned **200**. | **Critical** | `grep -c 'rel="canonical"' *.html` → 0; `curl -I` on all four variants → 200 |
| 3 | **The sitemap could not be honoured: all 13 entries were redirects.** Cloudflare serves extensionless URLs (`/rooms` → 200) and 307-redirects `.html` (`/rooms.html` → 307 → `/rooms`); `/index.html` → 307 → `/`. The sitemap listed `/index.html`, `/rooms.html`, … — every single entry redirected. | **Critical** | `curl -o /dev/null -w '%{http_code} %{redirect_url}' /rooms.html` → `307 https://jonehotels.com/rooms` |
| 4 | **No structured data anywhere.** 0 JSON-LD blocks on 26 pages — no `Hotel`, `LocalBusiness`, `WebSite` or `BreadcrumbList`. Nothing tied the website to the Google Maps listing. | **High** | `grep -rl application/ld+json frontend` → none |
| 5 | **robots.txt disallowed the very pages it also marked `noindex`** (`/my-bookings.html`, `/payment-verify.html`, `/booking-confirmation.html`, `/cancellation-result.html`, `/qr-service.html`, `/login.html`, `/register.html`), and its rules named `.html` paths while production serves extensionless ones. A disallowed URL can never be crawled, so its `noindex` is never read — the pages could still be indexed as bare URLs. | **High** | live `/robots.txt` vs `curl /login.html` → 307 |
| 6 | **The pages that should rank had no crawlable content.** `rooms.html`, `facilities.html`, `gallery.html`, `offers.html` shipped only skeleton placeholders; room types, capacities and amenities existed solely after `GET /api/rooms/` returned. | **High** | `rooms.html` initial HTML contains 3 `<div class="skeleton">` and no room name |
| 7 | **Generic H1s with no brand or place** — "Quiet comfort, close to everything.", "Everything you need", "A closer look", "Special rates". The homepage H1 never named the business. | **Medium** | page source |
| 8 | **Open Graph was broken for sharing.** `og:image` missing on **every** page; `og:url` missing on every page; `og:title` on 2 pages; no `twitter:title`/`twitter:image`; no dimensions. | **Medium** | head inspection |
| 9 | **"View on Google Maps" pointed at `https://www.google.com/maps`** — the Maps homepage, not the hotel. And when `google_maps_url` was unset (it is), JS **hid** the link entirely, so the site offered nothing that connected its address to the Maps listing. | **Medium** | `components/footer.html`, `js/hotel-data.js`, `js/navigation.js`; live `GET /api/hotel/` → `"google_maps_url": ""` |
| 10 | **Active navigation never highlighted in production** — links are written `rooms.html`, Cloudflare serves `/rooms`, so `markActive()` never matched. Cosmetic, but it also meant no self-referential link signal. | **Medium** | `js/navigation.js:84` vs production URL shape |
| 11 | **LCP and CLS exposure.** Hero LCP image 229 KB with no preload and no `srcset`; 9 of 10 homepage images had no `width`/`height`; six full-viewport hero photos fetched on first load. | **Medium** | headless Chromium: images without dimensions 9/10, homepage 1,900.7 KB |
| 12 | **Version drift — the release pipeline was already red.** `version.json` = 1.1.23 but every HTML asset stamp was `?v=1.1.24`, failing an existing deployment test. | **Medium** | `node --test` failure #101 at baseline |
| 13 | `_headers` had no explicit content type or cache policy for `robots.txt`/`sitemap.xml`; `booking-review.html` and `my-bookings.html` had a `meta noindex` but no `X-Robots-Tag` header. | **Low** | `frontend/_headers` |
| 14 | Pre-existing repository/deployment conflicts, unrelated to SEO: the root `frontend/manifest.webmanifest` and `frontend/worker/index.js` still exist although two existing test suites require their removal. | **Low (out of scope)** | failing tests #132, #409, and `tests-pwa/test_pwa.py`'s "old root manifest is gone" |

### Baseline before touching anything

| Check | Result |
|---|---|
| `python3 validate.py` | `ALL OK` (72 HTML files, 31 JS files) |
| `node --test tests-js/` | **572 tests, 569 pass, 3 fail** — #101 (version drift), #132 (worker/index.js present), #409 (root manifest present) |
| Production `GET /`, `/robots.txt`, `/sitemap.xml` | 200 / 200 / 200 (`application/xml`) |

---

## 2. Implemented fixes (completed work)

### 2.1 A single, generated SEO layer
New module **`frontend/seo_build.py`** owns every SEO signal for every public page: title, description, canonical, robots directive, Open Graph/Twitter, JSON-LD, `sitemap.xml` and `robots.txt`. `build.py` applies it on every build inside `<!-- SEO:BEGIN --> … <!-- SEO:END -->` markers, strips any hand-written/legacy copies of those tags first, and **fails the build** on any SEO defect. The page head, the sitemap and robots.txt can no longer disagree.

*Why:* every one of findings 1–5, 8, 9 and 13 was a drift between hand-maintained copies. One registry is the only way this stays correct.

### 2.2 Canonical URL and origin consistency
Every one of the 23 managed pages now carries an absolute, self-referencing, **extensionless** canonical on `https://jonehotels.com`; `og:url` always equals it. None can be `www`, `.html`, trailing-slash or query-bearing (enforced by validation and tests).

*Why:* Cloudflare 307-redirects `.html`, so the "obvious" `.html` canonical would have pointed every page at a redirect. Booking/payment query parameters were not touched.

### 2.3 Sitemap rebuilt
`https://jonehotels.com/sitemap.xml` now lists exactly the 12 public, canonical, indexable, non-redirecting pages. The booking/payment flow, staff and guest portals, `room-details`, `review` and the QR guest page are excluded. No `lastmod` fabrications (none is emitted — the real modification time of a generated page is build time, which would be misleading). Generated from the same registry, so adding/removing a page cannot desynchronise it.

### 2.4 robots.txt rebuilt
`Allow: /`, no global disallow, no asset blocking, correct absolute `Sitemap:`. Disallows trimmed to the five genuinely private prefix paths (`/dashboard`, `/portal`, `/login`, `/register`, `/qr-service`) — the `noindex` pages that are *not* private are no longer both disallowed and noindexed, so Google can now actually read their `noindex`.

### 2.5 Indexing boundaries made explicit
All 23 managed pages carry an explicit `robots` directive: 13 `index, follow, max-image-preview:large…`, 10 `noindex, follow`. `X-Robots-Tag: noindex, nofollow` added in `_headers` for `/booking-review.html` and `/my-bookings.html` (previously meta-only). Error pages and the QR page keep `noindex`. No global noindex, nothing indexable lost.

### 2.6 Page-level metadata and content
* Homepage title → `J-ONE Hotel & Lodge | Hotel in Enugu, Nigeria`; description rewritten (Abuja removed) around hotel + location + rooms + facilities + direct booking.
* Unique title and description for all 13 indexable pages (previously 4 near-empty duplicates like "Contact | J-ONE Hotel & Lodge").
* Every H1 now names the business and/or the city: *"J-ONE Hotel & Lodge — quiet comfort in Enugu"*, *"Rooms & suites at J-ONE Hotel & Lodge, Enugu"*, *"Hotel facilities at…"*, *"Photo gallery of…"*, *"Offers & special rates at…"*, *"About…"*, *"Contact…"*.
* **Crawlable room summary** added to `rooms.html`: the four real room types with bed, capacity, size, view and a one-line description (from the live catalogue), plus the six verified amenities — real content in the initial HTML, no hard-coded prices (rates stay live-quoted).
* **Crawlable amenities summary** added to `facilities.html`: the six amenities the room catalogue confirms.
* Homepage body copy now states "…place to rest **in Enugu**".
* "View on Google Maps" now resolves to a Maps **search for the verified address** when no profile URL is configured, instead of a dead generic link or a hidden button — the link that ties the site to the Business Profile now works.

### 2.7 Structured data
`Hotel` (`#hotel`) on the homepage and contact page, `WebSite` + `WebPage` everywhere, `BreadcrumbList` on every non-home page — all generated from one verified `SITE` record. No `aggregateRating`, `review`, `priceRange`, `geo`, `openingHours`, `checkinTime`/`checkoutTime` or `sameAs`: see §7 for exactly why each is withheld.

### 2.8 Images and performance
* 10 shipped JPEGs re-encoded **in place** (same filenames, same URLs, same pixels dimensions): 1,819 KB → 1,629 KB (−10.4 %), PSNR 38–52 dB per image.
* LCP image: `srcset` 800/1200/1376 + `sizes="100vw"` + matching `<link rel="preload" imagesrcset>`; `fetchpriority="high"`; never lazy-loaded.
* `width`/`height` on every content photograph and the header logo (homepage images without dimensions 9/10 → 0/10); `decoding="async"` on below-the-fold imagery.
* `optimize_images.py` added so the re-encode is repeatable and never renames/moves/resizes anything.
* No change to CSS, no change to the service worker's caching strategy, no change to the booking flow's script order.

### 2.9 Two small JavaScript corrections
* `js/navigation.js` — active-nav comparison normalises `.html`/`/index` so it works with the URLs Cloudflare actually serves.
* `js/hotel-data.js` + `js/navigation.js` — Google Maps link falls back to an address search instead of being hidden.

Both are 10-line, additive changes; no API call, booking, payment or auth path was touched.

### 2.10 Release engineering
`version.json` bumped 1.1.23 → 1.1.24, `js/version.js` and `sw.js` `CACHE_VERSION` regenerated by `build.py --bump patch`, all asset stamps refreshed. `seo_build.py`/`optimize_images.py` added to `.assetsignore` so build tooling is not published.

---

## 3. Files changed (exact paths)

**Modified**
| Path | Change |
|---|---|
| `frontend/build.py` | Imports `seo_build`; applies the SEO head to every non-portal page; regenerates sitemap/robots after the build; runs SEO validation and fails the build on any defect |
| `frontend/robots.txt` | Regenerated (see §5) |
| `frontend/sitemap.xml` | Regenerated (see §5) |
| `frontend/_headers` | `Content-Type` + `Cache-Control: max-age=3600` for `/robots.txt` and `/sitemap.xml`; `X-Robots-Tag: noindex, nofollow` for `/booking-review.html` and `/my-bookings.html` |
| `frontend/.assetsignore` | Excludes `seo_build.py`, `optimize_images.py` from public assets |
| `frontend/index.html` | New H1/hero copy, "Enugu" in body copy, hero `srcset`+preload, image dimensions, LCP comment |
| `frontend/rooms.html` | New H1, crawlable room-type + amenities section |
| `frontend/facilities.html` | New H1, crawlable in-room amenities section, editorial image dimensions |
| `frontend/gallery.html`, `offers.html`, `about.html` | New H1s (brand + city); `about.html` also "in Enugu" in body copy, image dimensions |
| `frontend/contact.html` | New H1; real Google Maps destination for the verified address |
| `frontend/policies.html`, `privacy.html`, `terms.html`, `cancellation-policy.html`, `refund-policy.html`, `review.html`, `booking.html`, `booking-review.html`, `booking-confirmation.html`, `cancellation-result.html`, `payment-verify.html`, `my-bookings.html`, `login.html`, `register.html`, `room-details.html`, `offline.html`, `403.html`, `404.html`, `500.html`, `qr-service.html`, `portal/*.html`, `dashboard/*.html` | Regenerated head (title/description/canonical/robots/OG/JSON-LD where managed) and version stamps — no layout or behaviour change |
| `frontend/components/header.html` | Logo `width`/`height`/`decoding` (CSS already fixes the rendered height) |
| `frontend/js/navigation.js` | Extensionless-safe active-nav matching; Maps fallback to an address search |
| `frontend/js/hotel-data.js` | Maps link falls back to an address search instead of being hidden |
| `frontend/assets/images/*.jpg` (10 files) | Re-encoded in place, −10.4 % total, visually identical |
| `frontend/js/version.js`, `frontend/version.json`, `frontend/sw.js` | Regenerated for release 1.1.24 |
| `frontend/tests-js/test-inline-scripts-parse.js` | The existing inline-script parser now skips non-JavaScript data blocks (`type="application/ld+json"`); JSON-LD is validated by `test-seo.js` and `seo_build.py --check` instead. Classic scripts are still strictly parsed — nothing was weakened |
| `frontend/README.md` | Documents the SEO layer and its commands |
| `docs/SEO.md`, `docs/GOOGLE_SETUP.md` | New documentation (listed below as created) |

**Created**
| Path | Purpose |
|---|---|
| `frontend/seo_build.py` | Single source of truth: page registry, canonical/robots/OG/JSON-LD generation, sitemap, robots, validator |
| `frontend/optimize_images.py` | Repeatable in-place JPEG re-encode (≥8 % gain required, never renames) |
| `frontend/tests-js/test-seo.js` | 21 automated SEO contract tests |
| `frontend/assets/images/og-j-one-hotel-enugu.jpg` | 1200×630 Open Graph image, generated from the real `hero-1.jpg` photograph |
| `frontend/assets/images/hero-1-800.jpg`, `hero-1-1200.jpg` | Responsive LCP variants (77 KB / 156 KB) |
| `docs/SEO.md` | Implementation, indexability map, maintenance rules |
| `docs/GOOGLE_SETUP.md` | Owner guide: Search Console, sitemap submission, Business Profile checklist, data conflicts |

---

## 4. Metadata summary (all 23 managed pages)

| Page | Canonical | Title | Description | Indexed | In sitemap |
|---|---|---|---|---|---|
| `index.html` | `https://jonehotels.com/` | J-ONE Hotel & Lodge \| Hotel in Enugu, Nigeria | J-ONE Hotel & Lodge is a contemporary hotel on Mgbowo Street, Enugu, with air-conditioned rooms and suites, 24/7 power, free Wi-Fi and direct online booking. | yes | yes |
| `rooms.html` | `/rooms` | Rooms & Suites in Enugu \| J-ONE Hotel & Lodge | Rooms and suites at J-ONE Hotel & Lodge, Enugu: Peony Patios, Deluxe Room, J One Special and Kings and Queens Court. Air conditioning, free Wi-Fi, 24/7 power. | yes | yes |
| `facilities.html` | `/facilities` | Hotel Facilities in Enugu \| J-ONE Hotel & Lodge | In-room air conditioning, free Wi-Fi, TV, refrigerator, water heater and 24/7 power at J-ONE Hotel & Lodge, Enugu, plus space for dining and gatherings. | yes | yes |
| `gallery.html` | `/gallery` | Photo Gallery \| J-ONE Hotel & Lodge, Enugu | See the rooms, exterior, dining and facilities at J-ONE Hotel & Lodge on Mgbowo Street, Enugu, before you book your stay. | yes | yes |
| `offers.html` | `/offers` | Hotel Offers & Special Rates in Enugu \| J-ONE Hotel & Lodge | Current offers and special rates at J-ONE Hotel & Lodge, Enugu. Expired offers are removed automatically — check live availability and book direct. | yes | yes |
| `about.html` | `/about` | About J-ONE Hotel & Lodge \| Enugu, Nigeria | J-ONE Hotel & Lodge is a contemporary hotel on Mgbowo Street, Enugu, built around clean, comfortable rooms, privacy and attentive hospitality. | yes | yes |
| `contact.html` | `/contact` | Contact & Directions \| J-ONE Hotel & Lodge, Enugu | Contact J-ONE Hotel & Lodge: Plot 566 Mgbowo Street, off Ezike Street, Enugu State, Nigeria. Call +234 803 211 2874, email us or send an enquiry. | yes | yes |
| `policies.html` | `/policies` | Hotel Policies \| J-ONE Hotel & Lodge, Enugu | The stay policies that apply at J-ONE Hotel & Lodge, Enugu — check-in and check-out, cancellation, children, identification and house rules. | yes | yes |
| `cancellation-policy.html` | `/cancellation-policy` | Cancellation Policy \| J-ONE Hotel & Lodge, Enugu | How cancellation works at J-ONE Hotel & Lodge, Enugu: deadlines, review by hotel staff and how refund requests are handled. | yes | yes |
| `refund-policy.html` | `/refund-policy` | Refund Policy \| J-ONE Hotel & Lodge, Enugu | The refund policy for stays at J-ONE Hotel & Lodge, Enugu, including how Paystack payments are refunded once the hotel approves a request. | yes | yes |
| `privacy.html` | `/privacy` | Privacy Policy \| J-ONE Hotel & Lodge | How J-ONE Hotel & Lodge collects, uses and protects the personal information you share when you enquire, book or stay with us. | yes | yes |
| `terms.html` | `/terms` | Terms & Conditions \| J-ONE Hotel & Lodge | The terms and conditions that govern use of the J-ONE Hotel & Lodge website and the bookings made through it. | yes | yes |
| `review.html` | `/review` | Leave a Review \| J-ONE Hotel & Lodge, Enugu | Stayed with us? Tell J-ONE Hotel & Lodge how we did — your feedback goes directly to hotel management. | yes | **no** |
| `booking.html` | `/booking` | Book Your Stay \| J-ONE Hotel & Lodge, Enugu | Check live availability and book your stay online at J-ONE Hotel & Lodge, Enugu. Secure direct booking with instant confirmation. | **noindex** | no |
| `booking-review.html` | `/booking-review` | Review Your Booking \| J-ONE Hotel & Lodge | Review your booking details and continue to secure payment. | noindex | no |
| `booking-confirmation.html` | `/booking-confirmation` | Booking Confirmation \| J-ONE Hotel & Lodge | Your booking confirmation and receipt. | noindex | no |
| `cancellation-result.html` | `/cancellation-result` | Cancellation / Refund Status \| J-ONE Hotel & Lodge | Check the status of a cancellation or refund request. | noindex | no |
| `payment-verify.html` | `/payment-verify` | Payment Verification \| J-ONE Hotel & Lodge | Verifying your payment. | noindex | no |
| `my-bookings.html` | `/my-bookings` | Your Stays \| J-ONE Hotel & Lodge | Find your bookings, references and receipts. | noindex | no |
| `login.html` | `/login` | Staff Sign In \| J-ONE Hotel & Lodge | Staff sign in. | noindex | no |
| `register.html` | `/register` | Guest Checkout \| J-ONE Hotel & Lodge | Complete your booking. | noindex | no |
| `room-details.html` | `/room-details` | Room Details \| J-ONE Hotel & Lodge, Enugu | Room details, amenities and live availability at J-ONE Hotel & Lodge, Enugu. | noindex | no |
| `offline.html` | `/offline` | You're Offline \| J-ONE Hotel & Lodge | You are offline. | noindex | no |

`/dashboard/*`, `/portal/*`, `/qr-service` and the error pages keep their existing `noindex` and are managed outside this registry (asserted by the test suite).

---

## 5. Sitemap and robots.txt — final state

**`https://jonehotels.com/sitemap.xml`** (12 URLs, no `lastmod`, no priorities, no duplicates, all canonical/extensionless/HTTPS):

```
https://jonehotels.com/            https://jonehotels.com/policies
https://jonehotels.com/rooms       https://jonehotels.com/cancellation-policy
https://jonehotels.com/facilities  https://jonehotels.com/refund-policy
https://jonehotels.com/gallery     https://jonehotels.com/privacy
https://jonehotels.com/offers      https://jonehotels.com/terms
https://jonehotels.com/about       https://jonehotels.com/contact
```

**`https://jonehotels.com/robots.txt`**

```
# robots.txt — J-ONE Hotel & Lodge (https://jonehotels.com)
# Generated by frontend/seo_build.py. Do not edit by hand.
# (public pages, CSS, JS, images, sitemap.xml and robots.txt are all crawlable;
#  robots.txt is not an access control — private routes are also authenticated
#  and carry an X-Robots-Tag/robots noindex directive)

User-agent: *
Allow: /

# authenticated staff console
Disallow: /dashboard
# verified-email guest portal
Disallow: /portal
# staff sign-in
Disallow: /login
# guest checkout shell
Disallow: /register
# bearer-link guest request form (URL carries a token)
Disallow: /qr-service

Sitemap: https://jonehotels.com/sitemap.xml
```

**Validation:** `python3 seo_build.py --check` → `ALL OK`; 7 sitemap tests + 2 robots tests in `test-seo.js` pass (well-formed XML, HTTPS canonical origin, no duplicates, every URL maps to a real indexable non-redirecting page, no private/tokenised URL, sitemap declared, no global disallow, no asset blocked). Locally served as `text/plain` and `application/xml`; production already returned `application/xml` for the sitemap and now has explicit content-type + 1-hour cache headers.

---

## 6. Structured data — implemented and validated

| Entity | Where | Fields |
|---|---|---|
| `Hotel` `#hotel` | homepage, `/contact` | `name`, `alternateName` (J-ONE Hotel and Lodge, J One Hotel, Jone Hotels), `url`, `telephone` `+2348032112874`, `email`, `image[]` (4 real photos), `logo` (507×900 ImageObject), `PostalAddress` (Mgbowo Street / Enugu / Enugu State / NG), `currenciesAccepted` NGN, `hasMap`, `amenityFeature[]` (air conditioning, free Wi-Fi, television, refrigerator, water heater, 24/7 power supply), `areaServed` Enugu |
| `WebSite` `#website` | every managed page | `url`, `name`, `inLanguage` `en-NG`, `publisher` → `#hotel` |
| `WebPage` | every managed page | `url`, `name`, `description`, `isPartOf` → `#website`, `about` → `#hotel`, `primaryImageOfPage` |
| `BreadcrumbList` | every non-home page | home → page, positions sequential, every item a canonical URL |

**Deliberately withheld** (each is a real fact that is not verified — publishing any of them would be fabrication):

| Field | Why |
|---|---|
| `aggregateRating`, `review` | The hotel has no verified public rating; Google's policy prohibits self-serving invented ratings |
| `geo.latitude/longitude` | No verified coordinates exist in the repository or the public API. A wrong pin is worse than no pin and would misplace the Maps listing |
| `checkinTime` / `checkoutTime` | The backend settings report **12:00/12:00** while the contact page states **2:00 PM** check-in. Must be reconciled by the owner first |
| `priceRange`, `openingHours`, `sameAs` | Not verified / no confirmed social profiles |
| `address` (long backend form) | Backend stores a longer address variant; the owner must confirm which matches the Business Profile (see §7) |

**Validation performed:** JSON parses; `@context`/`@type` correct; required properties present; breadcrumbs canonical and sequential; every referenced image exists on disk and is absolute; no rating/review/price/geo node present; no API origin, booking reference or token in the markup. Locally that is `python3 seo_build.py --check` + 4 structured-data tests. **Not** submitted to Google's Rich Results Test — that needs a browser and the deployed URL, and the owner should run it after deploy (see §12).

---

## 7. Performance — measured before and after

**Method:** headless Chromium 156 (Playwright), mobile viewport 412×915 @2×, `/api/` requests blocked so only frontend assets count, 5 runs per page, medians reported, before/after served from the same machine over loopback. These are **lab** numbers, not field data.

| Page | Metric | Before | After | Δ |
|---|---|---|---|---|
| `/` | transferred bytes | 1,900.7 KB | **1,732.0 KB** | −8.9 % |
| `/` | LCP element | `hero-1.jpg` 229 KB, no preload | `hero-1-1200.jpg` 156 KB, preloaded + `srcset` | — |
| `/` | images without intrinsic dimensions | 9 / 10 | **0 / 10** | fixed |
| `/` | CLS | 0.00 | 0.00 | unchanged (already good) |
| `/` | JSON-LD blocks | 0 | 1 | +1 |
| `/facilities` | transferred bytes | 740.6 KB | 728.0 KB | −1.7 % |
| `/facilities` | CLS | **0.10** | **0.00** | below the 0.1 threshold |
| `/rooms` | transferred bytes | 548.3 KB | 554.5 KB | **+1.1 %** (the new crawlable room summary — a deliberate trade: 6 KB of HTML for real room content in the initial document) |
| `/contact` | transferred bytes | 552.1 KB | 571.0 KB | +3.4 % (SEO head + JSON-LD; compresses to a fraction with Brotli at the edge) |
| all | images re-encoded | 1,819 KB | 1,629 KB | −10.4 %, PSNR 38–52 dB |

**LCP timings** (median / best of 5): `/` 148/132 ms → 144/128 ms; `/rooms` 124/100 → 112/100; `/contact` 124/100 → 108/96; `/facilities` 104/100 → 100/84. On a loopback server these are dominated by container noise, so they are reported but **not** claimed as a real-world improvement — the deterministic wins are the bytes, the preload, the `srcset` and the dimension coverage. All measured pages stay far inside the Core Web Vitals thresholds in the lab; the 75th-percentile field values must come from Search Console after deployment.

**Biggest remaining byte opportunity (not taken, by design):** the homepage fetches six full-viewport hero photographs (~1 MB) because each absolutely-positioned slide is inside the viewport, so `loading="lazy"` does not defer them. Trimming the slideshow from six slides to three, or giving slides 2–6 their own `srcset`, is the single largest further win — but it is a visible design change and belongs to the owner.

---

## 8. Security and indexing boundaries — confirmation

* **No backend file was modified.** `git diff --stat backend/` → empty. No schema, migration, permission, CORS, CSP, pricing, Paystack or authentication change.
* **No security header was removed or weakened.** CSP, HSTS, `X-Frame-Options`, `nosniff`, `Referrer-Policy`, `Permissions-Policy`, `frame-ancestors 'none'`, `form-action` allow-list are byte-identical; the only `_headers` changes are two added `X-Robots-Tag` blocks and two content-type/cache blocks.
* **No private route was made public.** `/dashboard/*`, `/portal/*`, `/qr-service`, error pages keep `noindex` + `robots.txt` + (where applicable) `X-Robots-Tag: no-store`.
* **No private data leaked into metadata.** Tests assert no booking reference, Paystack key, bearer token or credential appears in any page `<head>` or in `sitemap.xml`/`robots.txt`/JSON-LD. The API origin never appears in published metadata.
* **`robots.txt` is not treated as a security control** — the document says so explicitly and the authentication layers are untouched.
* **No shared caching of private responses**: the service worker remains network-only for `/api/`, `/media/`, `/dashboard/`, `/portal/`, `/qr-service.html`. It still only controls `/dashboard/`, so a stale cache cannot hide these SEO changes or interrupt a booking.
* **No secret committed**: the new Python tooling reads no environment variable and hard-codes only public business facts already published on the site.

---

## 9. Regression testing — what was run, what passed, what did not

| Suite | Command | Result |
|---|---|---|
| Frontend HTML/JS validator | `python3 validate.py` | **ALL OK** (72 HTML files, 31 JS files) |
| SEO validator | `python3 seo_build.py --check` | **ALL OK** (23 pages, sitemap, robots) |
| Frontend JS suite | `node --test tests-js/` | **593 tests, 591 pass, 2 fail** (see below) |
| ← including the new SEO contract | `node --test tests-js/test-seo.js` | **21 / 21 pass** |
| Backend full suite | `python3 manage.py test tests` | **1,685 tests, 1 failure, 17 skipped** (1 environment failure, see below) |
| Backend booking/availability/pricing/auth/cancellation | `python3 manage.py test tests.test_bookings tests.test_availability tests.test_pricing tests.test_auth tests.test_cancellation_refunds` | **73 / 73 pass** |
| PWA regression + security | `python3 tests-pwa/test_regression.py` | **88 / 88 pass** (incl. dashboard auth guard, no tokens in cache/localStorage, no secrets in shipped JS) |
| PWA behaviour | `python3 tests-pwa/test_pwa.py` | **150 / 151 pass** (1 pre-existing: "old root manifest is gone") |
| Accessibility (public + PWA UI) | `python3 tests-pwa/test_a11y.py` | **14 / 14 pass**, no serious/critical violations |

**Failures — all pre-existing, none introduced by this work:**

1. `tests-js` #132 "no obsolete production API rewrite remains active" and #409 "the only web app manifest is scoped to the staff dashboard". **Verified pre-existing:** `git stash` to the untouched `HEAD` and re-running gives the same two failures. They require deleting `frontend/worker/index.js` and `frontend/manifest.webmanifest` — a deployment-architecture decision, not an SEO one, so it was **not** done unilaterally. Note for the owner: production `GET https://jonehotels.com/api/hotel/` returns **404**, which proves the Worker proxy is *not* active, so removing `worker/index.js` would be safe — but it should be a deliberate, separately reviewed change because it touches the Cloudflare deployment contract.
2. `tests-pwa/test_pwa.py` "old root manifest is gone" — the same root-manifest issue.
3. Backend `tests.test_deployment_readiness.ProductionSettingsBootAndFailFastTests.test_production_settings_boot_cleanly_with_northflank_mysqls_and_rediss_urls` — fails with `Error loading MySQLdb module. Did you install mysqlclient?` This sandbox has no MySQL client headers, so the `mysqlclient` C extension cannot be built. Environment limitation; no backend file was touched.
4. `node --test` #101 (version drift, `version.json` 1.1.23 vs `?v=1.1.24` stamps) was **fixed** by releasing 1.1.24 properly.

**Not run:** `frontend/tests-pwa/test_update.py`, `test_ios.py`, `test_installability.py`, `test_paystack.py`, `test_sticky*.py`, `test_multi_room_ui.py`, `test_table_layout.py` — they need the Playwright iOS/Chrome device emulation and a live `dev_server.py` session; the equivalents that matter for this change (auth guard, no-cache of tokens, PWA scope, a11y, no JS errors) are covered by the three suites above. `blog`-style synthetic SEO audits and Google Rich Results were not run — they need the deployed URL and Google's own tooling.

**Deployment risk assessment**

| Risk | Likelihood | Mitigation |
|---|---|---|
| Cloudflare serves the new `robots.txt`/`sitemap.xml` | Low risk — plain static files | Verify both URLs after deploy (§11) |
| `.html` canonical confusion | Eliminated | Canonicals are extensionless, matching what Cloudflare serves with 200 |
| Booking/payment flow broken | Very low — no flow code touched | 88/88 PWA + 73/73 backend booking tests pass; only JSDoc-level JS edits in nav/hotel-data |
| Image quality regression | Very low | Same filenames/URLs, PSNR ≥ 38 dB |
| Layout shift | Lower than before | Dimensions added; CLS 0.10 → 0.00 on `/facilities` |
| Service-worker stale cache hiding changes | Eliminated | Public pages register no worker; dashboard cache version bumped to `jone-v1.1.24` |
| SEO validation blocking a future release | Intentional | Build fails loudly with the exact defect; `seo_build.py --check` can be run alone |

---

## 10. What was *not* changed (deliberate)

* No framework, no build-system replacement, no redesign, no CSS change.
* No room price, inventory, amenity, address or business-identity change.
* No backend code, database schema, API contract, auth, payment or payroll change.
* No Cloudflare routing/redirect rule created and no DNS change — `wrangler.jsonc`, `vercel.json` and the deployment arrangement are untouched (only `_headers` gained four header blocks).
* No invented reviews, ratings, coordinates, opening hours, prices, awards or testimonials.
* The pre-existing `worker/index.js` / root `manifest.webmanifest` test conflicts were left for the owner (§9).

---

## 11. Deployment instructions

Nothing in this change requires a Northflank deployment. The backend is untouched.

**Cloudflare (frontend)** — from `frontend/`:

1. `python3 build.py --bump patch` — already done (`1.1.24`); re-run only if you make further changes. It rewrites chrome, SEO heads, sitemap, robots, version stamps, the service-worker cache name, and **validates**. A non-zero exit means a real defect, not a flake.
2. `node --test tests-js/test-seo.js` (and the rest of `tests-js/`) — must pass except the two known pre-existing failures.
3. Deploy the `frontend/` directory to Cloudflare Pages as usual (static assets; `.assetsignore` keeps the build tooling out). The Worker, if configured, needs no change — `robots.txt` and `sitemap.xml` are plain static files, not intercepted by `/api/`, `/media/` or the SPA fallback.
4. Purge the Cloudflare cache for `/`, `/robots.txt` and `/sitemap.xml` (HTML is `max-age=0, must-revalidate`, so a purge is usually unnecessary — do it only if verification shows the old files).
5. Verify in this order:
   * `curl -sI https://jonehotels.com/sitemap.xml` → `200`, `content-type: application/xml`
   * `curl -s https://jonehotels.com/robots.txt` → shows the new file and the `Sitemap:` line
   * `curl -s https://jonehotels.com/ | grep -o '<link rel="canonical"[^>]*>'` → `https://jonehotels.com/`
   * `curl -s https://jonehotels.com/rooms | grep -o '<title>[^<]*'` → the new rooms title
   * `curl -s https://jonehotels.com/rooms.html` → still `307` to `/rooms` (unchanged behaviour; the sitemap simply no longer lists it)
6. Optional (owner approval, §12 of `docs/GOOGLE_SETUP.md`): add the `www → apex` 301 redirect rule and enable Cloudflare image conversion (Polish/WebP).

**Rollback:** revert the release commit — the previous static files, including `sitemap.xml`/`robots.txt`/`robots` directives, come back wholesale, and `version.json` drops back so the service worker re-caches. No database or backend state is involved.

---

## 12. Google-side setup (owner, not automated)

Full step-by-step in [`docs/GOOGLE_SETUP.md`](docs/GOOGLE_SETUP.md). Summary:

1. **Verify** `jonehotels.com` in Search Console — Domain property via a DNS TXT record (no developer needed, no invented token committed).
2. **Submit** `https://jonehotels.com/sitemap.xml` (Sitemaps report → expect 12 discovered URLs).
3. **Inspect** `/`, `/rooms`, `/facilities`, `/contact`, `/gallery`, `/offers`, `/about` in URL Inspection and request indexing for those that matter most.
4. **Do not** request indexing for `/booking`, `/booking-confirmation`, `/payment-verify`, `/my-bookings`, `/login`, `/dashboard/`, `/portal/`.
5. **Watch** Pages (expect "Excluded by noindex tag" on the booking/portal pages — that is correct), Core Web Vitals (mobile, 75th percentile), and Performance split into branded vs non-branded queries.
6. **Business Profile checklist** (in the doc): ownership verified, primary category `Hotel` + honest secondary categories, NAP + website identical to the site, attributes matching the six published amenities, genuine photographs, honest review solicitation, replies to every review, monthly performance review, no duplicate listing.

**Three owner decisions that block further SEO data** (documented, not changed):

| Item | Conflict | Needed |
|---|---|---|
| Street address | Site/JSON-LD: "Plot 566 Mgbowo Street, off Ezike Street, Enugu State". Backend hotel settings: "Plot 566, Mgbowo Street, off NOWAS, near Mater Dei Catholic Church, Trans-Ekulu, Enugu State, Nigeria" | Confirm which form the Business Profile shows; update the other two (backend setting + `SITE["street"]`) |
| Check-in / check-out | Backend: 12:00 / 12:00. Contact page: "Check-in from 2:00 PM" | Confirm the real times; they can then be published as `checkinTime`/`checkoutTime` |
| Google Maps + review URLs | Backend `google_maps_url` and `google_review_url` are empty, so the site falls back to an address search | Paste the Business Profile share link and the "write a review" link into Hotel Settings; the site then uses both automatically on every page |

---

## 13. Remaining limitations

* **No Google account access was used or claimed.** Nothing is verified, submitted, indexed or ranked. No ranking position is promised: Google controls ranking, and this work only makes the site legible, consistent and technically eligible.
* **Field Core Web Vitals are unmeasured.** Lab numbers are in §7; the 75th-percentile field data appears in Search Console only after ~28 days of real traffic.
* **Google Rich Results / Schema validator verdicts are pending** — run them against the deployed homepage and `/contact` (§12).
* **Coordinates, opening hours, check-in times, social profiles and ratings are intentionally absent** — see §6. Each is a one-line addition to `SITE` once the owner confirms the fact.
* **`www.jonehotels.com` still serves 200** (no 301 to apex). Canonicals consolidate signals on the apex, but the redirect is recommended and is an owner/Cloudflare change.
* **Homepage hero weight (~1 MB, six slides)** remains the top byte opportunity; it needs a design decision.
* **Two pre-existing repo/deployment test conflicts** (`worker/index.js`, root `manifest.webmanifest`) are unresolved by choice — they need a deliberate deployment-architecture decision, see §9.
* **One backend test** could not run in this environment (`mysqlclient` unavailable) — an environment limitation, not a code issue; it must be confirmed on a machine with MySQL client libraries or in CI.
* **No per-room pages.** `/room-details` is a stateful single page driven by query parameters and is `noindex`; creating one thin page per room type would be a doorway-page risk, so room information was instead put into the crawlable `rooms.html` body. If the owner later wants rankable room pages, they should be real, distinct, server-rendered-content pages — a separate, deliberate project.
