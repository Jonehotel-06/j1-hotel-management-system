# Batch 08 package manifest

Source: https://github.com/Jonehotel-06/j1-hotel-management-system, commit `c17a84f`. Package contains only the following changed/new repository-relative files and the five Batch 08 documents at package root. Paths below are also the original integration locations; new files did not previously exist.

| Path | Change |
|---|---|
| `backend/apps/housekeeping/models.py` | Modified: asset catalog, configurations, inspection evidence and reviews |
| `backend/apps/housekeeping/urls.py` | Modified: asset route registration before task reference routes |
| `backend/apps/housekeeping/asset_views.py` | New: role-gated API and workflow |
| `backend/apps/housekeeping/migrations/0002_reusableitem_assetinspection_assetinspectionline_and_more.py` | New: schema migration |
| `backend/tests/test_batch08_room_assets.py` | New: backend/API tests |
| `frontend/dashboard/housekeeping.html` | Modified: responsive asset panel, original task queue retained |
| `frontend/js/room-assets.js` | New: asynchronous panel logic |
| `frontend/tests/batch08_mobile.py` | New: Playwright component smoke test |
| `MANIFEST.md` | New: this manifest |
| `CHANGES.md` | New: changes and known limitations |
| `TEST_REPORT.md` | New: executed checks and results |
| `HOUSEKEEPING_WORKFLOW.md` | New: process and API contract |
| `DEPENDENCIES.md` | New: deployment and test dependencies |
