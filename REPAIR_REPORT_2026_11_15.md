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

Backend (10 modified, 4 new):

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
```

Frontend (hand-edited): `js/dashboard.js`, `dashboard/gallery.html`, `dashboard/rooms.html`,
`dashboard/offers.html`, `dashboard/guests.html`, `dashboard/amenities.html`,
`dashboard/breakfast.html`, `dashboard/menu-print.html`, `manifest.webmanifest`, `_headers`,
`vercel.json`, `version.json`, `js/version.js`, `sw.js`, `tests-js/test-image-upload-accept.js`
(new), `tests-js/test-management-tier-gates.js` (new),
`tests-js/test-batch04-gallery-housekeeping-navigation.js`,
`tests-js/test-department-page-access.js`, `tests-js/test-cloudflare-northflank-deployment.js`,
`tests-js/test-accounting-audit-repair-ui.js`.
**Deleted:** `frontend/worker/index.js` (dead proxy, see §1.5).
Plus the **regenerated pages** — this repository commits its built pages, so `build.py` output
(77 pages, version 1.1.25, asset stamps + PWA head on every page) is part of the delivery.

### Test-suite updates that are NOT weakenings
Four tripwires/contracts had to move because the product moved, and each one is documented in
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

---

## 7. What was deliberately NOT changed

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
