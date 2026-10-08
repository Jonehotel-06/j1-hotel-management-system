# config/celery.py
"""Celery application configuration.

Background tasks live in the apps that own them (for example bookings and
finance). Celery is reserved for bounded operational work that must not block
an API request. Transactional email remains synchronous by deliberate design.
"""
import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

app = Celery("jone_hotel")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    # Release inventory held by abandoned pending bookings.
    "expire-pending-bookings": {
        "task": "apps.bookings.tasks.expire_pending_bookings",
        "schedule": crontab(minute="*/5"),
    },
    # Automatic checkout at the hotel's configured checkout time (idempotent,
    # row-locked; never checks out early). Runs every 5 minutes.
    "auto-checkout-due-bookings": {
        "task": "apps.bookings.tasks.auto_checkout_due_bookings",
        "schedule": crontab(minute="*/5"),
    },
    # 30-minute checkout warning to staff (deduplicated per stay).
    "checkout-due-soon-warnings": {
        "task": "apps.bookings.tasks.checkout_due_soon_warnings",
        "schedule": crontab(minute="*/5"),
    },
    # Recognize the prior completed hotel night as an immutable finance event.
    # Per-stay source keys make a missed/repeated beat run safe; checkout also
    # catches up the final elapsed nights before it closes a stay.
    "post-previous-night-accommodation": {
        "task": "apps.finance.tasks.post_previous_night_accommodation",
        "schedule": crontab(hour=0, minute=10),
    },
}
