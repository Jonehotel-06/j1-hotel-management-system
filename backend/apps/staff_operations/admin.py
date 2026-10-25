"""Django-admin fallback for workforce records.

Attendance, leave and disciplinary events are audit trails; they are exposed
read-only so an administrator can investigate without being able to rewrite
the record.
"""
from django import forms
from django.contrib import admin

from .models import (
    AttendanceEvent,
    AttendanceRecord,
    Department,
    LeaveRequest,
    LeaveRequestEvent,
    ShiftAssignment,
    ShiftAssignmentEvent,
    ShiftTemplate,
    StaffDisciplinaryRecord,
    StaffDocument,
    StaffPerformanceReview,
    StaffProfile,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "head", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "name", "slug")


class StaffProfileAdminForm(forms.ModelForm):
    """Keep department edits in the Django admin consistent with User.role."""

    class Meta:
        model = StaffProfile
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        user = cleaned.get("user")
        if user is None and self.instance.user_id:
            user = self.instance.user
        if user is None:
            return cleaned
        department = cleaned.get("department", self.instance.department)
        department_ref = cleaned.get("department_ref", self.instance.department_ref)
        from apps.accounts.departments import validate_role_department

        try:
            validate_role_department(
                user.role,
                department,
                department_ref=getattr(department_ref, "code", "") if department_ref else "",
            )
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc
        return cleaned


@admin.register(StaffProfile)
class StaffProfileAdmin(admin.ModelAdmin):
    form = StaffProfileAdminForm
    list_display = ("employee_code", "user", "department", "department_ref", "job_title", "employment_status")
    list_filter = ("department", "employment_type", "employment_status")
    search_fields = ("employee_code", "user__email", "user__first_name", "user__last_name", "job_title")
    autocomplete_fields = ()


@admin.register(ShiftTemplate)
class ShiftTemplateAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "department", "start_time", "end_time", "is_active")
    list_filter = ("department", "is_active")
    search_fields = ("code", "name")


@admin.register(ShiftAssignment)
class ShiftAssignmentAdmin(admin.ModelAdmin):
    list_display = ("reference", "staff", "template", "status", "planned_start_at", "planned_end_at", "department")
    list_filter = ("status", "department")
    search_fields = ("reference", "staff__user__email")
    date_hierarchy = "planned_start_at"
    readonly_fields = ("cancelled_at", "actual_start_at", "actual_end_at", "actual_worked_minutes")


@admin.register(ShiftAssignmentEvent)
class ShiftAssignmentEventAdmin(ReadOnlyAdmin):
    list_display = ("shift", "type", "actor", "created_at")
    list_filter = ("type",)


@admin.register(AttendanceRecord)
class AttendanceRecordAdmin(admin.ModelAdmin):
    list_display = ("reference", "staff", "business_date", "status", "clock_in_at", "clock_out_at", "worked_minutes")
    list_filter = ("status", "business_date")
    search_fields = ("reference", "staff__user__email")
    date_hierarchy = "business_date"
    readonly_fields = ("reference", "clock_in_at", "clock_out_at", "worked_minutes", "last_event_at")


@admin.register(AttendanceEvent)
class AttendanceEventAdmin(ReadOnlyAdmin):
    list_display = ("record", "type", "actor", "occurred_at")
    list_filter = ("type",)


@admin.register(LeaveRequest)
class LeaveRequestAdmin(admin.ModelAdmin):
    list_display = ("reference", "staff", "type", "status", "start_date", "end_date", "reviewer")
    list_filter = ("type", "status")
    search_fields = ("reference", "staff__user__email")
    date_hierarchy = "start_date"
    readonly_fields = ("reviewed_at", "cancelled_at")


@admin.register(LeaveRequestEvent)
class LeaveRequestEventAdmin(ReadOnlyAdmin):
    list_display = ("leave_request", "type", "actor", "created_at")
    list_filter = ("type",)


@admin.register(StaffDisciplinaryRecord)
class StaffDisciplinaryRecordAdmin(admin.ModelAdmin):
    list_display = ("reference", "staff", "category", "status", "incident_at", "warning_level", "reviewed_by")
    list_filter = ("status", "category", "warning_level", "appeal_status")
    search_fields = ("reference", "staff__user__email", "allegation_summary")
    date_hierarchy = "incident_at"


@admin.register(StaffDocument)
class StaffDocumentAdmin(admin.ModelAdmin):
    list_display = ("reference", "staff", "category", "title", "issued_date", "expiry_date", "is_confidential", "is_active")
    list_filter = ("category", "is_confidential", "is_active")
    search_fields = ("reference", "staff__user__email", "title")


@admin.register(StaffPerformanceReview)
class StaffPerformanceReviewAdmin(admin.ModelAdmin):
    list_display = ("staff", "reviewer", "period_start", "period_end", "rating", "status", "review_date")
    list_filter = ("status", "rating")
    search_fields = ("staff__user__email", "reviewer__email")
