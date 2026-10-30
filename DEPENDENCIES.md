# Dependencies

No new dependencies. Uses existing Django, DRF and Pillow; `django-storages`/Boto3 are already listed in `backend/requirements.txt` for configured B2 storage. Existing frontend API, modal, icons, and browser FormData/Object URL APIs are reused.

Runtime prerequisites: apply gallery migration; configure durable media storage and `/api/media/` (or the configured media/CDN URL); ensure storage is writable/readable from the backend and images are publicly retrievable on the frontend. Existing `MAX_UPLOAD_MB` default is 5 MB; dashboard advice assumes this default; the server remains authoritative when customized. Existing role permission classes are reused without modifying staff permissions.

Cross-module integration: room image display still uses existing `RoomSerializer`/room-type fallback; booking module is not modified. Public image swapping integrates with existing hotel settings through `site-images.js`; the gallery slot wins if both are set. No changes to inventory/accounting/staff modules.
