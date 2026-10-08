# J-ONE Hotel & Lodge — Frontend

A premium, fully static frontend for **J-ONE Hotel & Lodge** built with **pure HTML, CSS and
Vanilla JavaScript** and designed to be hosted independently while talking to a **Django REST API** backend.

> This is a frontend-only project. No React, Vue, jQuery, Bootstrap or Tailwind. No Django
> server-rendered templates. Communication with the backend happens entirely through a
> centralized reusable API layer using `fetch()` and JSON.

---

## Project overview

The frontend delivers three coherent experiences in one product:

1. **Public website** — an editorial, conversion-focused hospitality site (home, rooms, facilities,
   gallery, offers, about, contact, policies).
2. **Booking + payment flow** — a short, confidence-building flow: dates/guests → room selection →
   guest info → review → secure payment (Paystack) → confirmation + receipt.
3. **Staff dashboard** — a dense, operational console for reservations, stays, folios/cashier,
   POS, guest services, housekeeping/maintenance, inventory/procurement, workforce, reports,
   content and settings.
4. **Verified-email guest portal** — a deliberately narrow portal for a guest's reservations,
   permitted folio views and active-stay service requests; it is separate from the booking-token
   `my-bookings.html` journey.

The design language is **quiet luxury**: editorial serif display type (Cormorant Garamond) paired with
a precise UI face (Inter), generous whitespace, restrained motion, and a calm palette built around
central CSS variables — charcoal ink `#26241F`, warm off-white `#F6F4F0`, and a restrained ink-blue
accent `#33526E` (light) / `#8FB0CB` (dark). The official J-ONE mark keeps its signature yellow
strictly inside the logo; it never dominates the interface. Light and dark themes are first-class,
persisted per visitor.

## Technology stack

- **HTML5** semantics
- **CSS3** with custom properties (design tokens) and a mobile-first, responsive grid
- **Vanilla JavaScript** (ES modules not required; pattern-based IIFE modules on `window`)
- **REST API** via a single centralized layer (`js/api.js`)
- **Progressive Web App** — installable, with an offline fallback and a conservative service worker
  (plain `sw.js` + `manifest.webmanifest`; no Workbox, no bundler, no added dependency)
- No build step at runtime — optional `build.py` inlines shared chrome for zero component-request overhead
- `dev_server.py` — tiny zero-dependency dev server that serves the static site and proxies
  `/api/` + `/media/` to the Django backend, so the site runs exactly as in production (same-origin API)

## Running locally

```bash
# 1. Start the Django backend (see its own README for setup)
cd backend && python manage.py runserver 127.0.0.1:8000

# 2. Serve the frontend with same-origin API proxying
cd frontend && python3 dev_server.py 5500 http://127.0.0.1:8000
# → open http://127.0.0.1:5500
```

In production, host the `frontend/` folder on any static web server (or Django) and expose `/api/`
on the same origin. If the API lives on a different origin, set the public `API_BASE_URL` in
`js/runtime-config.js` at deployment time and allow that exact frontend origin in the backend CORS
configuration. `config.js` itself stays deployment-neutral.

> After editing `components/` (header/footer/mobile-nav), re-run `python3 build.py` to re-inline
> them into the public pages, then `python3 validate.py` to check the result.

## Folder structure

