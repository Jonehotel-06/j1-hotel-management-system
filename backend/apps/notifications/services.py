# apps/notifications/services.py
"""Notification fan-out helpers used by other apps' service layers."""
import logging

from django.db import transaction

from apps.accounts.models import User

from .models import Notification

logger = logging.getLogger("apps")


def notify_users(users, *, type, title, message, link=""):
    """Efficient single-query write for one-to-many notifications.

    A notification is a NON-CRITICAL side effect of the caller's real work
    (creating a booking, reconciling a payment, ...), so its failure must
    never break — or silently roll back — that work.

    The INSERT therefore runs inside its own savepoint. This matters
    because ``bulk_create`` internally uses ``atomic(savepoint=False)``:
    when its INSERT fails at the database (production, 2026-10-18: MySQL
    error 1366 storing "→" in a non-utf8mb4 column), that inner block
    marks the WHOLE enclosing transaction ``needs_rollback``. Catching
    the exception here was not enough — the caller's ``atomic()`` then
    rolled back on exit and a successfully created booking vanished
    while the API still answered 201. With the savepoint below, the
    failure rolls back only the notification INSERT, the exception is
    logged, and the caller's transaction stays committable.
    """
    users = [u for u in users if u and u.is_active]
    if not users:
        return
    try:
        with transaction.atomic():
            Notification.objects.bulk_create(
                [Notification(recipient=u, type=type, title=title, message=message, link=link) for u in users]
            )
    except Exception:
        logger.exception("Failed to create notifications: %s", title)


def notify_staff(*, type, title, message, link="", roles=("ADMIN", "MANAGER", "RECEPTIONIST")):
    staff = list(User.objects.filter(is_active=True, role__in=roles))
    notify_users(staff, type=type, title=title, message=message, link=link)


def unread_count(user):
    if not user or not user.is_authenticated:
        return 0
    return Notification.objects.filter(recipient=user, is_read=False).count()
