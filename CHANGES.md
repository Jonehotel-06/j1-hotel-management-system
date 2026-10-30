# Batch 04 changes / integration

## Room uploads
- Physical-room image endpoint `POST /api/admin/rooms/{room_id}/images/` now accepts repeated multipart `images` fields (up to 20) as well as legacy `image` (one or repeated). Checks every file before any write, verifies each stored object, rolls back new database rows and removes newly written files when an upload fails. Existing room images are not cleared; the first image becomes primary only when the room lacks an active primary. Legacy single-upload response remains an object; multiple uploads return an array in the existing success envelope.
- Both the dedicated upload and room create/edit image path give each new file a unique storage key to prevent accidental overwrite on B2. Existing per-image PATCH (primary) and DELETE routes are retained.
- Dashboard physical-room image manager accepts multiple files, shows local previews, an upload loading state, validation/errors, and keeps individual star/delete actions. Creation/edit form's optional single-photo behavior remains unchanged; use the image manager to append multiple images after room creation.

## Homepage gallery
- Adds a nullable, unique `homepage_section` field to the existing `GalleryItem`: `hero`, `welcome`, `experience`, `find_us`. Existing ordinary gallery rows are unchanged. Homepage records are not mixed into the existing gallery listing/management or public gallery carousel.
- Public `GET /api/gallery/homepage/` returns a keyed map of published section records with image URLs, alt text and status. Cache disabled to show changes on the next website load.
- Management `GET`, `PUT`/`PATCH` (multipart `image` and/or `alt_text`) and `DELETE /api/admin/gallery/homepage/{section}/`. Role gate uses existing manager/admin role policy. PUT creates or replaces a singleton; DELETE returns to built-in image. Invalid section is 404, missing initial image is 400. Replacements create a distinct key, verify media storage, retain the old image if a write fails, and clean up the old object after a successful commit. Homepage images are auto-oriented and resized (at most 1600×1000) into quality-82 WebP using existing Pillow.
- Gallery dashboard has four responsive cards with section name, current image, status, recommended dimensions, alt text, local preview, replace/save, remove and loading/error states. Public homepage uses the existing `site-images.js` image swap architecture to apply the four gallery slots; when unset it retains shipped assets and layout. Below-the-fold images retain lazy loading and fixed dimensions. No new frontend framework or hardcoded API host.

## Apply
1. Copy repository-relative paths from this package over an isolated deployment branch, review conflicts, then merge.
2. Run `cd backend && python manage.py migrate gallery && python manage.py check` before releasing backend. Deploy backend and frontend together once. Subsequent image changes need no source edits or redeployment.
3. Confirm existing production media backend configuration (Backblaze B2 or durable, shared writable local media), read/write permissions, public/media serving URLs, and allowed frontend CORS origin. A read-back check cannot prove remote CDN/public ACL accessibility: test from an unauthenticated browser on production.
4. Staff manager/admin: open Dashboard → Gallery → Homepage images. Upload images to each slot; select a physical room → image manager to append multiple photos. No changes to booking, inventory, accounting, staff permission definitions or unrelated website sections.

## Compatibility / limitations
- Existing hotel-settings image URLs are preserved; the new gallery slot takes precedence when present. Deleting a slot returns to the pre-existing hotel-settings image if configured, otherwise the shipped static image.
- Existing static hero preload may still fetch the fallback asset before an admin hero is fetched; the dynamic hero is resized and is not available to static HTML preload. Other legacy gallery and direct image PATCH flows retain their existing behavior.
- No physical production B2 credentials or live deployment were available; verify actual production ACL/CDN and mobile/desktop visual appearance after integration. This package intentionally does not alter the existing storage configuration or booking images.
