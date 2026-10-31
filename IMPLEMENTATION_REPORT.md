# Batch 04 — Implementation report: gallery hero slots, housekeeping statuses, sidebar, reorder hand-off

Repository: `Jonehotel-06/j1-hotel-management-system`
Branch: `batch-04-gallery-housekeeping-navigation` (isolated clone; the original working project was not modified)
Deliverables: `ai_updates/batch_04_gallery_housekeeping_navigation/` (changed and new files only, at their original relative paths)

---

## 1. Summary

| # | Item | Result |
|---|------|--------|
| 1 | Homepage hero images: four independent admin-managed slots | Implemented and verified in the browser |
| 2 | Housekeeping: remove Claim, Assigned and Cancelled from user-facing UI | Implemented; history kept; backend transitions updated |
| 3 | Sidebar: remove the "Service" entry | Implemented as "Service Links" (your choice); page, access rule and APIs kept |
| 4 | Reorder button goes to Procurement | Destination is correct. The real defect was that the store was lost: a low-stock row at the second store produced a draft for the main store. Fixed and verified |

---

## 2. Problems, root causes and fixes

### 2.1 Homepage hero (Task 1)

**Problem.** The admin could only manage one hero image. The public site replaced the whole slideshow with that single image.

**Root cause.** The homepage hero exists only as the `hero` key of `GalleryItem.HomepageSection`. The public slider (`js/site-images.js`) read one `slots.hero` value and, when present, replaced the built-in five-slide carousel with one static slide. There was no upload progress, because the dashboard used `API.put()`, which has no progress support.

**Fix.**
- Added `hero_2`, `hero_3`, `hero_4` as separate `HomepageSection` choices. `hero` keeps its key and its URL, so existing images and configuration are unchanged. Each slot uses its own existing endpoint, `/api/admin/gallery/homepage/<section>/`, so one upload never touches another slot's row or file, and no field is overwritten four times.
- Public slideshow (`js/site-images.js`): builds the slides from whichever of `hero`, `hero_2`, `hero_3`, `hero_4` are set, in that order. If none is set, the built-in five-slide slideshow in `index.html` stays as it was. A single configured slot still gives a static hero, as before.
- Slides after the first keep their URL in `data-src`. `js/carousel.js` promotes it to `src` only for the active and the next slide, so uploads are not all downloaded on page load.
- A broken uploaded image falls back to the shipped image for its position, then is hidden.
- Dashboard (`dashboard/gallery.html`): four hero cards with preview, replace and remove. Each save shows a progress bar and status text. The client check is the same as before: JPEG/PNG/WebP and 5 MB.
- New helper `API.uploadWithProgress()` (`js/api.js`) uses `XMLHttpRequest`, because `fetch` cannot report upload progress. It sends the same auth, request-ID and terminal headers, uses the same error mapping, and does the same single refresh-and-retry on 401. Every other call still uses `request()`.
- Server-side validation is unchanged: `validate_image_upload` with `MAX_UPLOAD_MB` (default 5).

### 2.2 Housekeeping statuses (Task 2)

**Problem.** Claim, Assigned and Cancelled were offered in the UI.

**Root cause.** `task_service._TRANSITIONS` and the status filter and buttons in `housekeeping.html` all included them. `create_housekeeping_task` set `ASSIGNED` when an assignee was given, and `claim_housekeeping_task` moved tasks to `ASSIGNED`.

**Fix (backend).**
- `_TRANSITIONS`: OPEN → {ACCEPTED}; ASSIGNED → {ACCEPTED} (legacy source only); ACCEPTED → {IN_PROGRESS, BLOCKED}; IN_PROGRESS → {READY_FOR_INSPECTION, BLOCKED}; BLOCKED → {IN_PROGRESS}; READY_FOR_INSPECTION → {COMPLETED, IN_PROGRESS}. `_TERMINAL` is unchanged.
- `create_housekeeping_task` always creates `OPEN`. An assignee is still recorded in `assigned_to` and in the `ASSIGNED` event, so the history is kept.
- `assign_housekeeping_task` no longer promotes OPEN to ASSIGNED. Unassigning is allowed for OPEN and for legacy ASSIGNED, and a legacy ASSIGNED row goes back to OPEN.
- `claim_housekeeping_task` was removed from the service. `HousekeepingTaskClaimView` (`POST <reference>/claim/`) now returns **HTTP 410** with code `HOUSEKEEPING_CLAIM_RETIRED`. The task is looked up first, so an out-of-scope reference still returns 404, and no state changes.
- `HousekeepingStatusSerializer` only accepts requestable statuses. ASSIGNED and CANCELLED return 400.
- The `Status` model choices are unchanged, and there is no data migration. Historical rows, CLAIMED events and ASSIGNED events are kept and remain readable.

