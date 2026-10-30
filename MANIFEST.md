# Batch 05 manifest

Output: `ai_updates/batch_05_kitchen_food_availability/`
Source: https://github.com/Jonehotel-06/j1-hotel-management-system
Baseline commit: `0d76219b72b65018ae160b8e20ac586683e0b49c`

14 modified files, 7 new repository files, 5 package documents.
No removed files. No original repository edits or pushes. No inventory-owned files changed.

Every code/test/tool path below is its original repository-relative destination and its package-relative path.
Apply backend/ and frontend/ as an overlay at the repository root. This is not a full clone.

| Status | Original location / package path | Packaged SHA-256 |
|---|---|---|
| Modified | `backend/apps/portal/views_catalog.py` | `e4843984d7c8d6af5121c5e3661db393bf5a0fe460fd22209d2a14fdb3806f66` |
| New | `backend/apps/pos/food_views.py` | `4e095fe23136cf879a48bd467ef4fd2ba43016bab157e9bebb83280030e29008` |
| New | `backend/apps/pos/migrations/0009_prepared_food_availability.py` | `3cd94a9f4c4a2d23d64fbdb90424cf9efd7922df307f30f7e79a7040ddcb19f3` |
| Modified | `backend/apps/pos/models.py` | `3bfdfe1e1ff39a9b0a562acb50acc1864663468051ec16c5e388b0daac548877` |
| Modified | `backend/apps/pos/serializers.py` | `eb7116688bb57c1cd9e84f0c9a55a4a924ecb4be9041402051cec5938f498184` |
| Modified | `backend/apps/pos/services/barcode_service.py` | `7df0eeacfffdec83c38ceac5986cf11bb14830924ccdabcc2745eba2bab22b4b` |
| New | `backend/apps/pos/services/food_service.py` | `a6db968303765070110ff29442b9140c5e51ebe4c4f6356289cd102079a7fdf1` |
| Modified | `backend/apps/pos/services/order_service.py` | `f366820afe789beabc40ee8bef9909b8d4853593d9a7729d7f927b50de6dd1e3` |
| Modified | `backend/apps/pos/services/return_service.py` | `2045629c32fa0bb8f7f59ac6407a2ff6209087740bae15fdb3e1e4bef18e1ee5` |
| Modified | `backend/apps/pos/urls.py` | `23112e1bee771b73edb971e918af5fddfa7a396853c02c9d0ad53971f5d4c448` |
| Modified | `backend/apps/pos/views.py` | `c510861e1eed5daf37c076c236ac307516f911135a0f9b3dfc573f53e301e9c5` |
| New | `backend/tests/test_batch05_food.py` | `e51109181afb4b74ed551b89d737f6badc188d22cc7f47944de85311583059ac` |
| Modified | `backend/tests/test_guest_portal_room_orders.py` | `817302aebfec00efbfc93f6f1e2de9f9e803530416184f1e8d9a5382ad0be17f` |
| Modified | `backend/tests/test_pos.py` | `e1edb7d81d109dd3fc56961c6cf58e4ffbf9badff36352ff50bd6f17e2575529` |
| New | `backend/tools/test_batch05_concurrency.py` | `fbcc435e54b5dcea763868f8aece0bd91b2deac5a6cd1d1af8b1441a15f2c2f5` |
| Modified | `frontend/dashboard/pos.html` | `681dce6b9a70323597722dd130692867205dea41255a783505d8aa2d90da0961` |
| Modified | `frontend/js/config.js` | `e4d15dfe5c18abc0a24babdafca222878ff37bbc17a601ecdc5e74748455e4e3` |
| New | `frontend/js/kitchen-food.js` | `3367262052c9b2019f0c809fb9ab95858a2377cc383c1aafdf34052ae6056649` |
| Modified | `frontend/js/portal-catalog.js` | `9b4872730c0de72e64a34c8dc61ddd1a54e10d59fd6e368a7aadc134bcb4d5f1` |
| Modified | `frontend/portal/index.html` | `9a6e7241b94350e062f2c4cc639b61ae42d51dc662be829d43de9e05d7263584` |
| New | `frontend/tests-js/test-batch05-food.js` | `9bd64aa0ea7c47c45d6bed2afde2619b4a990680a5a73a1d9b0f9f8fa4b48f75` |

## Package metadata

- MANIFEST.md — complete file locations and integrity inventory (this file).
- CHANGES.md — completion report, API, migration, integration, rollback and known limitations.
- TEST_REPORT.md — executed commands, counts, baseline failures and unexecuted validation.
- KITCHEN_WORKFLOW.md — simple/portion/advanced operating instructions and safe returns.
- DEPENDENCIES.md — exact reused contracts, module boundaries and release prerequisites.

No unchanged source files, .git metadata, installed packages, databases, generated caches,
credentials, logs or fake production records are included. Test fixtures exist only in automated tests.
ZIP root is ai_updates/batch_05_kitchen_food_availability/.
