# TV Rooms Gallery — storage, limits and operations

**Written:** 2026-11-19, after the upload-400 repair.
**Scope:** `backend/apps/tv_rooms_gallery/`, `frontend/rooms-gallery/`, `backend/Dockerfile`, `compose.yaml`.

---

## 1. Why uploads returned HTTP 400

`POST /api/rooms-gallery/manage/media/` answered — captured verbatim from the
pre-repair code over HTTP (`J1_EVIDENCE/evidence-gallery-http.log`, stage
"BEFORE"):

```json
{
  "success": false,
  "code": "VALIDATION_ERROR",
  "message": "Validation failed.",
  "category": "VALIDATION",
  "request_id": "6e76079370e7fca295b516afdff49ddb",
  "errors": { "file": "Storage service unavailable. Please try again later." }
}
```

That body was the whole story: **the private gallery directory was never
configured**, so `storage.root()` raised `ImproperlyConfigured`; the view reported
it through the serializer's validation channel, so the UI showed a field error
against the file and the client (which read `data.file`) showed nothing useful. Nothing was
wrong with the 68 KB JPEG, the room id or the multipart request. Five further
defects sat behind it and would have broken the next uploads in turn:

| # | Defect | Effect |
|---|--------|--------|
| 1 | `TV_GALLERY_STORAGE_ROOT` unset (no default, no volume in the image) | **every** upload 400s |
| 2 | 16 MB / 32 MP image ceiling | modern phone photos rejected |
| 3 | image format allowlist `{JPEG, PNG, WEBP, GIF}` refused Pillow's `MPO` (multi-picture JPEG from phones) | valid photos rejected |
| 4 | video detection only tested bytes 4–8 for `ftyp` | MP4s whose first box is `free`/`wide`/`skip` were "not a video" |
| 5 | MP4 codec allowlist was `{h264, hevc, mpeg4}` (WebM `{vp8, vp9}`) | AV1/VP9/ProRes/DNxHD exports rejected; no `ffmpeg` in the image to transcode |

---

## 2. Where the files live

The gallery is **private**: originals, thumbnails and renditions live under
`TV_GALLERY_STORAGE_ROOT`, never under the public `MEDIA_ROOT`. Public access is
only through the API's authenticated media routes.

```
TV_GALLERY_STORAGE_ROOT  (env)  →  Django setting  →  default <MEDIA_ROOT>/../tv_gallery
```

Resolution order in `apps/tv_rooms_gallery/storage.py`:

1. `TV_GALLERY_STORAGE_ROOT` environment variable (operational override);
2. the `TV_GALLERY_STORAGE_ROOT` setting (`config/settings/base.py`);
3. a directory next to `MEDIA_ROOT`.

Then, in order, the store is rejected when it is not absolute, when it lies
inside the public `MEDIA_ROOT` (path overlap in either direction), or when a write
probe fails. Rejection raises `GalleryStorageUnavailable`, which the view
translates to **HTTP 503 `GALLERY_STORAGE_UNAVAILABLE`** with an operator-facing
`detail` and `data.storage_detail` — never to a fake "Validation failed.".

Layout: `aa/bb/<sha256-prefix>/…` — sharded so a room with thousands of photos
does not create one enormous directory.

Docker: the image creates `/var/lib/jone-gallery` (mode `0700`, owned by the app
user) and `compose.yaml` mounts the named volume `gallery_data` there on **both**
`api` and `worker`, so a container rebuild or a second API replica keeps the
photos. The volume is private to the containers; no host path is published.

---

## 3. Limits (all settings, all reported to the UI)

| Setting | Default | Meaning |
|---------|---------|---------|
| `TV_GALLERY_MAX_UPLOAD_MB` | `128` | per-file ceiling; the effective value is `min(this, setting, 512)` |
| `TV_GALLERY_MAX_IMAGE_PIXELS` | `200_000_000` | decoded-pixel guard (decompression bombs) |
| `TV_GALLERY_MAX_IMAGE_BYTES` | derived | raw image ceiling |

`GET /api/rooms-gallery/manage/settings/` returns `effective_max_upload_mb`,
`storage_ready` and `storage_detail`, and the admin screen prints them
("Up to 128 MB per file · JPG, PNG, WEBP, MP4, MOV, WEBM, 3GP") plus a warning
banner when `storage_ready` is false. `frontend/rooms-gallery/admin.js` also
pre-flights the file in the browser (extension, empty file, size vs.
`max_upload_mb`) so an obvious mistake never leaves the PC.

