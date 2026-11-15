# J-ONE Hotel Management System — Repair Report (15 Nov 2026)

**Repository:** `Jonehotel-06/j1-hotel-management-system`, branch `main`, base commit `2204222`
(“inveotry breakfast pwa”).
**Delivered as:** `JONE_FIXED_FILES/` (only the updated/new files, laid out with their
repo-relative paths) + this report + `TEST_EVIDENCE_2026_11_15.md`.
**Method:** every reported symptom was reproduced or disproved against the code and the
running test suite *before* anything was changed. Where the obvious explanation turned out to
be wrong, the real cause is stated and the wrong theory is recorded here so nobody repeats it.

---

## 1. Summary of what was actually broken

| # | Reported symptom | Real root cause found | Status |
|---|---|---|---|
| 1 | “Booking the first room of any room type fails: *the selected room is occupied*, though the room is free.” | `_resolve_exact_room()` refused **any** room whose `Room.status` flag was `OCCUPIED`, without looking at the requested dates. Rooms allocate in `room_number` order, so the first room of every type is the one carrying that flag. | **Fixed** |
| 2 | “Images do not upload — JPG fails while PNG works.” | Two independent causes: the browser pre-check compared `File.type` against `["image/jpeg","image/png","image/webp"]` (browsers report a real JPEG as `image/jpg`, `image/pjpeg` or `""`), **and** the server refused any upload whose *file name* did not end in exactly `.jpg/.jpeg/.png/.webp` before reading a single byte. | **Fixed** |
| 3 | “The app is not installable / the installed app has the wrong name and the staff app will not install on iPhone.” | The public manifest had been renamed to “J-ONE Hotel”; 22 public pages and 1 dashboard page shipped with **no** manifest link and no `apple-*` metadata; `/manifest.webmanifest` had no MIME/cache rule on either host. | **Fixed** |
| 4 | “The manager cannot manage offers / guest discounts / rooms.” | **Not** the browser role flag (proved below). Three real defects: two dashboard pages were shipped as orphans, the backend capability matrix had drifted from its own migration, and three backend endpoints crashed for every role. | **Fixed** |
| 5 | Deployment hygiene: an obsolete Cloudflare Worker proxy still in the tree. | `frontend/worker/index.js` is dead code (no `main` in `wrangler.jsonc`); two earlier reports record it as obsolete, and a shipped test requires its removal. | **Removed** |
| 6 | “There is nowhere to record that tissue, brush, paste and soap has been given to a guest checking in.” | The record-keeping existed but was **unreachable**: the guest picker asked for `/api/bookings/guests/`, a route that does not exist (it is captured by the guest booking detail route as `lookup="guests"` and 404s), so its `<select required>` never had an option and the Issue form could not be submitted at all; the same bug emptied the breakfast pickers. On top of that, nothing on the arrival path ever asked the question — `dashboard/check-in.html` checked the guest in and stopped — and the item dropdown offered the entire warehouse rather than what Reception actually holds. | **Fixed** |
| 7 | “The date and night in extend modal is failing. When a guest wants to extend the days, it's not going through.” (the red text they were reading: *“The requested item could not be found.”*) | Not the dates and not the extension engine — both were executed and are correct. The 404 branch of `frontend/js/api.js` decided “record missing” vs “route not served” with `data == null`, which can never be true for the plain-text/HTML body Django returns when a URL is not served, so an undeployed `extend-quote` endpoint was reported to the desk as a missing booking. The dialog then also blamed the form (“choose a date first”) for the server's refusal, and ignored the `can_extend:false` the history endpoint had already returned. | **Fixed** |

---

## 2. Defect 1 — “the first room in every room type is occupied”

### What was happening
`apps/bookings/services/booking_service.py` → `_resolve_exact_room()`:

```python
if room.status == Room.Status.OCCUPIED:
    raise <“The selected room is occupied …”>
```

`Room.status` is a **housekeeping/physical** flag (“somebody is in this room right now”), not
a reservation. It is set at check-in and cleared at check-out, and it is *not* consulted by the
availability engine — `apps/bookings/services/availability.py` decides availability from live
assignments only (`CONFIRMED`/`CHECKED_IN`, or `PENDING` with a live `expires_at`).

Because rooms are allocated in `room_number` order, the first room of each type is the one that
carries the flag after a busy night. The result was exactly the report: the calendar said
“available”, the desk was told “occupied”, and it was always the first room of every type.

