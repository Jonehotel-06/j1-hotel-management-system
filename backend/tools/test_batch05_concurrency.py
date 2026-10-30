"""Execute real threaded portion tests with a disposable file-backed SQLite DB.

Run: python tools/test_batch05_concurrency.py (from backend/).
For production row-lock validation run the same test class with MySQL settings.
Does not modify the application's configured database or require a new package.
"""
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

import django
from django.conf import settings
from django.core.management import call_command

with tempfile.TemporaryDirectory(prefix="batch05-concurrency-") as directory:
    settings.DATABASES["default"] = {
        "ENGINE": "django.db.backends.sqlite3", "NAME": str(Path(directory) / "unused.sqlite3"),
        "TEST": {"NAME": str(Path(directory) / "test.sqlite3")},
        "OPTIONS": {"transaction_mode": "IMMEDIATE", "timeout": 30, "init_command": "PRAGMA journal_mode=WAL"},
    }
    django.setup()
    call_command("test", "tests.test_batch05_food.PreparedFoodConcurrencyTests", interactive=False, verbosity=2)
