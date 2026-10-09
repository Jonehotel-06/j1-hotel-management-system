# apps/accounts/apps.py
from django.apps import AppConfig
from django.contrib.admin import apps as admin_apps


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    verbose_name = "Accounts & Authentication"


class JOneAdminConfig(admin_apps.AdminConfig):
    """Replaces django.contrib.admin so the admin site is restricted to management.

    default = False so Django keeps AccountsConfig as the default app config for apps.accounts.
    """

    default = False
    default_site = "apps.accounts.admin_site.JOneAdminSite"
