"""Focused API upload regression tests using temporary private storage."""
from io import BytesIO
import tempfile
from unittest.mock import patch
from PIL import Image
from django.test import TestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient
from .factories import make_user, make_room, make_room_type
from apps.tv_rooms_gallery.models import GalleryMedia


class RepairUploadTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='tv-gallery-repair-')
        self.addCleanup(directory.cleanup)
        env = patch.dict('os.environ', {'TV_GALLERY_STORAGE_ROOT': directory.name})
        env.start()
        self.addCleanup(env.stop)
        self.client = APIClient()
        self.client.force_authenticate(user=make_user('repair-admin@example.com', role='ADMIN'))
        self.room = make_room(make_room_type('Repair room'), 'R-202')
        self.base = '/api/rooms-gallery/'

    def test_supported_images_persist_on_correct_room(self):
        for fmt in ('JPEG', 'PNG', 'WEBP'):
            with self.subTest(format=fmt):
                image = BytesIO()
                Image.new('RGB', (24, 20), 'blue').save(image, fmt)
                response = self.client.post(self.base + 'manage/media/', {
                    'file': SimpleUploadedFile('image.' + fmt.lower(), image.getvalue()),
                    'room_id': str(self.room.pk),
                }, format='multipart')
                self.assertEqual(response.status_code, 201)
                item = GalleryMedia.objects.get(pk=response.data['id'])
                self.assertEqual(item.room_id, self.room.pk)
                self.assertEqual(item.kind, 'image')
                self.assertTrue(item.thumbnail_key and item.display_key)
                self.assertEqual(self.client.get(self.base + 'manage/media/').status_code, 200)
