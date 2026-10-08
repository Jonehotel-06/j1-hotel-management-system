# apps/bookings/tasks.py
"""Celery tasks for the bookings app."""
import logging

from celery import shared_task

logger = logging.getLogger("apps")


@shared_task(name="apps.bookings.tasks.expire_pending_bookings")
def expire_pending_bookings():
    """Scheduled every 5 minutes: release inventory held by abandoned bookings."""
    from .services.booking_service import expire_stale_pending_bookings

    count = expire_stale_pending_bookings()
    if count:
        logger.info("Expired %s stale pending booking(s).", count)
    return count


@shared_task(name="apps.bookings.tasks.auto_checkout_due_bookings")
def auto_checkout_due_bookings():
    """Scheduled frequently: automatically check out in-house bookings whose
    scheduled checkout time (checkout date + hotel checkout time, hotel
    timezone) has arrived. Idempotent; each booking is row-locked so
    concurrent workers cannot double-process."""
    from .services.booking_service import auto_checkout_due_bookings as run

    count = run()
    if count:
        logger.info("Automatically checked out %s booking(s).", count)
    return count


@shared_task(name="apps.bookings.tasks.checkout_due_soon_warnings")
def checkout_due_soon_warnings():
    """Scheduled frequently: warn staff ~30 minutes before a checked-in
    guest's scheduled checkout time. Deduplicated per stay."""
    from .services.booking_service import send_checkout_due_soon_notifications

    count = send_checkout_due_soon_notifications()
    if count:
        logger.info("Sent %s checkout-due-soon warning(s).", count)
    return count