```
frontend/
├── index.html, about.html, rooms.html, room-details.html, facilities.html,
│   gallery.html, offers.html, booking.html, booking-review.html,
│   booking-confirmation.html, contact.html, policies.html, privacy.html,
│   terms.html, cancellation-policy.html, refund-policy.html, login.html,
│   cancellation-result.html, 404.html, 403.html, 500.html
├── dashboard/            # checked-in API-driven staff console pages
├── portal/               # verified-email guest portal login + in-memory-session app
├── components/           # shared public header / footer / mobile-nav partials (inlined at build)
├── css/
│   ├── main.css          # imports + reset + utilities
│   ├── variables.css     # design tokens (brand + semantic palette)
│   ├── themes.css        # light/dark theme overrides
│   ├── typography.css    # type scale
│   ├── components.css    # buttons, forms, cards, tables, modals, toasts, etc.
│   ├── public.css        # header, hero, editorial sections, footer
│   ├── booking.css       # booking flow styles
│   ├── dashboard.css     # staff ops styles
│   └── responsive.css    # breakpoints, print, reduced-motion
├── js/
│   ├── runtime-config.js # deployment-owned public API-origin override (network-only)
│   ├── config.js         # deployment-neutral window.APP_CONFIG defaults + API contract
│   ├── utils.js          # currency/date formatting, DOM + event helpers, debounce/throttle
│   ├── icons.js          # Lucide-style inline SVG icon system (single source)
│   ├── api.js            # central API layer (GET/POST/PUT/PATCH/DELETE, errors, auth, abort)
│   ├── portal.js         # opaque portal-session header transport; no durable credential storage
│   ├── theme.js          # light/dark theme, persisted, flash-guarded
│   ├── ui.js             # toast, modal, confirmation, lightbox, accordions
│   ├── hotel-data.js     # centralized hotel-info hydration (name/address/phone/email/maps/wa)
│   ├── navigation.js     # sticky header, mobile drawer, active links, hotel chrome loader
│   ├── auth.js           # staff login/session, role-aware UI, guards, one-time token refresh
│   ├── contact.js        # public contact/enquiry form (real API submit, duplicate-safe)
│   ├── site.js           # public page controllers (availability search, reveal)
│   ├── pwa.js            # PWA: SW registration, safe updates, install experience
│   └── dashboard.js      # staff dashboard shared behaviours + nav + loading/empty/error states
├── assets/
│   ├── icons/            # logo-official.svg, logo-light/dark, watermark, spinner
│   └── images/           # photography (placeholders until authentic shots are supplied)
├── favicon/              # official favicons (supplied)
├── favicon/icon-192.png, icon-512.png            # PWA install icons (purpose: any)
├── favicon/icon-maskable-192.png, -512.png       # PWA maskable icons (Android adaptive)
├── manifest.webmanifest  # PWA web app manifest (served from the site root)
├── sw.js                 # service worker (MUST stay at the root for scope "/")
├── offline.html          # offline fallback page
├── vercel.json           # Vercel static config: MIME types, cache + security headers
├── robots.txt
├── sitemap.xml
├── build.py              # rebuilds public chrome; versions/PWA-wires public, dashboard and portal pages
└── README.md
```

## How the shared chrome works

The public `<header>`, `<footer>` and mobile-nav exist **once** in `components/`. Each public page
contains markers (`<!--HEADER-->`, `<!--FOOTER-->`, `<!--MOBILE-->`, `<!--JS-->`). Running
`python3 build.py` inlines them into every page at build time, so the deployed site is fully
self-contained with **zero runtime component-request overhead** — single source of truth, no
duplication, no dozens of network requests.

## API configuration

`js/runtime-config.js` loads **before** `js/config.js` on every API-driven public and dashboard
page. Its checked-in default is deliberately portable:

```js
window.__APP_CONFIG__ = { API_BASE_URL: "" }; // same origin: /api/...
```

For a split frontend/API deployment, replace or generate that one **public** file during deployment:

```js
window.__APP_CONFIG__ = { API_BASE_URL: "https://api.example.com" };
```

Use an origin only (no trailing slash), serve `runtime-config.js` with `Cache-Control: no-store`,
and configure `CORS_ALLOWED_ORIGINS` on Django with the exact frontend origin. The service worker
never caches this file, so a corrected environment value takes effect without a service-worker race.
`config.js` owns application defaults and endpoint paths; do not put environment-specific hosts in it.

A reverse proxy that serves the static frontend and `/api/` from one public origin needs no override.
`dev_server.py` uses this same default locally by proxying `/api/` to Django.

### Public, management, and guest origins