**Fix (frontend, `dashboard/housekeeping.html`).**
- Status filter: ASSIGNED and CANCELLED options removed.
- Claim button, `claim()` function and its click branch removed. The non-dispatcher toast no longer mentions claiming.
- Transition map: CANCELLED removed; ASSIGNED kept only as a source (mirrors the backend).
- Legacy display: `statusLabel()` shows stored ASSIGNED as "Open" and CANCELLED as "Closed" through `statusPill(status, label)`, in both the row and the detail view.

**Behaviour change (intentional).** Housekeepers can no longer claim an unassigned task. They can still see it in their queue, but they cannot act on it until a dispatcher assigns it.

### 2.3 Sidebar (Task 3)

**Problem.** The sidebar had a "Service" entry that the brief asked to remove.

**Root cause.** Two entries could be meant. You chose **Service Links** (`service-qr.html`, the QR guest-portal links page). The entry was in `NAV` in `js/dashboard.js`, which drives both the desktop and the mobile sidebar.

**Fix.** Removed the single `NAV` line for "Service Links". Kept:
- the page `dashboard/service-qr.html`,
- its page-level access rule (`PAGE_ACCESS` → `MANAGEMENT`),
- its API endpoints in `js/config.js`,
- the `grid` icon, which is still used by Availability and Workstations.

"Guest Requests" (`service-requests.html`) is still in the sidebar. Room-service, guest-service and service APIs and models are untouched.

### 2.4 Reorder button (Task 4)

**Problem.** The Reorder button sends the user to Procurement.

**Investigation.** The button is the intended reorder workflow. It opens the procurement reorder dialog, which calls `GET /api/admin/inventory/procurement/items/<id>/reorder-context/`, and it creates a purchase-order **draft** (`purchaseOrders`) after review. It is not a guessed destination. The `?reorder=` auto-open was also checked, and the access gate and the 403/404 contracts were checked.

**Root cause (real defect).** The inventory Reorder link carried only the stock item id (`procurement.html?reorder=<item>`). The dialog then defaulted to the store with the most stock. So a low-stock row at `E2E-STORE-2` (2 on hand, reorder level 4) opened with **`DEMO-STORE` preselected**, and a draft created from it would have been delivered to the wrong store. Stock reference details were lost on the hand-off.

**Fix.**
- `dashboard/inventory.html`: `reorderHref(itemId, locationId)` adds `&location=<balance.location>`. The row passes its own location.
- `dashboard/procurement.html`:
  - `reorderDefaultLocationId()` uses the linked store when the item is held there, and otherwise keeps the previous default.
  - `reorderLocationInitial()` takes an explicit location id.
  - The auto-open reads `?location=` and accepts digits only. A bad value is ignored.
  - When the dialog came from a balance row, it shows a note with that row's on-hand and reorder level. The note is server data, not a typed or invented figure.
  - The access gate (`if(!__procurementAllowed) return;`) is unchanged, so an unauthorised user never reaches `reorderItem`.
- No backend change. Reorder quantities, unit prices, stock references and the draft flow are unchanged.

---

## 3. Changed and new files

Paths are relative to the repository root. The copies are in `ai_updates/batch_04_gallery_housekeeping_navigation/` at the same paths.

| File | Status | Where / what changed |
|------|--------|----------------------|
| `backend/apps/gallery/models.py` | modified | `HomepageSection` (~line 17): `hero_2`, `hero_3`, `hero_4` added |
| `backend/apps/gallery/migrations/0003_homepage_hero_slots.py` | new | `AlterField` on `homepage_section` choices only |
| `backend/apps/gallery/tests/test_homepage_hero_slots.py` | new | 7 tests for the four-slot behaviour |
| `backend/apps/housekeeping/services/task_service.py` | modified | `_TRANSITIONS` (~line 30); `create_housekeeping_task` status (~line 136); `assign_housekeeping_task` (~line 170); `claim_housekeeping_task` removed |
| `backend/apps/housekeeping/views.py` | modified | `HousekeepingClaimRetired` (~line 112); `HousekeepingTaskClaimView` returns 410 (~line 119) |
| `backend/apps/housekeeping/serializers.py` | modified | `_REQUESTABLE_STATUS_CHOICES` (~line 85) |
| `backend/tests/test_housekeeping.py` | modified | Claim step replaced by dispatch → accept, plus a 410 assertion |
| `backend/tests/test_batch04_housekeeping_statuses.py` | new | 8 tests: OPEN creation, dispatch, legacy rows, refused statuses, 410, history |
| `frontend/js/site-images.js` | modified | Hero builder (`HERO_SLOTS` ~line 91, `bindHeroErrors` ~118, `renderHero` ~138, `applyHero` ~170) |
| `frontend/js/carousel.js` | modified | `hydrate()` (~line 60) called from `go()` (~line 68) |
| `frontend/js/api.js` | modified | `uploadWithProgress()` (~line 1135), exported |
| `frontend/dashboard/gallery.html` | modified | Hero 1–4 cards, progress bar and status, `uploadWithProgress` call (~line 160) |
| `frontend/js/dashboard.js` | modified | One `NAV` line removed (Service Links) |
| `frontend/dashboard/housekeeping.html` | modified | Status filter, transition map and `statusLabel()` (~line 63–65), row buttons, claim removed |
| `frontend/dashboard/inventory.html` | modified | `reorderHref(itemId, locationId)` (~line 77); row passes `row.location` |
| `frontend/dashboard/procurement.html` | modified | `reorderLocationInitial` (~592), `reorderDefaultLocationId` (~601), `reorderItem(itemId, fromLocationId)` (~637), `?location=` auto-open |
| `frontend/tests-js/test-batch04-gallery-housekeeping-navigation.js` | new | 18 static contract tests for Tasks 1–4 |
| `frontend/tests-js/test-inventory-batch01-management.js` | modified | 3 assertions updated to the new signatures (see §6) |
| `frontend/tests-js/test-service-qr-ui.js` | modified | 1 assertion moved from the removed nav entry to the page-level access rule |