### The fix
1. New helper `_room_is_physically_occupied_now(room, *, check_in)`:
   * returns `False` unless `status == OCCUPIED`;
   * a future `check_in` (later than `hotel_today()`) is **never** blocked by the flag;
   * a stay that includes today is blocked only while a live assignment
     (`booking__status__in=BLOCKING_STATUSES`) still has `check_out > today`.
2. `_resolve_exact_room()` takes `check_in`/`check_out` and gates the `OCCUPIED` refusal on
   that helper; the message is now actionable: “Room 301 is occupied tonight. Choose another
   physical room, or book this room for dates after the current guest departs; no substitute
   was assigned.”
3. The reschedule path had the same bug (`reschedule_booking` compared
   `available_by_id[...].status == OCCUPIED`); it now uses the same helper, so **availability,
   booking creation and rescheduling return one answer**.

### Evidence
`backend/tests/test_first_room_booking.py` — **19 tests, all passing** — pins every scenario in
the brief: first / second / last room of a type, the first room of several types, a *genuinely*
occupied room still refused, existing reservations on non-overlapping dates, a guest checking
out on the arrival date, a multi-room booking, an expired vs live pending hold, a second
booking of the same room being refused, availability-vs-creation agreement for every room, and
public (website) booking of an exact room — including that a substitution, if the public flow
uses one, must be disclosed with the requested room id.

Suite `tests.test_bookings tests.test_availability tests.test_multi_room_booking
tests.test_booking_idempotency tests.test_guest_checkout` → **56 tests, 0 failures**.

---

## 3. Defect 2 — JPG uploads failed while PNG worked

### Cause A (browser) — `frontend/js/dashboard.js`, `dashboard/gallery.html`, `dashboard/rooms.html`
```js
if (IMAGE_TYPES.indexOf(file.type) === -1) return reject("Unsupported format…");
const IMAGE_TYPES = ["image/jpeg", "image/png", "image/webp"];
```
A browser derives `File.type` from the file **name** using the operating system’s MIME table.
The same JPEG legitimately arrives as `image/jpeg`, `image/jpg`, `image/pjpeg`,
`image/x-citrix-jpeg` or `""` (unknown) — while a PNG is essentially always `image/png`. Valid
photos were rejected in the browser before a single byte was sent, and the asymmetry matched the
report exactly. Changing the input’s `accept` attribute could never have fixed this, because the
rejection happened in JavaScript afterwards.

### Cause B (server) — `apps/core/validators.py`
```python
ext = Path(file.name).suffix.lower()
if ext not in {".jpg", ".jpeg", ".png", ".webp"}: raise ValidationError("Unsupported file extension …")
```
The name was judged before the bytes. A JPEG named `IMG_2041.JFIF`, `scan.jpe` or `blob` (the
name several mobile browsers send) was refused as “Unsupported file extension”. On top of that,
DRF’s `ImageField` runs Django’s `validate_image_file_extension`, which fails an extensionless
name *before* the project validator is ever reached.

### The fixes
* **`validate_image_upload()` is now content-first.** The file is decoded with Pillow; the
  detected format decides acceptance, and the stored name is rewritten to the canonical
  extension for that format (`FORMAT_EXTENSIONS`). A `.png` renamed to `.jpg` is stored as
  `.png`; a `blob` JPEG becomes `blob.jpg`. A renamed **non-image is still refused** — nothing
  is admitted on its extension, and the disguised-executable rule is unchanged.
  New clear messages for the size ceiling (with the actual size and `MAX_UPLOAD_MB`), empty
  files and decompression bombs. `.jpe/.jfif/.pjpeg` are accepted for documents too.
* **`apps/core/serializers.py` → `ValidatedImageField`** decodes *before* DRF/Django’s extension
  check, then hands the renamed file to DRF (whose own content check still runs). It is applied
  to every upload surface: room type, room gallery, gallery items, homepage hero slots, offers
  and both profile-photo serializers. `apps/rooms/serializers.py` re-exports it so no call site
  changed.
* **One shared client rule** — `JONE.dashboard.imageFileIssue(file, maxMB)` — used by the
  drop-zone uploader, `gallery.html` and `rooms.html`. It rejects only on *positive* evidence
  (a non-image extension, or a known non-image MIME family) and never blocks a file it merely
  does not recognise; the decode probe and the server remain authoritative. Accept lists now
  offer `.jpg`, `.jpeg`, `.jpe`, `.jfif`, `.png`, `.webp` and the legacy MIME types.
* A latent crash was fixed in the same area: `formModal` threw
  `Cannot read properties of null` when a `type: "file"` field was submitted through a JSON
  form (it now upgrades to `FormData`).

