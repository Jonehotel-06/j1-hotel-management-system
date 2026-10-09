# apps/accounts/admin_site.py
"""Restricted Django admin fallback.

The Django admin is an internal fallback, not a staff entry point. Only
superusers and management roles (the same set exempt from the Receptionist
Desktop rule in ``desktop_policy``) may sign in. The check runs inside the
login form, before any session is created, so a refused operational account
never receives an admin session cookie.
"""
import logging

from django import forms
from django.contrib.admin import AdminSite
from django.contrib.admin.apps import AdminConfig
from django.contrib.admin.forms import AdminAuthenticationForm

from .desktop_policy import sign_in_policy_applies

logger = logging.getLogger("apps")


def admin_access_allowed(user) -> bool:
    if not (user and user.is_active and user.is_staff):
        return False
    return bool(user.is_superuser or not sign_in_policy_applies(user))


class JOneAdminLoginForm(AdminAuthenticationForm):
    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not admin_access_allowed(user):
            logger.warning("Django admin sign-in refused: user_id=%s role=%s", user.pk, user.role)
            raise forms.ValidationError(
                self.error_messages["invalid_login"],
                code="invalid_login",
                params={"username": self.username_field.verbose_name},
            )


class JOneAdminSite(AdminSite):
    site_header = "J-ONE administration"
    login_form = JOneAdminLoginForm

    def has_permission(self, request):
        return admin_access_allowed(request.user)
