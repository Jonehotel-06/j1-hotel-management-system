"""JWT authentication with optional, non-authoritative workstation attribution."""
from datetime import timedelta

from django.utils import timezone
from rest_framework_simplejwt.authentication import JWTAuthentication

from apps.core.request_context import bind_request_context

from .models import Workstation


class WorkstationJWTAuthentication(JWTAuthentication):
    """Resolve account permissions from JWT/DB and record optional terminal hints.

    ``X-JONE-Terminal`` identifies a registered workstation only for audit and
    presence displays. A missing, unknown, or inactive workstation never
    rejects an otherwise valid account request, and the value never scopes or
    grants API access.
    """

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None

        user, token = result
        reference = (request.headers.get("X-JONE-Terminal") or "").strip()
        terminal = None
        if reference and len(reference) <= 48 and getattr(user, "is_staff_member", False):
            terminal = Workstation.objects.filter(reference=reference, is_active=True).first()
            if terminal is not None:
                now = timezone.now()
                stale = terminal.last_seen_at is None or terminal.last_seen_at <= now - timedelta(minutes=2)
                changed_user = terminal.current_staff_id != user.pk
                if stale or changed_user:
                    Workstation.objects.filter(pk=terminal.pk, is_active=True).update(
                        last_seen_at=now,
                        current_staff=user,
                    )
                    terminal.last_seen_at = now
                    terminal.current_staff = user
                    terminal.current_staff_id = user.pk

        request.terminal = terminal
        bind_request_context(
            request_id=getattr(request, "request_id", ""),
            terminal=terminal,
            user=user,
        )
        return user, token
