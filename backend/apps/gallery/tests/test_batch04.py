"""Batch 04: multipart uploads, storage, singleton slots and public delivery."""
import io
import tempfile
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.gallery.models import GalleryItem
from apps.rooms.models import Room, RoomImage, RoomType


def photo(name='photo.jpg'):
    buf = io.BytesIO()
    Image.new('RGB', (16, 12), 'blue').save(buf, format='JPEG')
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/jpeg')


class Batch04Tests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.settings_override = override_settings(MEDIA_ROOT=self.media.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.admin = User.objects.create_user('owner04@example.com', 'password', first_name='Owner', last_name='Test', role='ADMIN')
        self.guest = User.objects.create_user('guest04@example.com', 'password', first_name='Guest', last_name='Test', role='GUEST')
        self.api = APIClient()
        self.api.force_authenticate(self.admin)
        self.type = RoomType.objects.create(name='Batch04 Suite', base_price=10000)
        self.room = Room.objects.create(room_number='B04', room_type=self.type)

    def room_url(self):
        return f'/api/admin/rooms/{self.room.pk}/images/'

    def test_single_multiple_append_primary_delete_and_readback(self):
        one = self.api.post(self.room_url(), {'image': photo()}, format='multipart')
        self.assertEqual(one.status_code, 201, one.data)
        first = RoomImage.objects.get(room=self.room)
        self.assertTrue(first.is_primary)
        self.assertTrue(first.image.storage.exists(first.image.name))
        multi = self.api.post(self.room_url(), {'images': [photo('a.jpg'), photo('b.jpg')]}, format='multipart')
        self.assertEqual(multi.status_code, 201, multi.data)
        self.assertEqual(self.room.images.count(), 3)
        self.assertTrue(RoomImage.objects.get(pk=first.pk).is_primary)
        second = self.room.images.exclude(pk=first.pk).first()
        response = self.api.patch(f'/api/admin/rooms/images/{second.pk}/', {'is_primary': True}, format='multipart')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(RoomImage.objects.get(pk=first.pk).is_primary)
        self.assertEqual(self.api.delete(f'/api/admin/rooms/images/{first.pk}/').status_code, 204)
        self.assertEqual(self.room.images.count(), 2)
        self.assertIn('images', self.api.get(f'/api/admin/rooms/{self.room.pk}/').data)

    def test_invalid_upload_and_storage_failure_keep_existing(self):
        self.api.post(self.room_url(), {'image': photo()}, format='multipart')
        bad = SimpleUploadedFile('bad.jpg', b'not-an-image', content_type='image/jpeg')
        response = self.api.post(self.room_url(), {'images': [photo('fine.jpg'), bad]}, format='multipart')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.room.images.count(), 1)
        with patch('apps.rooms.views_admin.verify_stored_upload', return_value=(False, 'Storage unavailable')):
            response = self.api.post(self.room_url(), {'images': [photo('new.jpg')]}, format='multipart')
        self.assertGreaterEqual(response.status_code, 400)
        self.assertEqual(self.room.images.count(), 1)

    def test_homepage_four_slots_replacement_public_and_permissions(self):
        for section in GalleryItem.HomepageSection.values:
            url = f'/api/admin/gallery/homepage/{section}/'
            result = self.api.put(url, {'image': photo(section+'.jpg'), 'alt_text': section}, format='multipart')
            self.assertEqual(result.status_code, 201, result.data)
            replacement = self.api.put(url, {'image': photo(section+'2.jpg')}, format='multipart')
            self.assertEqual(replacement.status_code, 200, replacement.data)
            self.assertEqual(GalleryItem.objects.filter(homepage_section=section).count(), 1)
        public = APIClient().get('/api/gallery/homepage/')
        self.assertEqual(public.status_code, 200)
        self.assertEqual(set(public.data), set(GalleryItem.HomepageSection.values))
        self.assertTrue(public.data['hero']['image_url'])
        self.assertTrue(GalleryItem.objects.get(homepage_section='hero').image.name.endswith('.webp'))
        self.assertNotIn('hero', [item.get('homepage_section') for item in APIClient().get('/api/gallery/').data.get('data', {}).get('items', [])])
        self.api.force_authenticate(self.guest)
        self.assertIn(self.api.put('/api/admin/gallery/homepage/hero/', {'image': photo()}, format='multipart').status_code, (401, 403))
        self.api.force_authenticate(self.admin)
        self.assertEqual(self.api.delete('/api/admin/gallery/homepage/hero/').status_code, 204)
        self.assertNotIn('hero', APIClient().get('/api/gallery/homepage/').data)

    def test_failed_replacement_keeps_published_image(self):
        url = '/api/admin/gallery/homepage/hero/'
        self.api.put(url, {'image': photo()}, format='multipart')
        previous = GalleryItem.objects.get(homepage_section='hero').image.name
        with patch('apps.gallery.homepage.verify_stored_upload', return_value=(False, 'Storage unavailable')):
            response = self.api.put(url, {'image': photo('replacement.jpg')}, format='multipart')
        self.assertGreaterEqual(response.status_code, 400)
        self.assertEqual(GalleryItem.objects.get(homepage_section='hero').image.name, previous)

    def test_room_create_edit_image_and_restricted_upload(self):
        create = self.api.post('/api/admin/rooms/', {
            'room_number': 'B05', 'room_type': self.type.pk, 'image': photo('create.jpg')
        }, format='multipart')
        self.assertEqual(create.status_code, 201, create.data)
        new_room = Room.objects.get(room_number='B05')
        self.assertEqual(new_room.images.count(), 1)
        edit = self.api.patch(f'/api/admin/rooms/{new_room.pk}/', {
            'image': photo('edit.jpg')
        }, format='multipart')
        self.assertEqual(edit.status_code, 200, edit.data)
        self.assertEqual(new_room.images.count(), 2)
        self.api.force_authenticate(self.guest)
        self.assertIn(self.api.post(self.room_url(), {'image': photo()}, format='multipart').status_code, (401, 403))
        self.assertEqual(self.room.images.count(), 0)

    def test_homepage_bad_input_and_write_failure(self):
        url = '/api/admin/gallery/homepage/welcome/'
        self.assertEqual(self.api.put(url, {'alt_text': 'Only text'}, format='multipart').status_code, 400)
        self.assertEqual(self.api.put(url, {'image': SimpleUploadedFile('bad.jpg', b'fake', content_type='image/jpeg')}, format='multipart').status_code, 400)
        with patch('django.core.files.storage.FileSystemStorage.save', side_effect=OSError('permission denied')):
            response = self.api.put(url, {'image': photo()}, format='multipart')
        self.assertGreaterEqual(response.status_code, 400)
        self.assertFalse(GalleryItem.objects.filter(homepage_section='welcome').exists())