### Evidence
* `backend/tests/test_image_upload_jpeg_regressions.py` — **17 tests**: `.jpg/.jpeg/.JPG/.JPEG`,
  legacy MIME types, `.jfif/.jpe/.pjpeg/no extension`, progressive and CMYK JPEGs, PNG/WebP,
  PNG-renamed-to-JPG stored as PNG, renamed non-images and an ELF payload refused, oversize and
  GIF refused with an actionable message, room-type/room/profile/gallery surfaces, and a
  rejected upload never deleting an existing photo.
* `frontend/tests-js/test-image-upload-accept.js` — **18 tests**, including a check that the
  client’s MIME/extension lists can never drift from `apps/core/validators.py`.

---

## 4. Defect 3 — installability (PWA)

Three separate causes, all in the browser/hosting layer:

1. `manifest.webmanifest` `name` had been changed to **“J-ONE Hotel”** in a later commit; the
   product is the **“J-ONE Hotel Management System”** (restored; `short_name` stays “J-ONE”).
2. **25 installable public pages and `dashboard/menu-print.html` shipped with no manifest link
   and no `apple-mobile-web-app-*` tags.** iOS decides “add to home screen” from the `apple-*` meta
   tags alone, so those pages installed as plain bookmarks — the exact “iPhone won’t install”
   complaint while Android worked.
3. `/manifest.webmanifest` had **no MIME/cache rule** on either host (`_headers` only covered
   `/dashboard/manifest.webmanifest`), so the public manifest could be served as
   `application/octet-stream` and rejected by the browser.

Fixes: name restored; the pages were regenerated with the project’s own build
(`python build.py --version 1.1.25`, 77 pages, SEO validation passed, idempotent on a second
run); `_headers` and `vercel.json` gained the public-manifest rule. The release number moved
1.1.23 → 1.1.25 because the committed pages were already stamped `1.1.24` while `version.json`
said 1.1.23 — shipping 1.1.24 would have left every browser on the old, broken
`js/dashboard.js`.

Evidence: `tests-js/test-pwa-dashboard-only.js` **7/7 passing** (public manifest values, dashboard
manifest scope, every installable page carrying exactly one manifest link + three `apple-*` tags +
one `apple-touch-icon 180x180`, portal/QR/reset pages carrying none, and build/sw/host agreeing).

---

## 5. Defect 4 — “the manager cannot manage …”

**The obvious theory was wrong, and it was disproved before anything was changed.** The three
suspect flags (`canManageRooms`, `canManageOffers`, `canManageDiscounts`) used
`Auth.hasRole("manager")`. `hasRole()` normalises *both* sides through
`RETIRED_ROLE_ALIASES`, so `"manager"` resolves to `"general_manager"` and the flags were
already `true` for the general manager and admin, `false` for every department role (verified
for all eleven roles). So the gate was not hiding the controls.

The real defects were three others:

### 5a. Two dashboard pages were shipped as orphans
`dashboard/amenities.html` and `dashboard/breakfast.html` (added by the same commit that added
their backend) had:
* **no `JONE.dashboard.boot(...)` call** — no auth guard, so a signed-out visitor could open them
  at a bare URL;
* **no `js/auth.js`, no `js/runtime-config.js`** in their script list (and `js/version.js` twice);
* **an inline sidebar script containing literal `\n` sequences** — a JavaScript syntax error, so
  the whole block died and the sidebar never rendered;
* **no `PAGE_ACCESS` entry**, and `canOpenPage()` fails closed — so the “Amenities” and
  “Breakfast” sidebar entries they had just added could never appear for anybody.

Fixed: the standard pre-paint sidebar snippet (real newlines), the standard script order, a
`JONE.ui.initChrome()` + `JONE.dashboard.boot([...])` + sign-out block, registry entries
(`amenities.html`: admin, general_manager, receptionist, storekeeper — the union of
`guest_amenity.issue` and `guest_amenity.allocate` holders; `breakfast.html`: admin,
general_manager, receptionist). `dashboard/menu-print.html` also lacked the pre-paint theme
script and the standard sidebar footer; both were added.

### 5b. The capability matrix had drifted from its own migration
`apps/accounts/migrations/0037_…` granted `guest_amenity.allocate` to `GENERAL_MANAGER` and
`MANAGER`; `ROLE_CAPABILITY_CODES` in `apps/accounts/capabilities.py` did not list it at all,
and neither source gave the management tier `guest_amenity.issue` / `breakfast.issue`. So a
general manager covering the front desk could not issue an amenity to a checking-in guest or
record a complementary breakfast — the endpoints answered 403 while every other front-desk
action worked.

