"""Inspect (or explicitly delete) old gallery orphans and transcoding temp files.

Backend-aware (2026-11-22): the ACTIVE durable layer is cleaned. On the
filesystem backend that is the resolved directory, exactly as before. On the
object backend the durable layer is the configured private bucket — its keys
are listed and unreferenced ones (and stale local cache copies) are removed;
the local directory still hides nothing durable, so it is scanned for cache
strays only. Same safety rules apply in every mode: the referenced set comes
from the database, the minimum age is 24 hours, transcoding temp files require
the worker-stopped confirmation, and nothing is deleted without --delete.
"""
import re
import time
from django.core.management.base import BaseCommand, CommandError
from apps.tv_rooms_gallery import storage
from apps.tv_rooms_gallery.models import GalleryMedia


class Command(BaseCommand):
    help = "Dry-run gallery orphan cleanup; optionally inspect old transcoding temporary files."

    def add_arguments(self, parser):
        parser.add_argument('--delete', action='store_true')
        parser.add_argument('--older-than-hours', type=int, default=24)
        parser.add_argument('--transcode-temp-only', action='store_true',
                            help='Inspect only old .transcode-* files, not random-key orphan files.')
        parser.add_argument('--confirm-gallery-worker-stopped', action='store_true',
                            help='Acknowledge all gallery workers and their FFmpeg processes are stopped.')

    def handle(self, *args, **options):
        hours = options['older_than_hours']
        if hours < 24:
            raise CommandError('The minimum safety age is 24 hours.')
        temp_only = options['transcode_temp_only']
        if temp_only and options['delete'] and not options['confirm_gallery_worker_stopped']:
            raise CommandError('Stop all gallery workers and FFmpeg processes, then pass --confirm-gallery-worker-stopped before deleting transcoding temp files.')
        referenced = set()
        for keys in GalleryMedia.objects.values_list('storage_key', 'thumbnail_key', 'display_key').iterator():
            referenced.update(k for k in keys if k)
        cutoff = time.time() - hours * 3600
        count = 0
        mode, _reason = storage.backend_mode()

        # ---- durable layer ---------------------------------------------------
        if mode == 'object':
            from apps.tv_rooms_gallery import object_store

            for key, last_modified in object_store.list_keys_with_age():
                orphan = not temp_only and key not in referenced
                if not orphan:
                    continue
                if last_modified is not None and last_modified > cutoff:
                    continue
                count += 1
                self.stdout.write('object %s' % key)
                if options['delete']:
                    try:
                        object_store.delete(key)
                    except Exception as exc:
                        raise CommandError('Object %s could not be deleted: %s' % (key, exc))
        else:
            for file in storage.root().iterdir():
                # tempfile.mkstemp uses the fixed prefix and an eight-character
                # random suffix. Never traverse, follow symlinks or remove other files.
                temp = temp_only and re.fullmatch(r'\.transcode-[a-z0-9_]{8}', file.name)
                orphan = not temp_only and re.fullmatch(r'[0-9a-f]{32}', file.name) and file.name not in referenced
                if not (temp or orphan) or file.is_symlink() or not file.is_file():
                    continue
                if file.stat().st_mtime > cutoff:
                    continue
                count += 1
                self.stdout.write(file.name)
                if options['delete']:
                    file.unlink()

        # ---- local cache/scratch layer (object mode only) --------------------
        if mode == 'object':
            try:
                cache_dir = storage.root() / storage.OBJECT_CACHE_DIR
            except Exception:
                cache_dir = None
            if cache_dir is not None and cache_dir.is_dir():
                for file in cache_dir.iterdir():
                    stray = file.name not in referenced and re.fullmatch(r'[0-9a-f]{32}', file.name)
                    if not stray or not file.is_file():
                        continue
                    if file.stat().st_mtime > cutoff:
                        continue
                    count += 1
                    self.stdout.write('cache %s' % file.name)
                    if options['delete']:
                        file.unlink()
        self.stdout.write(('%s ' % count) + ('deleted.' if options['delete'] else 'candidate(s); dry run only.'))
