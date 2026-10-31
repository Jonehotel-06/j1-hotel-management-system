# Updated files manifest — Batch 04

Every file below is copied to this folder at its original repository-relative path. No repository-wide copy is included. Hashes are the first 16 hex characters of SHA-256 of each file's contents.

Total: 19 files (4 new, 15 modified).

| # | Path (repository-relative) | Status | Lines | SHA-256 (16) | Purpose |
|---|---|---|---|---|---|
| 1 | `backend/apps/gallery/migrations/0003_homepage_hero_slots.py` | new | 18 | `2892354e68ab1f68` | Task 1 — choices-only migration |
| 2 | `backend/apps/gallery/models.py` | modified | 48 | `b50e3034595439e0` | Task 1 — add hero_2, hero_3, hero_4 homepage sections |
| 3 | `backend/apps/gallery/tests/test_homepage_hero_slots.py` | new | 116 | `1d1d6047516308fe` | Task 1 — backend tests for four hero slots |
| 4 | `backend/apps/housekeeping/serializers.py` | modified | 97 | `0f179834dada92ae` | Task 2 — requestable status choices |
| 5 | `backend/apps/housekeeping/services/task_service.py` | modified | 249 | `f16875b5d7037dd7` | Task 2 — transitions, OPEN creation, dispatch, claim removed |
| 6 | `backend/apps/housekeeping/views.py` | modified | 147 | `c042e1e23629f7fb` | Task 2 — claim route returns 410 |
| 7 | `backend/tests/test_batch04_housekeeping_statuses.py` | new | 122 | `09fc25e78fb4dbb5` | Task 2 — backend tests for retired statuses and history |
| 8 | `backend/tests/test_housekeeping.py` | modified | 76 | `cb4ad46283842f82` | Task 2 — claim step replaced by dispatch |
| 9 | `frontend/dashboard/gallery.html` | modified | 266 | `4ca12c6746accb15` | Task 1 — hero 1–4 cards, progress bar |
| 10 | `frontend/dashboard/housekeeping.html` | modified | 83 | `d48687120c79848d` | Task 2 — Claim, Assigned, Cancelled removed from UI |
| 11 | `frontend/dashboard/inventory.html` | modified | 282 | `a64788c11f3b1082` | Task 4 — Reorder link carries the balance's store |
| 12 | `frontend/dashboard/procurement.html` | modified | 1102 | `a09edd7d039bac3e` | Task 4 — reorder dialog preselects the linked store |
| 13 | `frontend/js/api.js` | modified | 1253 | `f40b034af28b6ea4` | Task 1 — uploadWithProgress helper |
| 14 | `frontend/js/carousel.js` | modified | 130 | `15c44c6a64472a01` | Task 1 — defer and promote slide images (data-src) |
| 15 | `frontend/js/dashboard.js` | modified | 2298 | `2da6867d442e88fe` | Task 3 — Service Links nav entry removed |
| 16 | `frontend/js/site-images.js` | modified | 224 | `730a09b93a46b741` | Task 1 — hero slideshow from hero slots, fallbacks |
| 17 | `frontend/tests-js/test-batch04-gallery-housekeeping-navigation.js` | new | 147 | `bc8319640b235cf1` | Batch 04 static contract tests |
| 18 | `frontend/tests-js/test-inventory-batch01-management.js` | modified | 193 | `a4e23922f3c57771` | Assertions updated to new reorder signatures |
| 19 | `frontend/tests-js/test-service-qr-ui.js` | modified | 72 | `2904d2bb867a8743` | Assertion moved from removed nav entry to page access rule |

## Not part of the change

- `ai_updates/batch_04_gallery_housekeeping_navigation/IMPLEMENTATION_REPORT.md` — implementation report (this folder).
- `ai_updates/batch_04_gallery_housekeeping_navigation/UPDATED_FILES_MANIFEST.md` — this manifest.
- No migrations other than `gallery/0003_homepage_hero_slots.py`. No housekeeping data migration.
- No files outside `backend/` and `frontend/` were changed.