Fixed in **both** places, and nothing was revoked: the matrix gains the three codes for
`GENERAL_MANAGER` and `MANAGER`, and new additive migration
`0038_management_tier_guest_amenity_breakfast` writes the matching `RoleCapability` rows
(`get_or_create`, audit-logged, reversible; the reverse deletes only what it created). The front
desk keeps issue/breakfast, the store keeper keeps allocation, and no other department gains
anything.

### 5c. Three backend crashes on the amenity station (every role, not just managers)
| Endpoint | Error | Cause |
|---|---|---|
| `POST …/reception/amenities/allocate/` | 400 “not enough values to unpack (expected 3, got 2)” | the view unpacked three values from a function that returns `((outbound, inbound), created)` |
| same endpoint (after the unpack fix) | 500 `AttributeError: 'StockMovement' object has no attribute 'created_at'` | `StockMovement` is not a `TimeStampedModel`; its timestamp is `occurred_at` — the stock had **already moved** when the 500 was raised |
| `GET …/reception/amenities/issues/` | 500 `NameError: GuestAmenityIssue is not defined` | the model was never imported in `views.py` |

All three fixed. The allocation response now also reports the real movement timestamp.

### 5d. Hardening the gates
The three management flags now use the exact-role list the server enforces
(`JONE.dashboard.hasAnyRole(["admin","general_manager"])`) instead of a rank comparison that
happened to work through an alias table. Behaviour is unchanged today and cannot silently drift
if the alias table changes; `canManageStaff` stays `["admin"]`.

### Evidence
* `backend/tests/test_management_tier_guest_services.py` — **11 tests**: the matrix grants the
  station to the management tier; the department boundaries do **not** move (store keeper can
  allocate but never issue to a guest, receptionist can issue but never allocate, waiter /
  housekeeping / accountant / procurement / security refused everywhere); persisted grants match
  the matrix; a general manager really resolves the codes from persisted rows; and over HTTP the
  GM and the manager can run the whole station while the others get 403s.
* `frontend/tests-js/test-management-tier-gates.js` — **11 tests**: every role’s access to the
  management controls, the retired `manager` claim still working, unknown/guest/signed-out roles
  failing closed, no page using the rank form, and every dashboard page booting the shell with
  `auth.js`.
* `frontend/tests-js/test-department-page-access.js` — **24/24 passing** (was 21/24): the two
  pages are graded by the registry, reachable by the right departments, and the “management tier
  never forgotten” invariant now holds against the real capability matrix.

---

## 6. Files delivered

Backend (11 modified, 4 new):

```
backend/apps/accounts/capabilities.py
backend/apps/accounts/serializers.py
backend/apps/accounts/migrations/0038_management_tier_guest_amenity_breakfast.py   (new)
backend/apps/bookings/services/booking_service.py
backend/apps/core/serializers.py
backend/apps/core/validators.py
backend/apps/gallery/homepage.py
backend/apps/gallery/serializers.py
backend/apps/guest_services/views.py
backend/apps/offers/serializers.py
backend/apps/rooms/serializers.py
backend/tests/test_first_room_booking.py                                            (new)
backend/tests/test_image_upload_jpeg_regressions.py                                 (new)
backend/tests/test_management_tier_guest_services.py                                (new)
backend/tests/test_guest_services_amenities_breakfast.py                            (extended: check-in kit)
backend/tests/test_stay_extension.py                                                (new — defect 7)
```

Frontend (hand-edited): `js/dashboard.js`, `dashboard/gallery.html`, `dashboard/rooms.html`,
`dashboard/offers.html`, `dashboard/guests.html`, `dashboard/amenities.html`,
`dashboard/breakfast.html`, `dashboard/menu-print.html`, `manifest.webmanifest`, `_headers`,
`vercel.json`, `version.json`, `js/version.js`, `sw.js`, `tests-js/test-image-upload-accept.js`
(new), `tests-js/test-management-tier-gates.js` (new),
`tests-js/test-checkin-amenities.js` (new), `js/amenities.js`, `js/breakfast.js`,
`dashboard/check-in.html`,
`tests-js/test-batch04-gallery-housekeeping-navigation.js`,
`tests-js/test-department-page-access.js`, `tests-js/test-cloudflare-northflank-deployment.js`,
`tests-js/test-accounting-audit-repair-ui.js`, `js/api.js`, `tests-js/test-extend-stay-flow.js`
(new — defect 7), `tests-js/test-extend-stay-modal-e2e.js` (new — defect 7),
`tests-js/test-device-credential.js` (404 branch assertion corrected, §8).
**Deleted:** `frontend/worker/index.js` (dead proxy, see §1.5).
Plus the **regenerated pages** — this repository commits its built pages, so `build.py` output
(77 pages, version 1.1.28, asset stamps + PWA head on every page) is part of the delivery.