No other files were changed. No CSS, no new framework, no image storage change, no change to the public image URLs.

---

## 4. Migrations

- **`gallery.0003_homepage_hero_slots`**: `AlterField` on `GalleryItem.homepage_section`. It changes choices only, and the SQL is unchanged apart from SQLite table rebuilds. Applied to the E2E database; `makemigrations --check` reports no further drift.
- **Housekeeping**: no migration. Model `Status` choices are unchanged. Existing rows are not rewritten.

---

## 5. Test results

Environment: Python 3.13, Django project tests on SQLite (see limitations), Node 20, Playwright Chromium headless.

### 5.1 Backend (`python manage.py test --parallel 1`)

| Run | Tests | Result |
|-----|-------|--------|
| Baseline (original code) | 2235 | 3 failures, 27 skipped |
| After changes | 2250 (+15 new) | 3 failures, 27 skipped |

- The 3 failures are identical before and after, and are all listed in the repository's `baseline-known-failures.txt`:
  - `test_department_roles … test_security_and_procurement_role_grants_stay_narrow`
  - `test_portal_stay_lifecycle … test_menu_shows_only_active_items_with_availability_labels_and_no_internal_fields`
  - `test_deployment_readiness … test_production_settings_boot_cleanly_with_northflank_mysqls_and_rediss_urls`
- Targeted runs: `apps.gallery` 13 tests OK; housekeeping suites (`test_housekeeping`, `test_batch04_housekeeping_statuses`, `test_stays`, `test_batch08_room_assets`, `test_role_alias_guards`, `test_auto_checkout`) 39 tests OK.

### 5.2 Frontend (`node --test tests-js/`)

| Run | Tests | Pass | Fail |
|-----|-------|------|------|
| Baseline | 725 | 718 | 7 |
| After changes | 743 | 736 | 7 |

- The 7 failures are the same as baseline and unrelated to this batch: a deployed page count (72 vs 73), accounting copy, deploy config, PWA manifest scope, a table-layout audit, and accounting date fields.
- The 18 new batch-04 tests all pass, including the sidebar guard for the removed entry.

---

## 6. Verified functionality (browser, Playwright headless, E2E stack)

E2E stack: backend on `127.0.0.1:8000` with a separate SQLite database, frontend on `:5500` serving the repository. Test users have the seeded roles. The sandbox-only override that lets test users sign in from any device was applied to the E2E database only.

**Task 1 — hero**
- Oversize file (>5 MB) refused client-side with a message; no request sent.
- Each of the four slots uploaded independently. A throttled 1 MB upload showed progress 3% → 7% → 12% → 19%.
- Public homepage: four slides, four dots, in order. On load, only the active slide and the next slide had `src`; slides 3 and 4 kept `data-src` until they became active or next. Desktop and 375 px mobile both render.
- Replacing `hero_2` changed only `hero_2`. Removing `hero_3` left the other three and gave a three-slide hero.
- Broken uploaded media (route aborted) fell back to `assets/images/hero-1.jpg` and `hero-2.jpg`.
- The `welcome`, `experience` and `find_us` slots were not uploaded or changed in this test run.

**Task 2 — housekeeping**
- Admin and housekeeper pages: status filter lists Open, Accepted, In progress, Ready for inspection, Blocked, Completed only. No Claim buttons.
- API: `POST /claim/` returns 410 with `HOUSEKEEPING_CLAIM_RETIRED`; ASSIGNED and CANCELLED statuses return 400; housekeeper cannot act on an unassigned task (403).

