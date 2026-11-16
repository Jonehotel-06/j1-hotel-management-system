# J-ONE complete source deployment — 1.1.29

This package contains the complete tracked application source (including unchanged files) plus the current repairs, not just a patch. It is intended to be deployed by the person who manages the hotel's frontend and API. The previous 1.1.28 changed-files package did not include unchanged backend routes and therefore cannot by itself bring an older backend up to date. Apologies for that omission.

## Findings

On 15 November 2026, unauthenticated checks against the configured production API, https://api.jonehotels.com, returned JSON 401 for the admin bookings and offers collection routes, but HTML 404 for booking extend-quote, offer deactivate, and reception amenities stock routes. These checks show that those paths were not served at that host at the time of the checks; they do not identify the server-side cause. The public check-in page at https://jonehotels.com/dashboard/check-in.html referenced version 1.1.23 even though the unversioned shared dashboard script downloaded during the same investigation contained the amenities function. A stale asset or mixed release can cause the reported missing function; the exact browser state was not inspected.

## Deploy safely

1. Back up the production database and confirm that this source is the intended application revision and will not overwrite newer production changes. The archive is based on the supplied repository, not a snapshot of the current live server.
2. Deploy the **complete `backend/`** through the existing API deployment pipeline to the service behind `api.jonehotels.com`, using its existing production environment, secrets, database, media storage and configuration. Do not copy a local SQLite database or replace production media. Apply Django migrations with the production deployment procedure and restart every web worker. In particular this release contains `apps/accounts/migrations/0038_management_tier_guest_amenity_breakfast.py`. Check migration compatibility and back up first.
3. Deploy the **complete `frontend/`** as a single release through the existing frontend pipeline. Keep the included version 1.1.29 page/script stamps and service worker together. Clear any CDN cache for HTML and JS if necessary. The archive deliberately omits the obsolete `frontend/worker/index.js`; deploy according to the included `wrangler.jsonc` and existing infrastructure, rather than restoring an old proxy.
4. Verify the live `/version.json` reports 1.1.29 and the check-in page loads `js/dashboard.js?v=1.1.29`. Close all dashboard tabs, accept the app update if prompted, and reopen/hard reload after any in-progress payment is complete. Verify that the loaded dashboard script exports `JONE.dashboard.amenityIssueModal`. Do not clear site data blindly during active work.
5. As an unauthenticated routing check, requests to `/api/admin/bookings/TEST-INVALID/extend-quote/`, `/api/admin/offers/99999999/deactivate/`, and `/api/admin/service-requests/reception/amenities/stock/` should reach the Django REST API and normally return JSON 401 rather than an HTML 404. A different reverse-proxy policy may alter this; investigate any HTML 404 and confirm host/path routing. Never use these invalid IDs for a real transaction.
6. With an authorised test account and test data, record toiletries for an in-house stay with Reception stock available; quote and confirm one stay extension while checking the amount and payment; deactivate a test offer and confirm its state, then reactivate if desired. Avoid charging or modifying a real guest merely to test.

The 404 guidance now says deployment **or routing may** be outdated; an HTML 404 cannot prove the cause. If an old shared script still loads, the Amenities button now presents an actionable reload message instead of a raw TypeError. Neither change can update a server or purge a browser automatically.

## Verification

* `python3 build.py --bump patch` released 1.1.29; `python3 validate.py` checked 77 HTML files and 41 standalone JS files: ALL OK.
* Targeted Node tests for check-in amenities, extension rules, API diagnostics and offers: 54 passed. Four extension dialog tests in jsdom: 4 passed.
* Django: `manage.py test tests.test_stay_extension tests.test_offer_admin_control_regressions tests.test_guest_services_amenities_breakfast --noinput`: 62 passed on SQLite.
* These are local automated tests, not a real-browser production test. The live deployment was not changed. Other unrelated failures in the broader suites remain documented in the previous test evidence and should not be presented as passing.

The archive excludes environment secrets, the local SQLite database, generated dependencies, caches, and local backend media. Retain the production database, environment and uploaded files.
