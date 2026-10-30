# Batch 04 test report

Executed in isolated copy with SQLite, local temporary media and installed project test dependencies (2026-10-30):

- `python manage.py check`: PASS (0 issues).
- `python manage.py makemigrations --check --dry-run gallery rooms`: PASS (no pending model changes).
- `python manage.py test apps.gallery.tests.test_batch04 tests.test_image_upload_paths tests.test_media_storage --verbosity 0`: PASS (38 tests). New Batch 04 tests cover single/repeated room upload, append and storage readback, primary selection, individual deletion, legacy room creation/edit images, invalid uploads, rollback on storage failure, four homepage slots, replacement uniqueness, optimized WebP result, public response, unauthorized role, removal, failed replacement and write failure.
- `python manage.py test tests.test_image_upload_paths tests.test_media_storage tests.test_multi_room_booking --verbosity 1`: PASS (38 tests; booking regression). Some expected HTTP error/storage-failure log lines and SQLite lock messages appear in tests exercising error/concurrency paths; test runner concluded OK.
- `python frontend/validate.py`: PASS (73 HTML files and 32 standalone JS files checked).
- `node frontend/tests-js/test-portal-room-order.js`: PASS (10 tests).
- `node --check frontend/js/api.js` and `node --check frontend/js/site-images.js`: PASS.
- `python -m compileall -q backend/apps/gallery backend/apps/rooms`: PASS.
- `git diff --check`: PASS.

Initial local test run was blocked by missing `whitenoise`; installed it from the repository's existing requirements, then tests ran. An intermediate assertion expected a wrapped viewset response and was corrected in the test. Final runs above passed.

**Not executed:** live production bucket permissions/anonymous CDN retrieval, browser-based mobile/desktop visual checks, large-image browser bandwidth profiling, full project test suite. Django test runner applied migrations to a disposable SQLite database; no production migration or real storage test was performed. Verify these in staging before release. Server logs for intentionally failed upload/permission tests include expected 400/403/502 responses.