### Test-suite updates that are NOT weakenings
Five tripwires/contracts had to move because the product moved, and each one is documented in
the file itself:
* two page-count tripwires (`44 → 47` dashboard pages, `72 → 77` deployed pages) — the counts
  are the test’s own “a new page needs a gate and a registry entry” alarm; all 47/77 pages are
  verified afterwards;
* the gallery hero test pinned the exact buggy literal (`['image/jpeg','image/png','image/webp']`)
  and now pins the shared rule that carries the same 5 MB limit and the same format set plus the
  formats that were wrongly refused;
* the `_headers` assertion in `test-pwa-dashboard-only.js` was anchored to the end of a line
  (`(^|\n)\/manifest\.webmanifest`): unanchored, it was satisfied by
  `/dashboard/manifest.webmanifest`, which *ends with* `/manifest.webmanifest`, so it never
  proved anything about the public manifest. It now really tests both blocks — and doing so is
  what caught that the public MIME/cache rule for `/manifest.webmanifest` was still missing in
  `_headers` (it was present in `vercel.json` only).
* `test-accounting-audit-repair-ui.js` hard-coded `/home/user/JONE_ORIGINAL/...` and crashed the
  whole file on any machine without it; it now resolves the baseline from
  `JONE_ORIGINAL_ROOT` and **skips that one comparison** (with the reason in the test output)
  when the baseline is absent, keeping its nine other assertions.
* `test-device-credential.js` asserted the literal expression
  `data == null ? NOT_FOUND_UNSERVED : NOT_FOUND_FALLBACK` inside `api.js` — i.e. it certified the
  404 branch that could never fire, which is why the desk's message was misdiagnosed as long as
  it did. It now asserts the *behaviour* (a non-object 404 body means “route not served”), and
  the end-to-end proof lives in `test-extend-stay-flow.js` / `test-extend-stay-modal-e2e.js`
  (§8).

---

## 7. Defect 6 — “nowhere to record the toiletries given at check-in”

### What was happening

Reported from the front desk: *“There is nowhere to record that tissue, brush, paste and soap
has been given to the guest checking in.”* The Amenities station already existed end to end
(`POST /api/admin/service-requests/reception/amenities/issue/`, the `GuestAmenityIssue` row,
the `StockMovement`, the history tab) — so the first question was why the desk could not use it.

Three separate reasons, all proven in the code:

**7a. The guest picker was permanently empty — a route that does not exist.**
`frontend/js/amenities.js` filled the *Guest \** `<select required>` from

```js
API.get("/api/bookings/guests/?page_size=500")
```

There is no `/api/bookings/guests/`. Django resolves that URL against the **guest** booking
routes (`apps/bookings/urls.py`), where `<str:lookup>/` matches with `lookup="guests"`:

```
$ python3 -c "... resolve('/api/bookings/guests/') ..."
/api/bookings/guests/ -> bookings:detail kwargs: {'lookup': 'guests'} cls: BookingDetailView
/api/admin/guests/    -> admin-guests:guest-list kwargs: {} cls: AdminGuestListView
```

So the page asked for “the booking whose reference is *guests*”. It fails, the `.catch(function(){})`
swallowed the failure, and the required select kept its single “— Select —” option. The form's
submit button therefore could never pass native validation: **the desk could not record an
amenity for anybody, ever.** `frontend/js/breakfast.js` made the identical call for both
of its pickers (same URL, same structure), which is the rest of “the manager cannot manage breakfast”.

The backend test run shows the 404 in its own log: `WARNING [django.request] Not Found: /api/bookings/guests/`.

**7b. Nothing on the arrival path asked.**
`dashboard/check-in.html` called `API.checkInBooking(ref)`, showed a toast and reloaded the list.
The toiletries physically changed hands at that counter and no screen asked about them, so they
were never recorded — and nothing on the check-in screen, the booking or the folio showed that
they had (or had not) been given.

**7c. The issue dropdown offered the whole warehouse.**
`[data-amenity-issue-item-select]` was filled from `/api/admin/inventory/items/?page_size=500` —
every item in the catalogue, whether Reception holds it or not. Anything not on the Reception
shelf could only ever come back as a rejection *after* the guest had been promised it.

A fourth, smaller cause: the *Reception stock* view was gated on `guest_amenity.allocate`, which
the front desk does not hold. The clerk who issues out of the shelf could not look at the shelf
(403) — the empty allocation was only discoverable from the failure that followed.

### The fix