The public site, `/dashboard/`, and `/portal/` can share one static origin or
be deployed on distinct origins without introducing another backend or database.
Each deployment serves its own **public** `runtime-config.js` pointing at the
same API origin. For example, if they are split, configure the backend with
literal scheme-and-host values (no paths, trailing slashes, wildcards, or
comma-space ambiguity):

```dotenv
FRONTEND_URL=https://hotel.example.com
PORTAL_FRONTEND_URL=https://guest.example.com
PAYMENT_CALLBACK_URL=https://hotel.example.com/payment-verify.html
DJANGO_ALLOWED_HOSTS=api.example.com
CORS_ALLOWED_ORIGINS=https://hotel.example.com,https://manage.example.com,https://guest.example.com
CSRF_TRUSTED_ORIGINS=https://hotel.example.com,https://manage.example.com,https://guest.example.com
```

`FRONTEND_URL` remains the public booking/email-link origin;
`PORTAL_FRONTEND_URL` is the origin used to generate magic links and falls back
to `FRONTEND_URL` when blank. The management origin is included only when it is
actually separate. Browser CORS is an exact allowlist: add every deployed
browser origin and no others. Do not use `*` and do not turn on credentials to
make a CORS failure disappear.

Public and staff APIs use their established guest/JWT transports. The portal is
different: after a one-use verified-email link is consumed, `portal.js` sends a
short-lived opaque value only in `X-Portal-Session`, with `auth:false`; it is
kept only in active-page JavaScript memory. It is **not** a staff JWT, booking
`X-Guest-Access-Token`, cookie, `localStorage`, or `sessionStorage` value. The
current design therefore needs no cross-origin cookies or CORS credentials.

### Verified-email guest portal

The public footer's **Guest portal** link opens `portal/login.html`; **Your
stays** remains the separate booking-reference/access-token experience. Portal
access is intentionally discoverable but never grants access based on an email
address alone:

1. The guest requests a link for the email used on a stay. The response is
   non-enumerating whether or not that email is eligible.
2. `POST /api/portal/auth/consume/` uses the one-use, short-lived link token.
   The login page removes `?token=` from browser history **before** network work.
3. The returned opaque session crosses to `portal/index.html` in a URL fragment;
   the portal immediately removes that fragment and holds the value in memory.
   A refresh deliberately requires reopening a secure link.
4. Portal requests carry only `X-Portal-Session`; logout revokes that server-side
   session. The portal has no token persistence and does not poll.

The overview is bounded. Folios and postings are requested only when the guest
opens the folio area, with pagination; service requests are likewise paginated.
New requests are allowed only for an email-owned active in-house stay and carry
an idempotency key. The portal never exposes staff queues, arbitrary room IDs,
or other guests' records.

Portal HTML is `noindex, nofollow` and must be served with `Cache-Control:
no-store`. The service worker also treats `/portal/` as network-only; it never
reads or writes portal pages to Cache Storage. Apply equivalent rules on any
host, not merely the included optional static-host configuration.

**Never place secrets in frontend code.** The frontend holds no Django `SECRET_KEY`, no database
credentials, no Paystack secret key, and no Backblaze B2 private keys. Storage credentials live
only on the backend; the frontend receives safe public/signed URLs.

## API endpoints consumed

Endpoints are centralized in `js/api.js`. Reference contract:

| Purpose                      | Method | Path                                              |
|------------------------------|--------|---------------------------------------------------|
| Rooms list                   | GET    | `/api/rooms/`                                     |
| Room detail                  | GET    | `/api/rooms/{slug}/`                              |
| Availability (authoritative) | GET    | `/api/availability/`                              |
| Offers                       | GET    | `/api/offers/`                                    |
| Facilities                   | GET    | `/api/facilities/`                                |
| Gallery                      | GET    | `/api/gallery/`                                   |
| Hotel info / settings        | GET    | `/api/hotel/`, `/api/settings/`                   |
| Create booking               | POST   | `/api/bookings/`                                  |
| Booking by reference         | GET    | `/api/bookings/ref/{ref}/`                        |
| Payment initialization       | POST   | `/api/bookings/{ref}/pay/`                        |
| Payment status (authoritative)| GET    | `/api/bookings/{ref}/payment-status/`             |
| Enquiry / contact            | POST   | `/api/enquiries/`                                 |
| Auth login / logout / me     | POST/POST/GET | `/api/auth/login/`, `/api/auth/logout/`, `/api/auth/me/` |

