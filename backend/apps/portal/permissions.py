"""Permissions specific to opaque guest-portal sessions."""
from rest_framework.permissions import BasePermission


class HasPortalSession(BasePermission):
    message = "A valid guest portal session is required."

    def has_permission(self, request, view):
        session = getattr(request, "portal_session", None)
        return bool(session and session.is_active)