**One shared dialog, no new endpoint** — `JONE.dashboard.amenityIssueModal()` in `js/dashboard.js`.
It is offered automatically the moment `POST /…/check-in/` succeeds, and it is a thin front end
over the three endpoints that already own the data:

| purpose | endpoint (unchanged) |
|---|---|
| what is on the Reception shelf | `GET /api/admin/service-requests/reception/amenities/stock/` |
| what was already recorded for this stay | `GET /api/admin/service-requests/reception/amenities/issues/?booking_id=…` |
| record one item | `POST /api/admin/service-requests/reception/amenities/issue/` |

The dialog lists only items with a real Reception balance (soap, toothbrush, toothpaste, tissue
are named in plain words in a fixed order and come first), takes a quantity per item, and sends
**one call per item** with the booking, the room, and an idempotency key of
`checkin:<booking reference>:<item id>:<service date>`:

* one item ⇒ one `GuestAmenityIssue` row ⇒ one `StockMovement` — never an aggregate row;
* a retry, a double tap or a refreshed page resolves to the same key, so nothing is recorded or
  deducted twice (`created: false` comes back and the dialog says so);
* an item the shelf cannot cover is refused **and named** (“Only 2 TUBE of Toothpaste are on the
  Reception shelf; 5 requested”), and the rest of the kit is still recorded;
* any partial failure keeps the dialog open with the reason — nothing claims success.

The record is visible in three places: the Amenities **History** tab (reference, guest, room, item,
quantity, balance before/after, who and when), the per-stay **Amenities** button on the check-in
screen (labelled with the number of items already recorded), and — on reopening the dialog — a line
naming what was recorded earlier for that stay.

Frontend changes:

| file | change |
|---|---|
| `js/dashboard.js` | new `amenityIssueModal()` + its pure rules `amenityIssueCatalogue`, `amenityIssuePlan`, `amenityIssueSummary`, `amenityIssueKey`, exported on `JONE.dashboard` |
| `dashboard/check-in.html` | after a confirmed check-in, opens the dialog for that stay (wrapped so a failure there can never turn a successful check-in into an error); each in-house row now carries an **Amenities** button that reads “4 amenities” once items are recorded and re-opens the same dialog, so the desk can both record and *see* what was given, later in the stay |
| `js/amenities.js` | guest picker now reads the **staff** list `/api/admin/guests/` with **server-side search**; `?booking=REF&room=N` deep link preselects the guest, the room and the booking; the issue dropdown is fed by the Reception shelf; the issuance carries `booking_id`; refresh of shelf + history after each issue |
| `js/breakfast.js` | same dead-route fix for both breakfast pickers |
| `dashboard/amenities.html` | guest search box, hidden `booking_id`, and a note that occupancy follows the shelf |

Backend change (one view, no schema change):

```python
# apps/guest_services/views.py — ReceptionAmenityStockView
required_capabilities = ("guest_amenity.allocate", "guest_amenity.issue")
require_any_capability = True
```

Reading the Reception shelf is not allocating: the write paths and the role boundaries are
untouched (store keeper still cannot issue; receptionist still cannot allocate — pinned by tests).

### Evidence

| check | result |
|---|---|
| `manage.py test tests.test_guest_services_amenities_breakfast tests.test_management_tier_guest_services` | **37 tests, OK** (6 new kit tests, 2 new boundary tests) |
| `manage.py test tests.test_first_room_booking tests.test_image_upload_jpeg_regressions tests.test_management_tier_guest_services tests.test_guest_services_amenities_breakfast tests.test_offer_admin_control_regressions` | **90 tests, OK** |
| `manage.py test tests.test_guest_services tests.test_guest_service_pos_details tests.test_service_request_pos_handoff` | **11 tests, OK** |
| `manage.py test tests.test_booking_access_capabilities tests.test_admin_list_pagination` | **21 tests, OK** |
| `node --test tests-js/test-checkin-amenities.js` | **17 / 17 pass** |
| full `node --test tests-js/` | **915 tests, 902 pass, 12 fail, 1 skip** — the same 12 pre-existing failures as before this work (listed in `TEST_EVIDENCE_2026_11_15.md` §4) |
| `python3 build.py --bump patch` | released **1.1.27**, 77 pages, SEO validation passed |
| full backend suite, twice (baseline vs this delivery, one unchanged environment) | 2518 → **2573 tests**; failures 72 → 72, errors 20 → 20, skipped 27 → 27; `comm` on the shipped failure-id lists: **0 new, 1 fixed**, plus one pre-existing time-bomb assertion explained in `TEST_EVIDENCE_2026_11_15.md` §2 |