The actual backend contract should be verified against your Django API and adapted in `js/api.js`
if response shapes differ. **The backend is the single source of truth** for availability, pricing,
payment status and confirmation. The frontend never computes authoritative totals or success on its own.

Portal endpoints are intentionally isolated in `js/portal.js`, not staff `Auth`:

| Purpose | Method | Path / transport |
|---|---|---|
| Request / consume magic link | POST | `/api/portal/auth/request/`, `/api/portal/auth/consume/` — public |
| End portal session | POST | `/api/portal/auth/logout/` + `X-Portal-Session` |
| Bounded overview | GET | `/api/portal/me/` + `X-Portal-Session` |
| Folios and statement postings | GET | `/api/portal/folios/…` + `X-Portal-Session`, requested lazily and paginated |
| Guest service requests | GET / POST | `/api/portal/requests/…` + `X-Portal-Session`; creates include an idempotency key |

### Staff / dashboard resources — one integration point

Staff endpoints are **not hard-coded**. They live in ONE clearly-marked block in `js/config.js`
under `APP_CONFIG.API_ENDPOINTS`. Until any entry is filled in, the matching dashboard page shows
an honest **"not configured" error state** instead of fake records or a guessed URL. Fill them to
match your backend (they are staff-authenticated, unlike the public endpoints):

```js
window.APP_CONFIG = {
  API_ENDPOINTS: {
    bookings:      "/api/staff/bookings/",
    booking:       "/api/staff/bookings/{id}/",
    guests:        "/api/staff/guests/",
    rooms:         "/api/staff/rooms/",
    availability:  "/api/staff/availability/",
    checkIns:      "/api/staff/check-ins/",
    checkOuts:     "/api/staff/check-outs/",
    payments:      "/api/staff/payments/",
    receipts:      "/api/staff/receipts/",
    enquiries:     "/api/staff/enquiries/",
    notifications: "/api/staff/notifications/",
    auditLogs:     "/api/staff/audit-logs/",
    reports:       "/api/staff/reports/",
    stats:         "/api/staff/stats/",
    settings:      "/api/settings/",
    facilities:    "/api/staff/facilities/",
    offers:        "/api/staff/offers/",
    gallery:       "/api/staff/gallery/"
  }
};
```

The API layer exposes generic `API.list/getOne/create/update/remove(resource, ...)` helpers
(resolved from that map) so dashboard pages don't invent paths. Every dashboard page loads real
data with loading / empty / error states, and mutations (check-in, check-out, cancel, delete,
save) only show success **after** the backend responds successfully.

> **Dashboard regeneration:** `dashboard/build_dash.py` is intentionally disabled.
> The checked-in dashboard HTML files are the source of truth and use centralized API helpers,
> real loading/empty/error states, and backend-confirmed mutations. If generation is needed again,
> rebuild the generator around those same API contracts before enabling it.

## Authentication

- Public pages are unauthenticated.
- Staff sign in on `login.html`. Tokens are kept in `sessionStorage`; the user profile is stored in
  `localStorage` for instant UI rendering.
- `Auth.guard()` on each dashboard page redirects unauthenticated users to login, and shows a 403
  for insufficient roles. **Role checks are UX-only** — the backend enforces authorization.
- The API layer automatically maps `401` to a "session expired" message and routes to login.

## No fake data / backend as source of truth

The frontend never fabricates production records. Specifically:

- **No hard-coded rooms, guests, bookings, payments, receipts, offers, facilities or notifications**
  as fallbacks. Every API-driven view renders a real loading state, a meaningful empty state, or an
  error state. If an endpoint is down or not configured, it shows that — never a fake list.