**Task 3 — sidebar**
- Desktop and mobile (375 px, menu opened): no `service-qr.html` link. Guest Requests, Housekeeping and Inventory remain.

**Task 4 — reorder**
- Low-stock row (`E2E-STORE-2`, 2 on hand, reorder level 4) → Reorder: link is `procurement.html?reorder=10&location=3`; dialog preselects **E2E-STORE-2**; source note shown; total ₦45,000.00 for 5 crates at ₦9,000.00.
- **Create draft** produced `PO-20261031-9FA837428D4F`: status DRAFT, `delivery_location_code = E2E-STORE-2`, total 45,000.00. No stock changed; the draft is awaiting approval.
- Procurement-capable roles (admin, general manager, procurement officer, inventory clerk): all preselect E2E-STORE-2 for that row.
- Direct link without `location` still preselects the highest-stock store (old behaviour). An invalid `location=abc` is ignored.
- The page's own "Reorder stock item" button still opens its picker.
- Housekeeper and receptionist: `procurement.html?reorder=10` and `inventory.html` both show `/403.html`; no Reorder link is rendered; API returns 403 `FORBIDDEN`. Anonymous returns 401. Missing item returns 404.

**Low-stock notification → reorder path.** The codebase has no push, email or in-app low-stock notification. The "alert" is the inventory stock-state pill ("Low stock", from `needs_reorder`) and the "Needs reorder" view. That is the path tested above. The staff-dashboard KPI `low_stock_items` exists in the backend but no frontend consumes it.

---

## 7. Limitations and notes

1. **Database.** The Django tests and E2E run used SQLite. `mysqlclient` could not be installed in the sandbox, so MySQL was not exercised. The migration is choices-only, so this is low risk, but it should be run once on the MySQL deployment.
2. **Responsive hero images.** Uploaded hero images have no `srcset` variants. Each uploaded slide uses one WebP rendition on all devices. The shipped first slide keeps its `srcset`. Separate mobile and desktop renditions would need a new image-variant pipeline, which the brief excludes.
3. **Caching.** `index.html` loads `js/carousel.js` and `js/site-images.js` with `?v=1.1.24`. The version was not bumped, so browsers may keep the old scripts until the deploy bumps the version or the cache expires.
4. **Carousel rebuild.** The slideshow is rebuilt at most twice per page load (once from the cached hotel payload, once after the gallery fetch). The old carousel's timer continues on the detached node until the page is left. This is the same pattern as before the batch.
5. **Housekeeping legacy rows.** Stored ASSIGNED rows show as "Open" but are only found under "All statuses", because the backend status filter is an exact match and the Open filter sends `OPEN`. Stored CANCELLED rows show as "Closed" and are also only under "All statuses". No data was changed, as agreed.
6. **Housekeeping behaviour change.** Housekeepers can no longer self-assign. This is intended, but staff should be told that dispatchers must assign tasks first.
7. **Pre-existing issue, not changed.** On the housekeeping page, `js/room-assets.js` requests asset configuration for every role. A housekeeper gets 403 for that request. It was already there and is outside batch 04.
8. **Pre-existing, not changed.** After a reorder draft, the URL keeps `?reorder=…&location=…`. Refreshing reopens the dialog. This also happened before the batch.
9. **Roles not browser-tested.** The `MANAGER` role was not in the E2E seed. The reorder gate for it follows the existing capability and role fallback, which are unchanged.
10. **Sandbox-only items (not in the repository).** The E2E database received a second location (`E2E-STORE-2`), a replenish of item 10 there, an admin balance adjustment, a draft purchase order and sign-in grants for test users. These are sandbox data only and are not in the repository or its migrations.
11. **Not pushed.** The changes are committed on the local branch only. No push was made.

---

## 8. Rollback

**Files.** Restore the 15 modified files from `HEAD` of the original repository (or revert the batch commit). Delete the 4 new files.

**Task 1 — migration.** Before reverting code, delete the `hero_2`, `hero_3` and `hero_4` rows (through the admin API or the Django shell), because the old code does not know those sections. Then run `python manage.py migrate gallery 0002`. The `hero` row and its image URL are unaffected.

**Task 2.** Reverting the code restores the Claim route (200) and the Assigned and Cancelled transitions. No data was rewritten, so no data step is needed. Tasks assigned under the new rules remain valid under the old ones.

**Task 3.** Re-add the single `NAV` line for "Service Links" in `js/dashboard.js`.

**Task 4.** Revert `inventory.html` and `procurement.html`. The reorder API is unchanged.
