"""check_gallery_storage must report records whose FILES are gone.

THE PRODUCTION SYMPTOM THIS PINS
--------------------------------
After the 2026-11-20 deploy every Rooms Gallery file was missing: seven media
records, all advertised by the public catalog, all answering
``GALLERY_MEDIA_FILE_MISSING``. The server told the operator to run
``python manage.py check_gallery_storage``.

Run against that deployment, the command said:

    TV gallery storage is ready.

because it only proved that the storage DIRECTORY was writable — it wrote and
deleted its own probe file in the very directory that had lost every gallery
file. The operator followed the instruction and was told everything was fine
while the TV showed nothing. A deployment check that cannot see the fault is
worse than none, because it closes the investigation.

These tests pin the record-level audit it now performs, and the persistence
warning that names the actual cause of that deployment (a storage root inside
the container's own filesystem, which no redeploy can preserve).

Run:
    DJANGO_SETTINGS_MODULE=config.settings.development \\
    python manage.py test tests.test_check_gallery_storage_audit_2026_11_20 -v 2
"""
import os
import tempfile
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from apps.tv_rooms_gallery import storage


class CheckGalleryStorageAuditTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'TV_GALLERY_STORAGE_ROOT': self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def _media(self, **overrides):
        from apps.tv_rooms_gallery.models import GalleryMedia
        fields = dict(
            original_name='lobby.jpg', kind='image', content_type='image/jpeg', size=10,
            storage_key=overrides.pop('storage_key', 'a' * 32),
            thumbnail_key='b' * 32, display_key='c' * 32,
            active=True, processing_status='ready', room_id=None,
        )
        fields.update(overrides)
        return GalleryMedia.objects.create(**fields)

    def test_a_writable_directory_with_lost_files_is_not_reported_ready(self):
        """The exact production state: storage fine, every record's files gone."""
        self._media()
        out, err = None, None
        with self.assertRaises(SystemExit) as ctx:
            call_command('check_gallery_storage', '--skip-roundtrip', stdout=open(os.devnull, 'w'))
        self.assertEqual(ctx.exception.code, 1,
                         'a deployment whose gallery files are gone must fail the check')

    def test_the_report_names_each_unreadable_rendition(self):
        item = self._media()
        from io import StringIO
        buffer = StringIO()
        with self.assertRaises(SystemExit):
            call_command('check_gallery_storage', '--skip-roundtrip', '--json', stdout=buffer)
        payload = buffer.getvalue()
        for key in (item.storage_key, item.thumbnail_key, item.display_key):
            self.assertIn(key, payload, 'the operator must be told which file is missing')
        self.assertIn('"unreadable": 1', payload)

    def test_present_files_pass_the_audit(self):
        from io import StringIO
        item = self._media()
        for key in (item.storage_key, item.thumbnail_key, item.display_key):
            storage.path_for(key).parent.mkdir(parents=True, exist_ok=True)
            storage.path_for(key).write_bytes(b'x')
        buffer = StringIO()
        call_command('check_gallery_storage', '--skip-roundtrip', '--json', stdout=buffer)
        self.assertIn('"unreadable": 0', buffer.getvalue())

    def test_the_report_flags_a_storage_root_that_cannot_survive_a_redeploy(self):
        """A root sharing a filesystem with / and not a mountpoint loses uploads.

        Asserted against the helper directly (with a stubbed os.stat) so the
        result does not depend on how this machine happens to mount /tmp.
        """
        from types import SimpleNamespace
        from unittest.mock import mock_open

        from apps.tv_rooms_gallery.management.commands.check_gallery_storage import Command

        same_device = SimpleNamespace(st_dev=42)
        with patch('os.stat', return_value=same_device), \
                patch('os.path.realpath', return_value='/var/lib/jone-gallery'), \
                patch('builtins.open', mock_open(read_data='/dev/sda1 / ext4 rw 0 0\n')):
            verdict = Command._persistence('/var/lib/jone-gallery')
        self.assertFalse(verdict['mounted'], verdict['detail'])
        self.assertIn('lost on redeploy', verdict['detail'])

    def test_a_mounted_volume_is_accepted(self):
        from types import SimpleNamespace
        from unittest.mock import mock_open

        from apps.tv_rooms_gallery.management.commands.check_gallery_storage import Command

        calls = {'n': 0}

        def stat(path):
            calls['n'] += 1
            return SimpleNamespace(st_dev=99 if path.rstrip('/') == '/var/lib/jone-gallery' else 42)

        with patch('os.stat', side_effect=stat), \
                patch('os.path.realpath', return_value='/var/lib/jone-gallery'), \
                patch('builtins.open', mock_open(read_data='/dev/sdb1 /var/lib/jone-gallery ext4 rw 0 0\n')):
            verdict = Command._persistence('/var/lib/jone-gallery')
        self.assertTrue(verdict['mounted'])

    def test_media_audit_can_be_skipped_for_a_directory_only_check(self):
        from io import StringIO
        self._media()
        buffer = StringIO()
        call_command('check_gallery_storage', '--skip-roundtrip', '--skip-media', '--json', stdout=buffer)
        self.assertIn('"records": 0', buffer.getvalue())

    def test_the_audit_is_read_only(self):
        from io import StringIO
        item = self._media()
        for key in (item.storage_key, item.thumbnail_key, item.display_key):
            storage.path_for(key).write_bytes(b'x')
        # Remove exactly one file: the record is now unreadable, and the audit
        # must report that without touching the two files that are still there.
        storage.path_for(item.thumbnail_key).unlink()
        with self.assertRaises(SystemExit):
            call_command('check_gallery_storage', '--skip-roundtrip', '--json', stdout=StringIO())
        self.assertTrue(storage.path_for(item.storage_key).is_file(),
                        'the audit must never remove a gallery file')
        self.assertTrue(storage.path_for(item.display_key).is_file())
        self.assertFalse(storage.path_for(item.thumbnail_key).is_file())