- **No client-side booking references** (`Math.random()`), and no client-computed authoritative
  `room_price` / `total` / `amount` / `discount` passed through the URL or trusted for payment.
  The backend validates availability, computes the total, and returns the reference.
- **No `?demo=1` bypass** in the staff dashboard — it always requires real authentication.
- **Hotel contact info** (address, phone, email, WhatsApp, Google Maps) hydrates from **one**
  centralized settings source via `[data-contact-*]` / `[data-hotel-*]` elements; if the Maps URL is
  not configured the CTA is hidden, never pointed at a placeholder.
- **Success is only shown after the backend confirms.** No toast-only "saved / deleted / sent /
  checked in / paid" until the request succeeds.
- **No secrets in the frontend** — Django/Paystack/B2/SMTP credentials live on the backend only.

## Booking & payment flow

1. Search availability (backend-authoritative).
2. Select an available room (dates/guests/room persist cleanly in the URL and a local draft).
3. Enter guest information.
4. Review the summary and policies.
5. **Pay** — the frontend asks the backend to initiate payment, receives the Paystack
   `authorization_url`, redirects there, and then **polls the backend for the authoritative
   payment/booking status**. It never treats a redirect back as success by itself.
6. Confirmation + receipt are shown only after backend confirmation. Receipt actions export **only the receipt sheet** as image/PDF (or print only the receipt), never the full page chrome.

## Progressive Web App (PWA)

J-ONE installs as a real app — **J-ONE HOTEL & LODGE** — on Android and iOS, while remaining the
exact same multi-page HTML/CSS/vanilla-JS site. No framework, no build step, no dependencies were
added.

### Versioning, cache busting and the "new version" modal

`version.json` at the frontend root is the **single source of truth** for the
deployed version. `build.py` owns it:

```bash
python3 build.py --bump patch   # or --version 2.0.0
```

On every release, `build.py` updates `version.json`, regenerates
`js/version.js` (the `window.JONE_VERSION` marker public/dashboard pages carry),
syncs the service-worker `CACHE_VERSION`, and stamps every local `js/…`/`css/`
reference in every intentional HTML page, including `/portal/`, with
`?v=<version>` (so a fresh deploy can never serve stale cached JS next to new
HTML). It injects `js/version.js` + `js/update-checker.js` into public and
dashboard pages only: portal pages deliberately omit reload-capable update
controllers because their opaque session is memory-only.

`js/update-checker.js` compares the loaded `window.JONE_VERSION` against the
live `version.json` (fetched `no-store`, at most every 15 minutes / 5 minutes
on tab focus). When they differ it shows the accessible
**"A new version is available"** modal with a *Refresh now* button — and
nothing else: it never auto-reloads, never clears booking drafts or auth
storage, stays silent during booking/payment flows and while a form or critical
operation is active, and never re-prompts for a version the user has already
dismissed. It is intentionally absent from the memory-session portal.

### Portal release checklist

1. Confirm `FRONTEND_URL`, `PORTAL_FRONTEND_URL`, `API_BASE_URL`,
   `DJANGO_ALLOWED_HOSTS`, and every exact browser entry in
   `CORS_ALLOWED_ORIGINS` are correct for the target environment.
2. Confirm the static host returns `Cache-Control: no-store` and
   `X-Robots-Tag: noindex, nofollow` for `/portal/**` (the included optional
   `vercel.json` does this).
3. Run `python3 build.py --bump patch`, then `python3 validate.py` and
   `node --test tests-js/`. The build stamps portal assets but must not add
   `pwa.js` or `update-checker.js` to portal HTML.
4. Deploy the compatible API/migrations before—or atomically with—the static
   release. Check the backend health endpoint, request a real staging magic
   link, then verify consume, a portal read, logout, and refresh behavior.

### The guiding rule

> The Django backend is the **only** source of truth. The service worker never caches, and never
> answers, a single `/api/` request.

A stale room-availability or payment response would be a serious operational and financial problem,
so caching is deliberately conservative: static shell assets only.

### Files

