# apps/notifications/services.py
"""Notification fan-out helpers used by other apps' service layers."""
import logging

from apps.accounts.models import User

from .models import Notification

logger = logging.getLogger("apps")


def notify_users(users, *, type, title, message, link=""):
    """Efficient single-query write for one-to-many notifications."""
    users = [u for u in users if u and u.is_active]
    if not users:
        return
    try:
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
