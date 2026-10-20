# config/celery.py
"""Celery application configuration.

Background tasks live in the apps that own them (for example bookings and
finance). Celery is reserved for bounded operational work that must not block
an API request. Transactional email remains synchronous by deliberate design.
"""
import os

from celery import Celery
from celery.schedules import crontab
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
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
    # Safety net for POS-to-inventory stock depletion. Sales deplete stock
    # inline at delivery; this sweep only resolves hand-offs that could not be
    # posted then (recipe added later, stock topped up after a shortage, or
    # sales that predate automatic depletion). Idempotent per POS order.
    "process-pending-pos-stock-consumption": {
        "task": "apps.inventory.tasks.process_pending_pos_stock_consumption",
        "schedule": crontab(minute="*/10"),
    },
}
