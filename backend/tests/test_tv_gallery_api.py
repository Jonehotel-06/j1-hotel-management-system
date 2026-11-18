"""Focused tests against the actual J-ONE room and authentication models."""
import os
import tempfile
import shutil
import wave
from pathlib import Path
from unittest import skipUnless
from io import BytesIO
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from PIL import Image
from rest_framework.test import APIClient

from apps.tv_rooms_gallery.models import GalleryMedia, GalleryRoom, GallerySettings
from .factories import make_user, make_room_type, make_room


class GalleryApiTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='tv-gallery-test-')
        self.env = patch.dict(os.environ, {'TV_GALLERY_STORAGE_ROOT': self.directory.name})
        self.env.start()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.env.stop)
        self.admin = make_user('tv-admin@example.com', role='ADMIN')
        self.guest = make_user('tv-guest@example.com', role='GUEST')
        self.room = make_room(make_room_type('Gallery test'), 'G-101')
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)
        self.base = '/api/rooms-gallery/'

    def upload(self, name, content, room=True):
        data = {'file': SimpleUploadedFile(name, content)}
        if room: data['room_id'] = str(self.room.pk)
        return self.client.post(self.base + 'manage/media/', data, format='multipart')

    def png(self):
        out = BytesIO(); Image.new('RGB', (40, 30), 'red').save(out, 'PNG'); return out.getvalue()

    def unwrap(self, response):
        return response.data  # DRF Response data before renderer envelope

    def test_public_catalog_only_opted_in_without_private_fields(self):
        r = self.upload('room.png', self.png()); self.assertEqual(r.status_code, 201)
        item = GalleryMedia.objects.get()
        self.client.force_authenticate(user=None)
        self.assertEqual(self.unwrap(self.client.get(self.base + 'catalog/'))['rooms'], [])
        self.assertEqual(self.client.get(self.base + 'media/%s/thumb/' % item.pk).status_code, 404)
        GalleryRoom.objects.create(room=self.room, active=True)
        data = self.unwrap(self.client.get(self.base + 'catalog/'))
        self.assertEqual(data['rooms'][0]['number'], 'G-101')
        self.assertEqual(set(data['rooms'][0]), {'id', 'number', 'type', 'media'})
        self.assertNotIn('storage_key', str(data))
        self.assertEqual(self.client.get(self.base + 'media/%s/thumb/' % item.pk).status_code, 200)
        self.assertEqual(self.client.get(self.base + 'media/%s/file/' % item.pk).status_code, 404)
        self.room.refresh_from_db(); self.assertEqual(self.room.status, 'AVAILABLE')

    def test_unknown_upload_is_private_attachment_and_sanitized(self):
        response = self.upload('../../evil.html', b'<html><script>alert(1)</script></html>')
        self.assertEqual(response.status_code, 201)
        item = GalleryMedia.objects.get()
        self.assertEqual(item.kind, 'other'); self.assertNotIn('/', item.original_name)
        GalleryRoom.objects.create(room=self.room, active=True)
        download = self.client.get(self.base + 'manage/media/%s/file/' % item.pk)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download['Content-Type'], 'application/octet-stream')
        self.assertEqual(download['X-Content-Type-Options'], 'nosniff')
        self.assertTrue(download['Content-Disposition'].startswith('attachment'))
        download.close()
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(self.base + 'media/%s/file/' % item.pk).status_code, 404)

    def test_staff_access_controls_and_invalid_image(self):
        self.assertEqual(self.upload('bad.jpg', b'\xff\xd8\xffNOT-AN-IMAGE').status_code, 400)
        self.assertEqual(GalleryMedia.objects.count(), 0)
        self.assertEqual(os.listdir(self.directory.name), [])
        self.client.force_authenticate(user=self.guest)
        self.assertEqual(self.client.get(self.base + 'manage/media/').status_code, 403)
        self.assertEqual(self.upload('a.html', b'hello').status_code, 403)
        self.client.force_authenticate(user=None)
        self.assertIn(self.client.get(self.base + 'manage/media/').status_code, (401, 403))

    @skipUnless(shutil.which('ffprobe') and shutil.which('ffmpeg'), 'ffprobe and ffmpeg required for video processing')
    def test_range_requests_and_deletion(self):
        content = (Path(__file__).parent / 'fixtures' / 'tv-gallery-test.mp4').read_bytes()
        response = self.upload('room.bin', content); self.assertEqual(response.status_code, 201)
        item = GalleryMedia.objects.get(); self.assertEqual(item.kind, 'video')
        self.assertEqual(item.processing_status, 'queued')
        from apps.tv_rooms_gallery.tasks import process_video
        process_video.run(item.pk, item.processing_token)
        item.refresh_from_db()
        self.assertEqual(item.processing_status, 'ready', item.processing_error)
        from apps.tv_rooms_gallery.storage import path_for
        content = path_for(item.display_key).read_bytes()
        self.assertTrue(path_for(item.thumbnail_key).is_file())
        GalleryRoom.objects.create(room=self.room, active=True)
        self.client.force_authenticate(user=None)
        response = self.client.get(self.base + 'media/%s/file/' % item.pk, HTTP_RANGE='bytes=2-7')
        self.assertEqual(response.status_code, 206)
        self.assertEqual(b''.join(response.streaming_content), content[2:8])
        self.assertEqual(response['Content-Range'], 'bytes 2-7/%s' % len(content))
        self.assertEqual(self.client.get(self.base + 'media/%s/file/' % item.pk, HTTP_RANGE='bytes=%s-' % (len(content) + 1)).status_code, 416)
        self.client.force_authenticate(user=self.admin)
        self.assertEqual(self.client.delete(self.base + 'manage/media/%s/' % item.pk).status_code, 204)
        self.assertEqual(os.listdir(self.directory.name), [])
        self.room.refresh_from_db(); self.assertEqual(self.room.room_number, 'G-101')

    @skipUnless(shutil.which('ffprobe'), 'ffprobe required for audio verification')
    def test_music_requires_setting_and_valid_audio(self):
        out = BytesIO()
        with wave.open(out, 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(8000); wav.writeframes(b'\x00\x00' * 400)
        audio = out.getvalue()
        self.assertEqual(self.upload('sound.wav', audio, room=False).status_code, 201)
        item = GalleryMedia.objects.get()
        self.assertEqual(item.kind, 'audio')
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(self.base + 'media/%s/file/' % item.pk).status_code, 404)
        self.client.force_authenticate(user=self.admin)
        r = self.client.patch(self.base + 'manage/settings/', {'music_id': item.pk, 'music_enabled': True}, format='json')
        self.assertEqual(r.status_code, 200)
        self.client.force_authenticate(user=None)
        self.assertTrue(self.unwrap(self.client.get(self.base + 'catalog/'))['music_url'])
        result = self.client.get(self.base + 'media/%s/file/' % item.pk)
        self.assertEqual(result.status_code, 200); result.close()

    def test_activate_and_reorder(self):
        self.upload('room.png', self.png())
        item = GalleryMedia.objects.get()
        response = self.client.patch(self.base + 'manage/rooms/', {'room_id': self.room.pk, 'active': True}, format='json')
        self.assertEqual(response.status_code, 200)
        response = self.client.patch(self.base + 'manage/media/%s/' % item.pk, {'is_cover': True, 'order': 4}, format='json')
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db(); self.assertTrue(item.is_cover); self.assertEqual(item.display_order, 4)

    def test_real_jwt_permissions(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        self.client.force_authenticate(user=None)
        admin_token = str(RefreshToken.for_user(self.admin).access_token)
        guest_token = str(RefreshToken.for_user(self.guest).access_token)
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + guest_token)
        self.assertEqual(self.client.get(self.base + 'manage/rooms/').status_code, 403)
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + admin_token)
        self.assertEqual(self.client.get(self.base + 'manage/rooms/').status_code, 200)
        self.client.credentials(HTTP_AUTHORIZATION='Bearer invalid')
        self.assertEqual(self.client.get(self.base + 'manage/rooms/').status_code, 401)

    def test_bad_room_id_returns_validation_error(self):
        response = self.client.post(self.base + 'manage/media/',
                                    {'room_id': 'abc', 'file': SimpleUploadedFile('x.bin', b'abc')},
                                    format='multipart')
        self.assertEqual(response.status_code, 400)

    def test_private_root_rejects_project_and_media(self):
        from django.conf import settings
        from django.core.exceptions import ImproperlyConfigured
        from apps.tv_rooms_gallery.storage import root
        for location in (str(settings.MEDIA_ROOT), str(settings.BASE_DIR), '/'):
            with patch.dict(os.environ, {'TV_GALLERY_STORAGE_ROOT': location}):
                with self.subTest(location=location), self.assertRaises(ImproperlyConfigured):
                    root()

    def test_key_collision_does_not_remove_existing_file(self):
        from pathlib import Path
        from apps.tv_rooms_gallery.storage import save_chunks
        class Fixed:
            hex = 'a' * 32
        existing = Path(self.directory.name) / Fixed.hex
        existing.write_bytes(b'existing')
        with patch('apps.tv_rooms_gallery.storage.uuid4', return_value=Fixed()):
            with self.assertRaises(FileExistsError):
                save_chunks([b'new'], 100)
        self.assertEqual(existing.read_bytes(), b'existing')

    @skipUnless(shutil.which('ffprobe'), 'ffprobe required for AV verification')
    def test_fake_mp4_is_private_and_reported(self):
        response = self.upload('fake.mp4', b'\x00\x00\x00\x18ftypisomnot video')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['kind'], 'other')
        self.assertIn('private', response.data['notice'])
        item = GalleryMedia.objects.get()
        GalleryRoom.objects.create(room=self.room, active=True)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(self.base + 'media/%s/file/' % item.pk).status_code, 404)

    def test_orphan_cleanup_is_dry_run_and_ignores_recent_files(self):
        import time
        from django.core.management import call_command
        from pathlib import Path
        old = Path(self.directory.name) / ('b' * 32)
        new = Path(self.directory.name) / ('c' * 32)
        old.write_bytes(b'old'); new.write_bytes(b'new')
        os.utime(old, (time.time() - 100000, time.time() - 100000))
        output = BytesIO()
        # Django commands write text streams.
        from io import StringIO
        text = StringIO(); call_command('tv_gallery_cleanup_orphans', stdout=text)
        self.assertTrue(old.exists()); self.assertIn('dry run', text.getvalue())
        call_command('tv_gallery_cleanup_orphans', delete=True, stdout=StringIO())
        self.assertFalse(old.exists()); self.assertTrue(new.exists())

    def test_delete_reports_storage_cleanup_failure(self):
        self.upload('test.png', self.png())
        item = GalleryMedia.objects.get()
        with patch('apps.tv_rooms_gallery.storage.remove', side_effect=OSError('failed')):
            result = self.client.delete(self.base + 'manage/media/%s/' % item.pk)
        self.assertEqual(result.status_code, 200)
        self.assertIn('cleanup', result.data['warning'])
        self.assertFalse(GalleryMedia.objects.filter(pk=item.pk).exists())

    def test_docker_app_root_can_use_a_separate_private_mount(self):
        from django.test import override_settings
        from apps.tv_rooms_gallery.storage import root
        with override_settings(BASE_DIR='/app', MEDIA_ROOT='/app/media'):
            self.assertEqual(str(root()), self.directory.name)

    def test_missing_ffprobe_fails_closed_and_explains_private_storage(self):
        content = (Path(__file__).parent / 'fixtures' / 'tv-gallery-test.mp4').read_bytes()
        with patch('apps.tv_rooms_gallery.media.shutil.which', return_value=None):
            response = self.upload('video.mp4', content)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['kind'], 'other')
        self.assertIn('ffprobe', response.data['notice'])

    @skipUnless(shutil.which('ffprobe') and shutil.which('ffmpeg'), 'ffmpeg required')
    def test_failed_processing_is_not_public_and_can_be_retried(self):
        from apps.tv_rooms_gallery.tasks import process_video
        content = (Path(__file__).parent / 'fixtures' / 'tv-gallery-test.mp4').read_bytes()
        self.upload('room.mp4', content)
        item = GalleryMedia.objects.get()
        GalleryRoom.objects.create(room=self.room, active=True)
        with patch('apps.tv_rooms_gallery.tasks.make_video', side_effect=Exception('not disclosed')):
            process_video.run(item.pk, item.processing_token)
        item.refresh_from_db()
        self.assertEqual(item.processing_status, 'failed')
        self.assertNotIn('not disclosed', item.processing_error)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(self.base + 'media/%s/file/' % item.pk).status_code, 404)
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(self.base + 'manage/media/%s/retry-processing/' % item.pk)
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db(); self.assertEqual(item.processing_status, 'queued')
        self.client.force_authenticate(user=self.guest)
        self.assertEqual(self.client.post(self.base + 'manage/media/%s/retry-processing/' % item.pk).status_code, 403)

    @skipUnless(shutil.which('ffprobe') and shutil.which('ffmpeg'), 'ffmpeg required')
    def test_duplicate_task_does_not_reprocess_ready_video(self):
        from apps.tv_rooms_gallery.tasks import process_video
        content = (Path(__file__).parent / 'fixtures' / 'tv-gallery-test.mp4').read_bytes()
        self.upload('room.mp4', content)
        item = GalleryMedia.objects.get(); token = item.processing_token
        process_video.run(item.pk, token); item.refresh_from_db()
        self.assertEqual(item.processing_status, 'ready')
        with patch('apps.tv_rooms_gallery.tasks.make_video') as transcode:
            process_video.run(item.pk, token)
        transcode.assert_not_called()

    @skipUnless(shutil.which('ffprobe'), 'ffprobe required')
    def test_stale_worker_discards_derivatives_after_retry_or_deletion(self):
        from apps.tv_rooms_gallery.tasks import process_video
        from apps.tv_rooms_gallery import storage
        content = (Path(__file__).parent / 'fixtures' / 'tv-gallery-test.mp4').read_bytes()
        self.upload('race.mp4', content)
        item = GalleryMedia.objects.get(); token = item.processing_token
        made = []
        def stale(_):
            out, _ = storage.save_chunks([b'derivative'], 100)
            poster, _ = storage.save_chunks([b'poster'], 100)
            made.extend([out, poster])
            GalleryMedia.objects.filter(pk=item.pk).update(processing_token='new-generation', processing_status='queued')
            return out, poster
        with patch('apps.tv_rooms_gallery.tasks.make_video', side_effect=stale):
            process_video.run(item.pk, token)
        item.refresh_from_db(); self.assertEqual(item.processing_status, 'queued')
        self.assertTrue(all(not storage.path_for(key).exists() for key in made))
        self.assertFalse(item.display_key)
        token = item.processing_token; made.clear()
        def deleted(_):
            out, _ = storage.save_chunks([b'derivative'], 100)
            poster, _ = storage.save_chunks([b'poster'], 100)
            made.extend([out, poster]); GalleryMedia.objects.filter(pk=item.pk).delete()
            return out, poster
        with patch('apps.tv_rooms_gallery.tasks.make_video', side_effect=deleted):
            process_video.run(item.pk, token)
        self.assertTrue(all(not storage.path_for(key).exists() for key in made))
        storage.remove(item.storage_key)

    @skipUnless(shutil.which('ffprobe'), 'ffprobe required')
    def test_queue_failure_and_timeout_are_retryable_private_failures(self):
        from apps.tv_rooms_gallery.tasks import dispatch_video, process_video
        from apps.tv_rooms_gallery.video_processing import VideoProcessingError
        content = (Path(__file__).parent / 'fixtures' / 'tv-gallery-test.mp4').read_bytes()
        self.upload('failure.mp4', content)
        item = GalleryMedia.objects.get()
        with patch('apps.tv_rooms_gallery.tasks.process_video.apply_async', side_effect=OSError('broker failed')):
            dispatch_video(item)
        item.refresh_from_db(); self.assertEqual(item.processing_status, 'failed')
        response = self.client.post(self.base + 'manage/media/%s/retry-processing/' % item.pk)
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        with patch('apps.tv_rooms_gallery.tasks.make_video', side_effect=VideoProcessingError('Video processing failed or exceeded its time limit.')):
            process_video.run(item.pk, item.processing_token)
        item.refresh_from_db(); self.assertEqual(item.processing_status, 'failed')
        self.assertIn('time limit', item.processing_error)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(self.base + 'media/%s/file/' % item.pk).status_code, 404)

    def test_all_management_routes_reject_guests_and_anonymous_requests(self):
        paths = [
            ('get', 'manage/rooms/'), ('patch', 'manage/rooms/'),
            ('get', 'manage/media/'), ('post', 'manage/media/'),
            ('patch', 'manage/media/1/'), ('delete', 'manage/media/1/'),
            ('post', 'manage/media/1/retry-processing/'), ('get', 'manage/media/1/file/'),
            ('get', 'manage/settings/'), ('patch', 'manage/settings/'),
        ]
        for user in (self.guest, None):
            self.client.force_authenticate(user=user)
            for method, path in paths:
                with self.subTest(role='guest' if user else 'anonymous', method=method, path=path):
                    response = getattr(self.client, method)(self.base + path)
                    self.assertIn(response.status_code, (401, 403))
        self.assertEqual(GalleryMedia.objects.count(), 0)

    def test_stale_transcode_cleanup_is_opt_in_and_requires_worker_confirmation(self):
        import time
        from io import StringIO
        from django.core.management import call_command
        from django.core.management.base import CommandError
        directory = Path(self.directory.name)
        self.assertEqual(self.upload('safe.png', self.png()).status_code, 201)
        item = GalleryMedia.objects.get()
        referenced = directory / item.storage_key
        old = directory / '.transcode-abcdef12'
        recent = directory / '.transcode-recent12'
        invalid = directory / '.transcode-do-not-touch'
        orphan = directory / ('a' * 32)
        link = directory / '.transcode-link1234'
        for path in (old, recent, invalid, orphan):
            path.write_bytes(b'synthetic')
        link.symlink_to(referenced)
        age = time.time() - 100000
        for path in (old, invalid, orphan, referenced):
            os.utime(path, (age, age))
        os.utime(link, (age, age), follow_symlinks=False)
        text = StringIO(); call_command('tv_gallery_cleanup_orphans', stdout=text)
        self.assertNotIn(old.name, text.getvalue())
        text = StringIO(); call_command('tv_gallery_cleanup_orphans', transcode_temp_only=True, stdout=text)
        self.assertIn(old.name, text.getvalue())
        for path in (old, recent, invalid, orphan, referenced, link):
            self.assertTrue(path.exists())
        self.assertNotIn(recent.name, text.getvalue())
        self.assertNotIn(invalid.name, text.getvalue())
        self.assertNotIn(link.name, text.getvalue())
        with self.assertRaises(CommandError):
            call_command('tv_gallery_cleanup_orphans', transcode_temp_only=True, delete=True, stdout=StringIO())
        self.assertTrue(orphan.exists())  # refusal occurs before any deletion
        call_command('tv_gallery_cleanup_orphans', transcode_temp_only=True, delete=True,
                     confirm_gallery_worker_stopped=True, stdout=StringIO())
        self.assertFalse(old.exists()); self.assertTrue(orphan.exists())
        for path in (recent, invalid, referenced, link):
            self.assertTrue(path.exists())

    def test_cleanup_refuses_less_than_24_hours_even_with_confirmation(self):
        from io import StringIO
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command('tv_gallery_cleanup_orphans', transcode_temp_only=True, delete=True,
                         confirm_gallery_worker_stopped=True, older_than_hours=23, stdout=StringIO())

    @skipUnless(shutil.which('ffprobe') and shutil.which('ffmpeg'), 'ffmpeg required')
    def test_process_kill_leaves_temp_that_opt_in_cleanup_can_remove(self):
        import select
        import subprocess
        import sys
        import time
        from io import StringIO
        from django.core.management import call_command
        from apps.tv_rooms_gallery import storage
        source, _ = storage.save_chunks([b'synthetic source'], 100)
        code = '''
import json, os, time, django
from unittest.mock import patch
django.setup()
from apps.tv_rooms_gallery.video_processing import make_video
count = 0
def fake_run(args, timeout):
    global count
    count += 1
    if count == 1:
        return json.dumps({'format': {'duration': '1'}, 'streams': [{'codec_type': 'video', 'width': 160, 'height': 90}]}).encode()
    print('ENCODING', flush=True)
    time.sleep(300)
with patch('apps.tv_rooms_gallery.video_processing._run', side_effect=fake_run):
    make_video(os.environ['TEST_SOURCE_KEY'])
'''
        env = dict(os.environ, TEST_SOURCE_KEY=source)
        proc = subprocess.Popen([sys.executable, '-u', '-c', code], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([proc.stdout], [], [], 15)
            self.assertTrue(ready, 'Synthetic worker did not reach the encoding stage')
            self.assertEqual(proc.stdout.readline().strip(), 'ENCODING')
            proc.kill(); proc.wait(timeout=5)
            temporary = list(Path(self.directory.name).glob('.transcode-*'))
            self.assertEqual(len(temporary), 1)
            output = StringIO()
            call_command('tv_gallery_cleanup_orphans', transcode_temp_only=True, stdout=output)
            self.assertNotIn(temporary[0].name, output.getvalue())  # fresh files are protected
            old = time.time() - 100000
            os.utime(temporary[0], (old, old))
            output = StringIO()
            call_command('tv_gallery_cleanup_orphans', transcode_temp_only=True, stdout=output)
            self.assertIn(temporary[0].name, output.getvalue())
            call_command('tv_gallery_cleanup_orphans', transcode_temp_only=True, delete=True,
                         confirm_gallery_worker_stopped=True, stdout=StringIO())
            self.assertFalse(temporary[0].exists())
            self.assertTrue(storage.path_for(source).exists())
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait(timeout=5)
            proc.stdout.close(); proc.stderr.close()
