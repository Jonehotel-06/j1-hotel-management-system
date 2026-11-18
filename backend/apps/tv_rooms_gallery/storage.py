"""Private gallery store, deliberately independent of public Django MEDIA_ROOT.

TV_GALLERY_STORAGE_ROOT must be an absolute directory on a durable shared mount,
not reachable by any web server. Keys are random and never supplied by clients.
This local filesystem implementation is not appropriate for ephemeral hosts.
"""
import os
import re
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


_KEY = re.compile(r"^[0-9a-f]{32}$")


def root():
    raw = os.environ.get("TV_GALLERY_STORAGE_ROOT", "")
    if not raw or not Path(raw).is_absolute():
        raise ImproperlyConfigured("TV_GALLERY_STORAGE_ROOT must be an absolute private directory.")
    directory = Path(raw).resolve()
    media = Path(settings.MEDIA_ROOT).resolve()
    base = Path(settings.BASE_DIR).resolve()
    # Source checkouts use <project>/backend; the Docker image uses /app.
    project = base.parent if base.name == "backend" else base
    if (directory == Path('/') or directory == media or media in directory.parents
            or directory in media.parents or directory == project or project in directory.parents
            or directory in project.parents):
        raise ImproperlyConfigured("Gallery storage must be outside the project and public media root.")
    if not directory.is_dir():
        raise ImproperlyConfigured("Private gallery storage directory does not exist.")
    return directory


def path_for(key):
    if not _KEY.fullmatch(key or ""):
        raise ValueError("Invalid gallery storage key")
    return root() / key


def save_chunks(chunks, max_size):
    key = uuid4().hex
    path = path_for(key)
    total = 0
    created = False
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(fd, 'wb') as target:
            for chunk in chunks:
                total += len(chunk)
                if total > max_size:
                    raise ValueError("File exceeds the configured upload size limit.")
                target.write(chunk)
        if total == 0:
            raise ValueError("File is empty.")
        return key, total
    except BaseException:
        if created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def remove(key):
    if key:
        path_for(key).unlink(missing_ok=True)
