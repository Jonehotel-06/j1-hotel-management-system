"""Regression tests for the 2026-11-20 TV gallery media repair.

THE PRODUCTION SYMPTOM
----------------------
``GET /api/rooms-gallery/media/1/thumb/``, ``…/7/thumb/`` and ``…/7/display/``
answered 404 while ``GET /api/rooms-gallery/catalog/`` still listed media 1–9
for active rooms. The catalog is answered from the database alone; the media
endpoint needs the stored file. ``_serve()`` collapsed four different faults
into one 404, so "the record does not exist", "the file is gone", "the API
cannot read the file" and "that rendition does not apply" were indistinguishable
and no repair could target the real one.

These tests pin the honest answers:
    404  record missing / inactive / room not published / rendition n/a
    500  record exists but its file is missing or unreadable (with the key)
    503  the storage configuration itself is broken
and the catalog's new ``available`` flag.

Run:
    DJANGO_SETTINGS_MODULE=config.settings.development \
    python manage.py test tests.test_tv_gallery_media_honesty_2026_11_20 -v 2
"""
import os
import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from PIL import Image
from rest_framework.test import APIClient

from apps.tv_rooms_gallery import storage
from apps.tv_rooms_gallery.models import GalleryMedia, GalleryRoom
from .factories import make_user, make_room_type, make_room


class GalleryMediaHonestyTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='tv-gallery-honesty-')
        self.env = patch.dict(os.environ, {'TV_GALLERY_STORAGE_ROOT': self.directory.name})
        self.env.start()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.env.stop)
        self.admin = make_user('tv-honesty@example.com', role='ADMIN')
        self.room = make_room(make_room_type('Honesty'), 'H-201')
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)
        self.base = '/api/rooms-gallery/'

    # ------------------------------------------------------------------ helpers
    def png(self, colour='blue', size=(48, 32)):
        out = BytesIO()
        Image.new('RGB', size, colour).save(out, 'PNG')
        return out.getvalue()

    def upload_image(self, publish=True):
        response = self.client.post(
            self.base + 'manage/media/',
            {'file': SimpleUploadedFile('room.png', self.png()), 'room_id': str(self.room.pk)},
            format='multipart',
        )
        self.assertEqual(response.status_code, 201, response.data)
        item = GalleryMedia.objects.latest('id')
        if publish:
            GalleryRoom.objects.update_or_create(room=self.room, defaults={'active': True})
        return item

    def anon(self):
        self.client.force_authenticate(user=None)
        return self.client

    # ------------------------------------------------------- valid renditions
    def test_valid_image_renditions_are_served_with_correct_types(self):
        item = self.upload_image()
        client = self.anon()
        for rendition in ('thumb', 'display'):
            response = client.get('%smedia/%s/%s/' % (self.base, item.pk, rendition))
            self.assertEqual(response.status_code, 200, rendition)
            self.assertEqual(response['Content-Type'], 'image/jpeg')
            self.assertGreater(int(response['Content-Length']), 0)
            # The project-wide API middleware pins every /api/ response to
            # "private, no-store" (see apps/core/request_middleware.py), which
            # is why a proxy can never cache a gallery 404 either. The view's
            # own public header only survives outside that middleware.
            self.assertIn('no-store', response['Cache-Control'])
            self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
            self.assertEqual(response['Accept-Ranges'], 'bytes')

    def test_original_image_is_not_public(self):
        item = self.upload_image()
        self.assertEqual(self.anon().get('%smedia/%s/file/' % (self.base, item.pk)).status_code, 404,
                         "the private original must never be published")

    def test_unsupported_rendition_is_404(self):
        item = self.upload_image()
        self.assertEqual(self.anon().get('%smedia/%s/thumbnail/' % (self.base, item.pk)).status_code, 404)

    def test_missing_record_is_404(self):
        self.assertEqual(self.anon().get(self.base + 'media/999999/thumb/').status_code, 404)

    def test_byte_ranges_are_served_for_video_style_seeking(self):
        item = self.upload_image()
        response = self.anon().get('%smedia/%s/thumb/' % (self.base, item.pk), HTTP_RANGE='bytes=0-9')
        self.assertEqual(response.status_code, 206)
        self.assertEqual(len(b''.join(response.streaming_content)), 10)
        self.assertTrue(response['Content-Range'].startswith('bytes 0-9/'))

    # ------------------------------------------ the production 404, reproduced
    def test_record_whose_file_is_gone_is_reported_with_the_key_not_as_404(self):
        """This is the exact production shape: catalog lists it, file 404s.

        Before the repair this answered a bare 404 and was read as "the media
        does not exist", which is why it survived several repairs.
        """
        item = self.upload_image()
        victim = Path(self.directory.name) / item.thumbnail_key
        self.assertTrue(victim.exists())
        victim.unlink()

        response = self.anon().get('%smedia/%s/thumb/' % (self.base, item.pk))
        self.assertEqual(response.status_code, 500)
        payload = response.json()
        self.assertEqual(payload['code'], 'GALLERY_MEDIA_FILE_MISSING')
        self.assertEqual(payload['data']['media_id'], item.pk)
        self.assertEqual(payload['data']['storage_key'], item.thumbnail_key)
        self.assertFalse(payload['data']['retryable'])
        self.assertIn('check_gallery_storage', payload['message'])
        # The filesystem path is never disclosed to an anonymous caller.
        self.assertNotIn(self.directory.name, response.content.decode('utf-8'))

        # The record is untouched — nothing was deleted to silence the error.
        self.assertTrue(GalleryMedia.objects.filter(pk=item.pk).exists())

    def test_unreadable_file_is_distinguished_from_a_missing_one(self):
        item = self.upload_image()
        key = item.thumbnail_key
        real_open = Path.open

        def hostile_open(self_path, *args, **kwargs):
            if self_path.name == key:
                raise PermissionError('denied by test')
            return real_open(self_path, *args, **kwargs)

        with patch.object(Path, 'open', hostile_open):
            response = self.anon().get('%smedia/%s/thumb/' % (self.base, item.pk))
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()['code'], 'GALLERY_MEDIA_FILE_UNREADABLE')

    def test_broken_storage_configuration_is_503_not_404(self):
        item = self.upload_image()
        with patch.dict(os.environ, {'TV_GALLERY_STORAGE_ROOT': '/proc/definitely-not-writable'}):
            response = self.anon().get('%smedia/%s/thumb/' % (self.base, item.pk))
        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload['code'], 'GALLERY_STORAGE_UNAVAILABLE')
        self.assertTrue(payload['data']['retryable'])

    # ------------------------------------------------- visibility stays intact
    def test_inactive_media_room_and_room_type_stay_private(self):
        item = self.upload_image()
        url = '%smedia/%s/thumb/' % (self.base, item.pk)
        self.assertEqual(self.anon().get(url).status_code, 200)

        GalleryMedia.objects.filter(pk=item.pk).update(active=False)
        self.assertEqual(self.anon().get(url).status_code, 404, 'inactive media must not be served')

        GalleryMedia.objects.filter(pk=item.pk).update(active=True)
        GalleryRoom.objects.filter(room=self.room).update(active=False)
        self.assertEqual(self.anon().get(url).status_code, 404, 'a disabled gallery room must not be served')

        GalleryRoom.objects.filter(room=self.room).update(active=True)
        type(self.room).objects.filter(pk=self.room.pk)  # noqa: B018 - keep linters quiet
        self.room.room_type.is_active = False
        self.room.room_type.save(update_fields=['is_active'])
        self.assertEqual(self.anon().get(url).status_code, 404, 'an inactive room type must not be served')

    # ------------------------------------------------------ catalog honesty
    def test_catalog_marks_media_whose_files_are_gone(self):
        item = self.upload_image()
        catalog = self.anon().get(self.base + 'catalog/').json()['data']
        media = catalog['rooms'][0]['media'][0]
        self.assertTrue(media['available'])
        self.assertEqual(media['id'], item.pk)

        (Path(self.directory.name) / item.display_key).unlink()
        catalog = self.anon().get(self.base + 'catalog/').json()['data']
        media = catalog['rooms'][0]['media'][0]
        self.assertFalse(media['available'],
                         'the TV client must be able to skip media it cannot fetch')
        self.assertIn('/media/%s/display/' % item.pk, media['url'],
                      'the URL stays in the payload so a repaired deployment needs no client change')

    # ---------------------------------------------------------- storage API
    def test_storage_resolve_reports_outcomes(self):
        item = self.upload_image()
        path, outcome = storage.resolve(item.thumbnail_key)
        self.assertEqual(outcome, storage.FILE_OK)
        self.assertTrue(path.is_file())

        path.unlink()
        self.assertEqual(storage.resolve(item.thumbnail_key)[1], storage.FILE_MISSING)
        self.assertEqual(storage.resolve('not-a-key')[1], storage.FILE_MISSING)
        self.assertEqual(storage.resolve('..%2fetc%2fpasswd')[1], storage.FILE_MISSING)

    def test_removing_a_malformed_key_is_a_no_op(self):
        storage.remove('../../etc/passwd')  # must not raise, must not touch a path
        storage.remove('')
        storage.remove(None)