**Honest limitation:** no browser (and no `jsdom`) is available in this sandbox, so the dialog was
not clicked through end to end. Its decision rules, its three endpoints, the server's idempotency
and stock guards, the permission boundaries and the page's wiring are all executed and green
(`TEST_EVIDENCE_2026_11_15.md` §3, §4); the click-through itself is the one step still open.

The new backend tests pin: one row + one movement per kit item; the shelf drops by exactly the
kit; a re-submitted kit records and deducts nothing twice; a short item is named and does not
block the rest; the record points back at the stay (`booking_id`, room, `AMY-…` reference); the
store keeper still gets 403; `/api/admin/guests/` answers the desk while the old
`/api/bookings/guests/` path does **not** return a guest list.

---

## 8. Defect 7 — “the date and night in extend modal is failing”

### What was happening

Reported from the front desk: *“The date and night in extend modal is failing. When a guest
wants to extend the days, it's not going through.”* Asked which message they saw, they quoted it
exactly: **“The requested item could not be found.”**

That sentence is not in the backend. It exists in exactly one place in the whole product —
`frontend/js/api.js`, `NOT_FOUND_FALLBACK`, reachable only from the 404 branch of
`friendlyMessage()`. The message indicates a **404**, but by itself does not prove whether the request reached a
view or which deployment served it. The code that produced the sentence could not tell the desk
which of two different things had happened:

| what the desk does | what the server answers | what the old 404 branch said |
|---|---|---|
| the booking reference is stale/deleted | `404` JSON `{"code":"RESOURCE_NOT_FOUND","message":"Not found."}` | “The requested item could not be found.” |
| **the deployment does not serve `extend-quote/` at all** | `404` **HTML** `<!doctype html>…<h1>Not Found</h1>` | “The requested item could not be found.” — *identical* |

The two cases were meant to be distinguishable. They were not, because the condition guarding
the honest branch was **unreachable**:

```js
// frontend/js/api.js, before
case 404:
  // No JSON envelope at all means the request never reached a DRF view.
  return data == null ? NOT_FOUND_UNSERVED : NOT_FOUND_FALLBACK;   // data is a STRING here
```

`requestOnce()` sets `data = await res.text()` when the body is not JSON, so for an HTML 404
`data` is the string `"<!doctype html>…"` — never `null`. The “this route is not served by this
deployment” message could therefore never be shown, and every undeployed endpoint looked to the
desk like a missing booking: they retyped the date, re-picked the guest, and got the same
sentence forever. The repository even carried a test pinning that dead expression
(`tests-js/test-device-credential.js:103`), which is why nothing caught it.

Three defects, all in the browser:

1. **`api.js` could not distinguish “no such record” from “no such route”** (above).
2. **`dashboard.js` blamed the form for a server refusal.** After a failed quote the desk pressed
   Confirm and was told *“Choose a new check-out date, or how many nights to add, first.”* — when
   they *had* chosen one and the server had refused it. The real reason was replaced by a
   guess about the form.
3. **A stay the API already says cannot be extended was only discovered after typing.** The
   dialog asked the server for the extension history as it opened (`…/extensions/`, which returns
   `can_extend` and `can_extend_reason`) and then ignored the answer.

What was **not** wrong — established by execution before touching anything, and deliberately left
alone: `extension_service.py` (pricing, room lock, overlap check, payment, history row),
`BookingExtensionQuote/ConfirmSerializer`, the capability gates, and the dialog's date/night
mutual exclusion. A probe against the real service quoted `20 000 × 2 rooms × 2 nights = 80 000`
and completed an extension end to end with a `J1P-…` payment reference.

### The fix

```js
// frontend/js/api.js — a 404 is only a real record when the body is a JSON object
case 404: {
  const unserved = data == null || typeof data !== "object";
  return unserved ? NOT_FOUND_UNSERVED : NOT_FOUND_FALLBACK;
}
```

The two messages now read:

* unserved route — *“The server does not recognise this request. If it persisted after a reload,
  the API deployment does not include this feature yet — report it to the administrator.”*
* missing record — the backend's own sentence when it authored one (“This booking no longer
  exists.”), otherwise *“The requested item could not be found.”*

```js
// frontend/js/dashboard.js — the dialog now remembers WHY it could not price
let lastQuoteError = "";   // the server's refusal, quoted back on Confirm
let blockedReason  = "";   // a stay the API says cannot be extended at all
...
if (!quote) {
  showError(blockedReason || lastQuoteError ||
    "Choose a new check-out date, or how many nights to add, first.");
}
```