> **Cloudflare note:** a Cloudflare-proxied host rejects request bodies over
> 100 MB with its own 413 before the request reaches Django. Keep uploads at or
> below 100 MB on the public host, or upload from a network that does not traverse
> that limit and raise `TV_GALLERY_MAX_UPLOAD_MB` explicitly.

Supported today:

* **Images** — JPEG (baseline, progressive, **MPO** — Pillow reports MPO, the old allowlist refused it), PNG, WEBP; EXIF orientation
  honoured; `Image.draft()` used for the bounded first decode.
* **Video** — MP4/MOV/3GP (H.264, HEVC, MPEG-4, AV1, VP9, VP8, MPEG-2, ProRes,
  DNxHD), WebM (VP8, VP9, AV1). Container detection walks the top-level boxes, so
  a leading `free`/`wide`/`skip` box no longer disguises a valid file.
* **Transcoding** — `ffmpeg`/`ffprobe` are installed in the image; when they are
  missing, uploads are still stored (never lost) but cannot be transcoded for TV
  playback and the settings payload says so.

---

## 4. Retry semantics (why a retry no longer makes duplicates)

Every stored upload carries `upload_fingerprint` = SHA-256 of the received bytes
(migration `0003_gallerymedia_upload_fingerprint`, indexed).

Resending the **same bytes to the same room** returns the existing record:

```http
HTTP/1.1 200 OK
{"duplicate": true, "notice": "This file was already uploaded to this room.", "item": {...}}
```

* different bytes → new item;
* same bytes, **different room** → new item (a photo may legitimately belong to two rooms);
* the duplicate copy written by the second request is deleted, so no orphan files.

This is what makes "press upload again after a timeout" safe: the request either
creates exactly one item or hands back the one that already exists.

---

## 5. Proof it works

```bash
# 1. Is the store usable? (real write → read → delete)
docker compose exec api python manage.py check_gallery_storage
docker compose exec api python manage.py check_gallery_storage --json

# 2. Does the API agree?
curl -s -H "Authorization: Bearer $TOKEN" \
  https://api.jonehotels.com/api/rooms-gallery/manage/settings/
# → {"max_upload_mb":128,"effective_max_upload_mb":128,"storage_ready":true,...}

# 3. Upload — the multipart fields are exactly `file` and `room_id`
#    (the file type is detected from the bytes, never trusted from the client)
curl -s -w '\nHTTP %{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  -F "room_id=12" -F "file=@IMG_20261119_101500.jpg" \
  https://api.jonehotels.com/api/rooms-gallery/manage/media/
```

Expected: `201` with the media object; the same command repeated returns `200`
with `"duplicate": true`. Then confirm the photo renders at
`/api/rooms-gallery/media/<id>/display/`, appears in that room's gallery, and
plays on the TV presentation. `GET …/media/?room=12` is the list the TV page reads.

Accepted formats and the exact envelopes are asserted in
`backend/tests/test_tv_gallery_upload_repair_2026_11_19.py` and
`backend/tests/test_tv_gallery_repair_upload.py`.

---

## 6. Failure playbook

| Symptom | Status | Cause | Action |
|---------|--------|-------|--------|
| `GALLERY_STORAGE_UNAVAILABLE` | 503 | root missing/unwritable/overlapping `MEDIA_ROOT` | `check_gallery_storage`, fix env/volume, restart `api` |
| body too large / Cloudflare page | 413 | above the proxy or Django body cap | shrink the file, or raise `TV_GALLERY_MAX_UPLOAD_MB` (≤100 MB behind Cloudflare) |
| `file: ["Select a file."]` | 400 | no file part, or the field is not named `file` | send the field **`file`** |
| `file: ["The selected file is empty (0 bytes)."]` | 400 | empty part | choose a real file |
| `file: ["File exceeds the maximum allowed size of 128 MB."]` | 400 | over the effective limit | shrink the file or raise the limit |
| `room_id: ["Provide a valid room ID."]` | 400 | non-numeric room id | send the numeric id of an existing room |
| `404` on upload | 404 | `room_id` does not exist | pick an existing room |
| `This video could not be prepared for TV playback` | 201 + warning | no `ffmpeg` in the image | install `ffmpeg` in the image, re-upload (file is stored) |
| Settings show `storage_ready: false` | — | same as the 503 above | fix storage before inviting uploads |

The API never returns "Validation failed." for a storage fault again: storage
faults are 503 with a code an operator can act on.
