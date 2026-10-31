# Batch 03 implementation report — partial, not release-ready

## Status and source

Source: `Jonehotel-06/j1-hotel-management-system`, commit `391b8becdd9b25afd163ef70bce2421d31ebcdaf`. Changes were developed in a separate clone and branch `ai/batch-03`; the original repository was not modified. The package contains only changed/new repository files and these reports. **The requested stay-specific direct-link lifecycle is not implemented. Do not deploy this package as a completed Batch 03.** No migrations were added.

## 1. Existing implementation discovered

- Check-in calls `ensure_stay_for_check_in`, which schedules the booking contact's `GuestPortalInvitation` on commit. A one-to-one stay row provides automatic-send idempotency; an `EmailLog` records provider outcome and an authorized API allows status and resend. The invitation currently sends a branded link to the login page, not a direct sign-in credential. Separately, newly checked-in room occupants receive login-page emails using `RoomOccupant.portal_invited_at`.
- Portal manual sign-in issues an email-scoped `PortalAccessChallenge` with a stored SHA-256 token hash. The link is one-use. Portal sessions use hashed tokens, HttpOnly cookies and active-stay eligibility checks; defaults include a seven-day sliding idle timeout and a 30-day absolute cap. Checkout checks eligibility immediately and attempts to revoke sessions. The principal is separate from staff authentication. Access is scoped by eligible **email**, not by the specific stay that produced a link.
- `Offer` already models public offers; `GuestDiscount` models a private rule, and `GuestDiscountApplication` snapshots values and amount. The staff offers form supports general offers; guest search and individual discount creation already exist in the staff Guests page. The quote service chooses between a public offer and a guest discount. It does not stack them.
- The central `frontend/seo_build.py` owns metadata, JSON-LD, canonicals, sitemap and robots. Public HTML already identifies the Enugu hotel and contains crawlable room and facility summaries. The central registry uses the canonical `https://jonehotels.com` origin and excludes private paths from the sitemap. `robots.txt` is not an authorization mechanism.

## 2. Problems, root causes and outstanding work

### Critical: direct stay-specific portal invitation and lifecycle — NOT implemented

The current invitation is not a direct access link; each guest must request a separate one-use link. Reopening that link on another device after it is consumed cannot work. The session's idle/absolute limits can expire during a long stay. Since challenge and session records are email-scoped, rather than stay-specific, an old credential can potentially span independent stays for the same address while another stay remains eligible. Occupant invitations lack the same reliable per-recipient delivery status/resend path, and `portal_invited_at` reflects queuing, not necessarily provider delivery. Secure stay-specific rotation, per-recipient status, and automatic direct delivery are not implemented here. Early checkout, extensions, cancellation, later room check-in, concurrent check-in and multiple-device scenarios still require a coordinated end-to-end design and tests.

Additionally, manual magic links currently put the token in the URL query and the central email pipeline stores message bodies in `EmailLog`. Simply putting a reusable token into those emails would leave bearer credentials in persisted email bodies and potentially access logs. A safe implementation must avoid persistent raw secrets, use fragment-based transport or equivalent, prevent referrer/analytics and logs exposure, bind every session and downstream portal/guest-service/finance query to the exact authorized stay and rooms, and revoke both links and sessions on checkout or rotation. It must retain delivery tracking without persisting raw message content and handle delivery exceptions outside the committed check-in transaction. No such workaround is claimed in this package.

The status API changes below are a partial improvement only. `portal_eligible` means the contact address currently has an eligible active room in that stay; it is not evidence that a stay-bound credential was issued. There is no new staff dashboard interface for these fields, and occupant-specific status/resend remains outstanding.

### Private offer eligibility

Previously public booking creation resolved a standalone guest by submitted email and then applied that guest's discount. An unverified person could submit the same email to receive a private rate. The pricing change requires a linked authenticated guest for public booking creation, or an authorized staff booking context. This intentionally means anonymous checkout no longer receives a personal rate merely from a matching email. Standalone guests need a securely linked account or staff assistance; account-linking is not implemented. Existing guest upsert behavior itself was not rewritten and needs a separate security review.

Comparing numeric fixed and percentage discount values directly also chose the wrong personal discount for some subtotals. Personal candidates are now compared by their actual capped monetary amount. Private discount reason is no longer exposed in the public quote. Existing applications and historical snapshots are not rewritten; rescheduling/repricing of existing bookings uses existing project behavior and is not fully redesigned here.

### Other remaining gaps

- Public listing intentionally retains the existing behavior of showing active upcoming offers, not only offers already in effect; pricing still validates applicable dates. If “active and eligible” is intended to exclude upcoming offers from the public page, this needs a product decision and a separate change.
- Existing booking overview exposed all room numbers and booking-contact totals to a room occupant. The narrower overview now masks these, but this does not by itself establish end-to-end stay-specific authorization for every portal API.
- No live production deployment, email-provider acceptance, Google/Bing indexing, AI-search recommendation, external structured-data validator, or real-browser/device verification was performed.

## 3. Files and original locations

See `UPDATED_FILES_MANIFEST.md` for every repository-relative path. In brief: booking pricing/creation and tests; offer validation/selection and tests; portal overview/status and tests; the existing SEO generator, its two generated public HTML pages and tests; one portal HTML rendering adjustment; the existing offers dashboard; SEO documentation; and a new `frontend/llms.txt`.

## 4. Portal lifecycle and status changes actually made