| File | Purpose |
| ---- | ------- |
| `manifest.webmanifest` | Web app manifest (name, icons, `start_url`, `scope`, shortcuts) |
| `sw.js`                | Service worker — **must remain at the site root** |
| `offline.html`         | Branded offline fallback page |
| `js/pwa.js`            | The *only* place that registers the SW; also the install UI |
| `favicon/icon-*.png`   | 192/512 install icons + maskable variants, generated from the official mark |

### Manifest

- `name`: `J-ONE HOTEL & LODGE` · `short_name`: `J-ONE Hotel`
- `start_url`: `/index.html` — the installed app opens the **public hotel website**, never a
  dashboard route (which would be a broken, login-gated entry point).
- `scope`: `/` — the whole site, so in-app navigation to `/rooms.html`, `/booking.html` etc. stays
  inside the installed app.
- `display`: `standalone`; `theme_color` `#373435`; `background_color` `#F8F9FA`.
- Icons: 192×192 and 512×512 with `purpose: "any"`, plus separate **maskable** icons whose artwork
  sits inside the Android safe zone so the mark is never clipped or distorted.

Every page — root, `/dashboard/`, and `/portal/` alike — references it with the **root-absolute** path
`/manifest.webmanifest`, so one identical tag is correct at every directory depth.

### Service-worker caching strategy

| Request | Strategy | Why |
| ------- | -------- | --- |
| Precached shell (CSS, core JS, icons, `offline.html`) | Cache-first | Small, versioned, safe to serve stale |
| Public HTML pages | **Network-first**, cached copy as fallback | Stays fresh; still readable offline once visited |
| Local images | Stale-while-revalidate, **capped at 40 entries** | Fast repeat views without unbounded growth |
| `/api/**` | **Never intercepted** | Availability, bookings, payments, auth — always live |
| `/media/**` | Never intercepted | Backend-owned uploads |
| `/dashboard/**` | **Network-only** (no cache read or write) | Staff data must never persist in a shared cache |
| `/portal/**` | **Network-only** (no cache read or write) | Verified guest portal pages and link flow must never persist in shared cache |
| Non-GET (POST/PUT/PATCH/DELETE) | Never intercepted | Bookings/payments are never cached or queued |
| Cross-origin (Paystack, Maps, the API host) | Never intercepted | Third-party flows untouched |

**Deliberately never cached:** room/booking availability, booking creation, payment initialization,
Paystack responses, payment verification/status, guest and staff records, dashboard statistics,
notifications, financial records, receipts, audit logs, authentication responses, access/refresh
tokens, and any request carrying an `Authorization` header.

### Offline behaviour

- A page you have already visited opens from cache.
- A page you have not visited falls back to **`offline.html`** — *not* `index.html`; this is a
  multi-page site, so URLs are never rewritten to an SPA shell.
- `/dashboard/*` and `/portal/*` offline show the branded offline page (containing no staff or
  guest data) rather than the browser's error screen.
- Booking, availability and payment actions are blocked with a plain-language message
  ("An internet connection is required to complete a booking…"). **Nothing is ever queued for
  later** — a silently deferred booking or payment would be unsafe.

### Update strategy

`sw.js` starts with:

```js
const CACHE_VERSION = "jone-v1";
```

Release with `python3 build.py --bump patch` (or an explicit `--version X.Y.Z`) rather than editing
this value by hand. The builder keeps `version.json`, `js/version.js`, `CACHE_VERSION`, and every
local JS/CSS asset stamp in lockstep. On activation the worker deletes every cache that does not
belong to the current version, so visitors are never stuck on obsolete files.

The new worker deliberately does **not** call `skipWaiting()` by itself. It waits until the page
tells it to, and `js/pwa.js` only does so when the visitor is idle — never during a booking,
payment, check-in/out, or with a dirty form. A quiet "Refresh to update" toast is offered instead.
Long-running flows can lock updates explicitly:

```js
JONE.pwa.beginCriticalFlow();   // e.g. entering the payment step
JONE.pwa.endCriticalFlow();     // when it completes or aborts
```

### Installation