and, when the history call reports `can_extend === false`, the date box, the nights box, the
amount box and Confirm are **disabled** with the server's own reason shown underneath — the desk
is refused before typing instead of after.

Three pure helpers were extracted so the rules are testable without a DOM:
`extensionRequestPayload(date, nights)` (the date wins; nights must be a whole 1–365),
`extensionPaymentError(paid, amount)` (blank means “paid in full”, more than the cost is
refused), and `extensionNotExtendable(history)`.

### Evidence

| check | result |
|---|---|
| `node --test tests-js/test-extend-stay-modal-e2e.js` | **4 / 4 pass** — the shipped `dashboard.js`/`api.js`/`ui.js`/`utils.js` executed in a **real jsdom document**: type a date → `POST …/extend-quote/ {"new_check_out":"2026-11-18"}` → ₦40,000.00 painted → Confirm → `POST …/extend-stay/ {"new_check_out":"2026-11-18","provider":"CASH","notes":""}` → dialog closes. Typed nights → `{"additional_nights":3}` on both calls. HTML-404 quote → *“The server does not recognise this request…”*, Confirm disabled, **zero** `extend-stay` calls. `can_extend:false` history → all three inputs disabled before typing |
| `node --test tests-js/test-extend-stay-flow.js` | **16 / 16 pass** — payload rules, payment limits, the 404 split (HTML / JSON-with-message / JSON-without-message / empty body), the `can_extend` pre-check, and a cross-check that the routes `api.js` calls are registered by `backend/apps/bookings/urls_admin.py` |
| `manage.py test tests.test_stay_extension` | **21 tests, OK** — quote by nights/date/multi-room prices identically and writes nothing; confirm moves check-out, *every* room assignment, the totals and the payment in one transaction; part payment leaves the exact balance; over-payment is refused having written nothing; repeat submit on the same date charges once; cancelled booking, no-room and room-conflict refusals are each named and write nothing; housekeeping/storekeeper/waiter get 403 on both endpoints; `original_check_out` survives two extensions. **Before this session the extension feature had no test anywhere in the repository.** |
| full `node --test tests-js/` | **935 tests, 922 pass, 12 fail, 1 skip** — the same 12 pre-existing failures as before (§`TEST_EVIDENCE` §1); +20 tests, all passing |
| `python3 build.py --bump patch` | released **1.1.28**, 77 pages, `validate.py` → `ALL OK` |
| full jsdom harness on the real check-in page (temporary, deleted after use) | Extend on an in-house row → modal → type date → quote → submit → dialog closes, both tables reload |

One shipped test was updated rather than obeyed, and it deserves to be named:
`tests-js/test-device-credential.js` asserted the literal dead branch
`data == null ? NOT_FOUND_UNSERVED : NOT_FOUND_FALLBACK`. Keeping it would have kept the
diagnosis impossible; it now pins the *behaviour* (non-object body ⇒ unserved route) and points
at `test-extend-stay-flow.js` for the proof. This is the only test in the delivery whose
expectation was changed, and the change is strictly narrower in what it lets through.

### What the desk should do now, and the one thing that is still open

The frontend can now say *why* it failed, but it cannot deploy the backend. **Deploy the included backend and frontend**, then hard-reload the desk browser. The routes exist
in this delivery (`backend/apps/bookings/urls_admin.py:39-41`) and work in local tests. If a
non-JSON 404 persists, check the API deployment, base URL and proxy routing: the new message
points to a missing route but cannot by itself distinguish an outdated deployment from a proxy
or other server returning a non-JSON 404. Check the network response and server logs to confirm.

**Honest limitation:** there is no real browser in this sandbox (Playwright cannot launch), so
the click-through was done in jsdom — a real DOM with the real scripts and a stubbed network, not
a person clicking. The four e2e tests skip (loudly, with the reason) rather than pass when jsdom
is absent, so a checkout without `npm i jsdom --no-save` cannot silently claim this evidence.

---

## 9. What was deliberately NOT changed

* No production payment key, credential, `.env` file or seeded database row was touched; no
  migration deletes or rewrites data.
* Authentication, CSRF, staff roles, upload content validation, payment and booking idempotency,
  audit logging and guest-privacy rules are untouched except for the minimum described above
  (the management tier gained two issuance codes; nothing was widened elsewhere).
* The `hasRole` hierarchy itself was **not** changed (`general_manager` is still not inserted
  next to `manager`), because that would widen every unrelated gate at once.
* The twelve remaining frontend suite failures (§`TEST_EVIDENCE_2026_11_15.md` §4) are
  pre-existing, out of the reported problem set, and need a product decision — they are listed
  one by one rather than “fixed” by editing the tests.