Existing lifecycle remains: check-in schedules an email pointing at sign-in; the guest requests and consumes a one-time manual link; a session is subject to idle and absolute timeout; live room/stay eligibility and checkout control requests. **This is not the requested reusable direct-link lifecycle.**

The authorized invitation status API now reports whether a record exists, contact-address `portal_eligible`, provider `sent_at` if available, and whether the stay ended. Authorized staff can inspect a checked-out stay's record but cannot resend from it. Status remains backed by the existing `EmailLog` and is provider acceptance, not proof of mailbox delivery. It returns no raw token. The new fields are API-only. Room-occupant overview responses show only rooms permitted to that identity and omit the booking contact's totals; the portal renders a dash instead of a false zero for omitted totals.

## 5. Security improvements and limitations

The personal rate is not awarded at public booking creation based solely on an unverified email. Public offers remain independent; staff permissions remain those of the existing endpoints. Personal rate amounts are calculated server-side, capped at the subtotal and compared with public offers. The individual guest's private reason is no longer returned in quote responses. Room numbers and booking totals are restricted in the occupant overview. No claim is made that these fixes provide complete guest isolation across all portal endpoints or that they implement token rotation or stay-specific credentials.

## 6. Offer changes

The offers page now provides distinct entry points: create a general offer in place, or navigate to Guests to search and create a personal discount. Existing models, image handling, services and snapshot records are reused. General-offer API validation now rejects zero discounts and zero-night limits, in addition to existing date/order and percentage limits. The best personal discount is chosen by actual discount amount for the stay. No offer model or payment code was duplicated or migrated.

## 7. SEO and AI discovery

The existing Hotel JSON-LD `alternateName` now contains all requested variations, on the generated homepage and contact page; the Hotel URL, Website publisher entity, canonicals and existing sitemap continue to use the official origin. The new `llms.txt` provides a concise public description and links to canonical public pages; it does not expose private URLs or invent reviews, ratings, profiles or facts. Existing SEO architecture and robots rules remain authoritative. Search engine and AI indexing, interpretation, association and ranking depend on external services and are not guaranteed. `llms.txt` is an optional convention, not a promise that a crawler reads it. Maintain business facts consistently and submit/monitor the sitemap via the relevant search platforms.

## 8. Tests executed

All tests used the isolated source copy and local SQLite test database. The frontend build was run in a further disposable copy because `build.py` rewrites generated files. Results:

- `python manage.py test tests.test_portal_invitations tests.test_portal_access tests.test_guest_discounts --noinput`: **49 passed** after the portal/status and offer changes.
- `python manage.py test tests.test_batch03_offer_admin tests.test_portal_invitations.PortalInvitationTests --noinput`: **16 passed**; includes general creation/validation, private creation/public-list isolation and current invitation status. After adding the cancelled-booking resend guard, `tests.test_portal_invitations.PortalInvitationTests` was rerun: **13 passed**.
- `python manage.py test tests.test_guest_discounts tests.test_offer_eligibility tests.test_portal_access tests.test_portal_invitations tests.test_portal_stay_lifecycle tests.test_guest_portal_room_orders --noinput`: **131 run, 1 failure, 5 skipped**. The failure is `PortalCatalogueTests.test_menu_shows_only_active_items_with_availability_labels_and_no_internal_fields` (actual `Not Available`, expected `Currently unavailable`). The same single test was separately run against the untouched source commit and failed identically. The combined run preceded the final status API additions; the targeted invitation tests were rerun afterward.
- `node --test tests-js/test-seo.js tests-js/test-offers-and-payments-ui.js tests-js/test-portal-ui.js tests-js/test-portal-catalog.js tests-js/test-portal-room-order.js`: **57 passed**.
- `python build.py`, `python seo_build.py --check`, `python validate.py`: **passed** in the disposable frontend copy; validator checked 73 HTML files and 34 standalone JS files. Existing canonical/sitemap and JSON parsing assertions passed; no external schema validator was run.
- `git diff --check` and Python compileall passed.

The existing portal tests cover current check-in, idempotency, delivery failure, an unused manual link days later, cookie reuse, checkout and room isolation. They do **not** establish success for the requested reusable automatic link, device/browser lifecycle, stay-bound rotation, per-occupant delivery status, or all early checkout/extension/cancellation cases; those features remain incomplete. No test result should be interpreted as proving them.

## 9. External dependencies and rollout

Delivery depends on configured email provider and frontend/API origins; actual acceptance is not mailbox delivery. Production must serve `llms.txt`, generated HTML and the canonical domain consistently. Search crawling and indexing are external. Before release, implement and independently security-review the missing portal lifecycle, design a private non-persistent email delivery path, address per-recipient invitation state and secure rotation, exercise real browsers and devices, and rerun the full booking/portal/offer suites. Review the intentional change to anonymous personal-rate eligibility with hotel staff.

## 10. Rollback instructions

For a deployment based exactly on the source commit, revert the modified paths in `UPDATED_FILES_MANIFEST.md` from commit `391b8becdd9b25afd163ef70bce2421d31ebcdaf`, remove the two new files `backend/tests/test_batch03_offer_admin.py` and `frontend/llms.txt`, and rebuild/redeploy the frontend. Preserve unrelated later changes: on a newer branch use a reviewed inverse patch or targeted restore rather than a blanket reset. No database migration or data rollback is required by this package. Rolling back restores the previously vulnerable anonymous personal-discount behavior and occupant overview exposure; evaluate that security consequence first. Keep existing booking, guest and discount-application records intact.