**Android (Chrome/Edge)** — `beforeinstallprompt` is captured and the default mini-infobar
suppressed. A hotel-branded "Install J-ONE Hotel App" control appears in the mobile drawer and the
footer, plus a dismissible banner on the homepage only (dismissal is remembered for 30 days).
Nothing is shown once the app is installed.

**iOS/iPadOS (Safari)** — iOS exposes no install event, so tapping the same control opens
instructions: **Share → Add to Home Screen**. Android-specific wording is never shown to iOS users.
`apple-touch-icon` and the Apple meta tags are in place for the home-screen icon, title and status
bar.

The install UI uses the project's existing Lucide SVG icons (no emoji), real `<button>` elements
with accessible names and visible focus states, and works in both light and dark themes.

### Testing the PWA locally

```bash
cd frontend && python3 dev_server.py 5500 http://127.0.0.1:8000
# → http://127.0.0.1:5500   (localhost counts as a secure context)
```

In Chrome DevTools → **Application**: check *Manifest* (no errors, icons render), *Service Workers*
(activated, scope `/`), and *Cache Storage* (only `jone-v*` caches — confirm no `/api/` or
`/dashboard/` entries). Use the **Offline** checkbox in the Network panel to exercise the fallback.

> `file://` will not work — service workers require `http://localhost` or HTTPS.


## Theme system

Light/dark themes are applied via `data-theme` on `<html>` with all colors from CSS custom
properties. The choice persists in `localStorage` and respects the OS preference when no explicit
choice is made. A `preload-theme` class on `<html>` avoids any flash of the wrong theme.

## Logo / branding

The site uses the **official** `assets/icons/logo-official.svg` (a pure-vector mark) in the header,
footer and the staff console, rendered at its intrinsic aspect ratio (never stretched, cropped,
recoloured or re-drawn) next to an HTML/CSS "J·ONE" wordmark that adapts to the active theme via
CSS variables.

`assets/icons/logo-light.svg` and `logo-official.svg` are supplied, but they render their wordmark with
a font-dependent `<text>` element (not baked vector paths), so cross-platform rendering can vary.
**Recommendation:** supply a baked, path-only full logo for the dark and light headers if you want
pixel-perfect variant switching. Until then the correct, official mark asset is used and the
wordmark is styled text (no recreated logo art). Because SVG `<text>` rendering can't be visually
verified in this environment, no full-logo variant is asserted as pixel-perfect here.

## Image handling

- Authentic photography is added to `assets/images/` and served by the backend/media URLs as they
  become available.
- Until then the site shows clearly-labelled **placeholders**, never misrepresented stock imagery.
- Images use `loading="lazy"`, explicit `width`/`height`, and `fetchpriority="high"` only for the hero.

## Backblaze B2

The backend handles B2. The frontend never contains `B2_APPLICATION_KEY` / `B2_APPLICATION_KEY_ID`
or any private storage credentials, in HTML, CSS, JS, `.env` shipped to the browser, or in API responses.
On upload, the frontend calls a backend endpoint that returns upload instructions and safe URLs.

## Local development

For a functional booking/dashboard flow, use the small same-origin proxy (no frontend build service
or local URL embedded in the shipped app):

```bash
cd frontend
python3 dev_server.py 5500 http://127.0.0.1:8000
```

A plain static server is sufficient for visual-only work. Run `python3 build.py` after editing
`components/` so public chrome stays inlined.

## Production hosting

Any static host, reverse proxy, or Django static setup works. Prefer exposing the frontend and
`/api/` on the same public origin. For separate origins, provision `js/runtime-config.js` with the
public API origin, set no-store caching for that file, and allow each exact browser origin in Django
CORS. Dashboard folders remain regular static HTML/JS/CSS; access control is enforced by the API.
Serve `/portal/**` with `Cache-Control: no-store` and `X-Robots-Tag: noindex, nofollow` as well as
the existing service-worker network-only rule; it contains a one-use-link flow and intentionally
memory-only opaque session.

### Vercel (optional static-host example)

