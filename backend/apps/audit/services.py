# apps/audit/services.py
"""Write-side helper for the audit trail. Call from service layers/views."""
import logging

from apps.core.request_context import get_request_context

from .models import AuditLog

logger = logging.getLogger("apps")


def _client_ip(request):
    if request is None:
        return None
    # Do not trust arbitrary X-Forwarded-For values: they are client-controlled
    # unless a deployment has a separately configured trusted-proxy policy.
    return request.META.get("REMOTE_ADDR")


def log_action(*, actor=None, action, instance=None, changes=None, metadata=None, request=None, summary=""):
    """Record a sensitive event. Failure must never break the main operation."""
    try:
        object_type, object_id = "", ""
        if instance is not None:
            object_type = instance._meta.label_lower
            object_id = str(getattr(instance, "pk", "") or "")
        context = get_request_context()
        authenticated_actor = actor if (actor and getattr(actor, "is_authenticated", True)) else None
        terminal = getattr(request, "terminal", None) if request is not None else context.get("terminal")
        request_id = (getattr(request, "request_id", "") if request is not None else "") or context.get("request_id", "")
        role = getattr(authenticated_actor, "role", "") if authenticated_actor else ""
        department = ""
        if authenticated_actor:
            try:
                department = authenticated_actor.staff_profile.department
            except Exception:
                department = ""
        return AuditLog.objects.create(
            actor=authenticated_actor,
            terminal=terminal,
            actor_role=role or "",
            actor_department=department or "",
            request_id=request_id or "",
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
