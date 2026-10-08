# apps/audit/services.py
"""Write-side helper for the audit trail. Call from service layers/views."""
import logging

from .models import AuditLog

logger = logging.getLogger("apps")


def _client_ip(request):
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def log_action(*, actor=None, action, instance=None, changes=None, metadata=None, request=None, summary=""):
    """Record a sensitive event. Failure must never break the main operation."""
    try:
        object_type, object_id = "", ""
        if instance is not None:
            object_type = instance._meta.label_lower
            object_id = str(getattr(instance, "pk", "") or "")
        return AuditLog.objects.create(
            actor=actor if (actor and getattr(actor, "is_authenticated", True)) else None,
            action=action,
            object_type=object_type,
            object_id=object_id,
            summary=summary or f"{action} {object_type} {object_id}".strip(),
            changes=changes or {},
            metadata=metadata or {},
            ip_address=_client_ip(request),
        )
    except Exception:
        logger.exception("Failed to write audit log for action=%s", action)
        return None