`vercel.json` is an optional static-host example. It does not select or require a backend provider:
Django remains the authoritative API wherever it is deployed. Provision `runtime-config.js` with a
same-origin empty value or a public API origin for that environment.

| Vercel setting      | Value                      |
| ------------------- | -------------------------- |
| Framework preset    | **Other** (no build step)  |
| **Root directory**  | **`frontend`**             |
| Build command       | *(leave empty)*            |
| Output directory    | *(leave empty)*            |
| Install command     | *(leave empty)*            |

The root directory **must** be `frontend`, because `sw.js` and `manifest.webmanifest` have to be
served from the deployed site root (`/sw.js`, `/manifest.webmanifest`) for the service worker to
control the whole site.

`vercel.json` (in `frontend/`) supplies:

- the correct MIME types — `application/javascript` for `/sw.js`,
  `application/manifest+json` for `/manifest.webmanifest`;
- `Service-Worker-Allowed: /` so the worker's scope is the entire site;
- `Cache-Control: must-revalidate` on HTML, `sw.js` and the manifest, so a deploy is picked up
  immediately, and longer caching for `/assets/` and `/favicon/`;
- `Cache-Control: no-store` for deployment-owned `/js/runtime-config.js` and everything under
  `/dashboard/` or `/portal/`, plus `noindex, nofollow` headers for the operational routes;
- baseline security headers (`X-Content-Type-Options`, `Referrer-Policy`, HSTS, etc.).

`cleanUrls` is deliberately **`false`**: every internal link in this project points at an explicit
`*.html` file, so enabling it would add a 308 redirect to every navigation. There are **no
`rewrites`** — this is a multi-page site, not an SPA, and each `.html` file must be served directly.

> **HTTPS is required.** Service workers only register on secure origins. `*.vercel.app` and any
> custom domain with a Vercel certificate satisfy this; `localhost` is treated as secure for
> development.

## Performance

- Minimal JS/CSS, small DOM, no unnecessary dependencies.
- Single reusable icon system (inline SVG — no icon font).
- Component chrome inlined at build (no component-request fan-out).
- Lazy media, debounced input, throttled scroll, `AbortController` timeouts on API calls.
- `<prefers-reduced-motion>` respected; light theme/dark theme via CSS variables only.

## Accessibility

- Semantic landmarks, labelled forms, keyboard-friendly modals/lightbox (Escape, trap focus),
  visible focus states, aria-live toasts, skip link, sufficient contrast, reduced-motion support.
- PWA install controls are real `<button>`s with accessible names, Lucide SVG icons (no emoji),
  visible focus rings and WCAG AA contrast in both themes; the offline page is keyboard-navigable
  with an `aria-live` connection status.

## Troubleshooting

- **Service worker not updating / stale assets** — release with
  `python3 build.py --bump patch`; it updates `CACHE_VERSION` in lockstep with
  page asset stamps. Do not edit the cache version by hand. Locally, use
  DevTools → Application → Service Workers → *Update on reload* / *Unregister*.
- **Manifest 404 or "not installable"** — the Vercel **root directory must be `frontend`** so that
  `/manifest.webmanifest` and `/sw.js` resolve at the site root. Installation also requires HTTPS.
- **Blank content / "Unable to reach servers"** — the API origin is incorrect or the backend is down.
  Verify the deployed `runtime-config.js` `API_BASE_URL`, its no-store response header, and backend
  CORS. Public pages show a clear error state (or hide a section) rather than inventing records.
- **A dashboard page says "not configured"** — that staff endpoint hasn't been filled in under
  `APP_CONFIG.API_ENDPOINTS` yet. Add the matching backend path.
- **Staff pages bounce to login** — the session token expired; sign in again. The dashboard always
  requires real authentication (there is no `?demo=1` bypass in production).
- **Maps link hidden** — no real `google_maps_url` is configured in backend settings; the frontend
  reads a single hotel-settings source and hides the CTA rather than defaulting to a placeholder link.

## License / ownership

© J-ONE HOTEL & LODGE. Internal project — do not redistribute brand assets.
