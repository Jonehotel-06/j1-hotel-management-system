# apps/accounts/admin.py
from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm

from .departments import RETIRED_DEPARTMENTAL_ROLES, RetiredDepartmentalRole, assert_assignable_role
from .models import User


class _AssignableRoleMixin:
    """Refuse a retired departmental title in the Django admin too.

    The staff dashboard API is the primary interface, but this fallback admin is
    a second write path to the same column: if only the API enforced the
    one-role-per-department rule, an administrator could recreate the hierarchy
    from here and the rule would be theatre. Both forms therefore resolve the
    role through the same ``assert_assignable_role`` the serializers use.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Retired titles are not offered at all: the select lists one role per
        # department, so the hierarchy cannot be recreated by picking a title.
        field = self.fields.get("role")
        if field is not None:
            field.choices = [
                (value, label) for value, label in field.choices
                if value not in RETIRED_DEPARTMENTAL_ROLES
            ]

    def clean_role(self):
        role = self.cleaned_data.get("role")
        try:
            return assert_assignable_role(role)
        except RetiredDepartmentalRole as exc:
            raise forms.ValidationError(str(exc)) from exc


class JOneUserChangeForm(_AssignableRoleMixin, UserChangeForm):
    pass


class JOneUserCreationForm(_AssignableRoleMixin, UserCreationForm):
    pass


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """Internal fallback interface; the staff dashboard API is primary."""

    form = JOneUserChangeForm
    add_form = JOneUserCreationForm
    list_display = ("email", "full_name", "role", "is_active", "is_staff", "date_joined", "last_login")
    list_filter = ("role", "is_active", "is_staff")
    search_fields = ("email", "first_name", "last_name", "phone")
    ordering = ("-date_joined",)
    readonly_fields = ("date_joined", "last_login")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name", "phone", "profile_image")}),
        ("Role", {"fields": ("role",)}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "email_verified", "groups", "user_permissions")}),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "first_name", "last_name", "password1", "password2", "role", "is_staff"),
        }),
    )

    def save_model(self, request, obj, form, change):
        old_role = None
        if change and obj.pk:
            old_role = User.objects.filter(pk=obj.pk).values_list("role", flat=True).first()
        super().save_model(request, obj, form, change)
        if old_role is None or old_role == obj.role:
            return
        try:
            profile = obj.staff_profile
        except Exception:  # noqa: BLE001 - profile is optional
            return
        from .departments import _normalize_department_code, department_for_role
        from apps.staff_operations.models import Department

        department = department_for_role(obj.role)
        profile.department = department
        if profile.department_ref_id and _normalize_department_code(profile.department_ref.code) != department:
            profile.department_ref = None
        if department and not profile.department_ref_id:
            for candidate in Department.objects.all():
                if department in {
                    _normalize_department_code(candidate.code),
                    _normalize_department_code(candidate.name),
                }:
                    profile.department_ref = candidate
                    break
        profile.save(update_fields=["department", "department_ref", "updated_at"])
