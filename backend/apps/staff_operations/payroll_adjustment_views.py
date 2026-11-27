"""Private salary-adjustment endpoints (partial month, unpaid absence, disciplinary, other).

Mounted beside the other payroll routes under ``payroll/salary-adjustments/``
and gated by the SAME payroll capabilities: ``payroll.view`` / ``payroll.manage``
to read, ``payroll.manage`` to record, ``payroll.approve`` to decide or reverse.
Nothing here computes pay outside the payroll run or touches the ledger.
"""
from django.db.models import Prefetch, Q
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.views import APIView

from apps.accounts.capabilities import has_capability
from apps.accounts.models import User
from apps.audit.services import log_action
from apps.core.approval_audit import ApprovalCategory, approval_decision_metadata
from apps.core.pagination import StandardPagination
from apps.core.permissions import HasCapability
from apps.core.responses import success_response

from .models import PayrollSalaryAdjustment, PayrollSalaryAdjustmentEvent
from .payroll_adjustment_serializers import (
    SalaryAdjustmentCreateSerializer,
    SalaryAdjustmentDecisionSerializer,
    SalaryAdjustmentDetailSerializer,
    SalaryAdjustmentInputSerializer,
    SalaryAdjustmentReversalSerializer,
    SalaryAdjustmentSerializer,
)
from .payroll_views import _page, _private_response
from .services.payroll_adjustment_service import (
    VIOLATION_CATEGORIES,
    adjustment_context,
    calculate_adjustment,
    compensation_for_month,
    decide_salary_adjustment,
    eligible_staff,
    month_summary,
    record_salary_adjustment,
    reverse_salary_adjustment,
    submit_salary_adjustment,
    validate_payroll_month,
)

READ_CAPABILITIES = ("payroll.view", "payroll.manage", "payroll.approve")


def _queryset(*, detail=False):
    queryset = (
        PayrollSalaryAdjustment.objects.exclude(staff__is_superuser=True).exclude(staff__role=User.Role.ADMIN)
        .select_related("staff", "staff__staff_profile", "created_by", "decided_by", "reversed_by",
                        "applied_payroll_line", "applied_payroll_line__period")
    )
    if detail:
        queryset = queryset.prefetch_related(
            Prefetch("events", queryset=PayrollSalaryAdjustmentEvent.objects.select_related("actor")),
        )
    return queryset


def _adjustment(reference, *, detail=False):
    value = _queryset(detail=detail).filter(reference=reference).first()
    if value is None:
        raise NotFound("Salary adjustment not found.")
    return value


def _audit_metadata(record):
    return {
        "reference": record.reference, "staff_id": record.staff_id, "payroll_year": record.payroll_year,
        "payroll_month": record.payroll_month, "adjustment_type": record.adjustment_type, "status": record.status,
        "monthly_salary": str(record.monthly_salary), "prorated_salary": str(record.prorated_salary),
        "total_deductions": str(record.total_deductions), "additional_payment": str(record.additional_payment),
        "final_payable": str(record.final_payable),
    }


@extend_schema(tags=["Admin · Payroll"], summary="List, look up context for, or record a salary adjustment")
class SalaryAdjustmentListCreateView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = READ_CAPABILITIES
    require_any_capability = True
    pagination_class = StandardPagination

    def get(self, request):
        params = request.query_params
        if params.get("options") == "context":
            # Salary information auto-loaded when a staff member is selected.
            staff = eligible_staff(params.get("staff_id"))
            context = adjustment_context(
                staff=staff, payroll_year=params.get("payroll_year"), payroll_month=params.get("payroll_month"),
            )
            return _private_response(success_response(context))
        if params.get("options") == "categories":
            return _private_response(success_response([{"value": code, "label": label} for code, label in VIOLATION_CATEGORIES]))
        queryset = _queryset().order_by("-payroll_year", "-payroll_month", "-created_at", "-pk")
        if staff_id := params.get("staff_id"):
            queryset = queryset.filter(staff_id=staff_id)
        if department := params.get("department"):
            queryset = queryset.filter(staff__staff_profile__department__iexact=department.strip())
        if year := params.get("payroll_year"):
            queryset = queryset.filter(payroll_year=year)
        if month := params.get("payroll_month"):
            queryset = queryset.filter(payroll_month=month)
        if adjustment_type := params.get("adjustment_type"):
            queryset = queryset.filter(adjustment_type=adjustment_type.upper())
        if status_filter := params.get("status"):
            queryset = queryset.filter(status=status_filter.upper())
        if search := params.get("search"):
            queryset = queryset.filter(
                Q(reference__icontains=search) | Q(staff__email__icontains=search)
                | Q(staff__first_name__icontains=search) | Q(staff__last_name__icontains=search)
                | Q(staff__staff_profile__employee_code__icontains=search)
            )
        return _page(self, request, queryset, SalaryAdjustmentSerializer)

    def post(self, request):
        if not has_capability(request.user, "payroll.manage"):
            raise PermissionDenied("This user cannot record salary adjustments.")
        serializer = SalaryAdjustmentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        record, created = record_salary_adjustment(
            actor=request.user,
            staff_id=data.pop("staff_id"), payroll_year=data.pop("payroll_year"), payroll_month=data.pop("payroll_month"),
            adjustment_type=data.pop("adjustment_type"), calculation_basis=data.pop("calculation_basis"),
            days_worked=data.pop("days_worked"), absence_days=data.pop("absence_days"), applicable_days=data.pop("applicable_days"),
            amount=data.pop("amount"), other_direction=data.pop("other_direction") or None,
            additional_payment=data.pop("additional_payment"), submit=data.pop("submit"), idempotency_key=data.pop("idempotency_key"),
            **data,
        )
        if created:
            log_action(
                actor=request.user, action="PAYROLL_SALARY_ADJUSTMENT_CREATED", instance=record, request=request,
                metadata=_audit_metadata(record),
            )
        message = (
            ("Salary adjustment recorded and submitted for independent approval." if record.status == PayrollSalaryAdjustment.Status.PENDING_APPROVAL
             else "Salary adjustment saved as a draft.") if created else "Existing salary adjustment returned."
        )
        return _private_response(success_response(
            SalaryAdjustmentDetailSerializer(_adjustment(record.reference, detail=True)).data,
            message=message, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        ))


@extend_schema(tags=["Admin · Payroll"], summary="Preview a salary adjustment calculation (nothing is saved)")
class SalaryAdjustmentPreviewView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = READ_CAPABILITIES
    require_any_capability = True

    def post(self, request):
        serializer = SalaryAdjustmentInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        year, month = validate_payroll_month(data["payroll_year"], data["payroll_month"])
        staff = eligible_staff(data["staff_id"])
        compensation = compensation_for_month(staff.pk, year, month)
        if compensation is None:
            return _private_response(success_response({
                "calculation": None, "summary": None,
                "error": "No salary terms are effective for this staff member in the selected month; record compensation first.",
            }))
        calc = calculate_adjustment(
            compensation=compensation, adjustment_type=data["adjustment_type"], calculation_basis=data["calculation_basis"],
            payroll_year=year, payroll_month=month, days_worked=data["days_worked"], absence_days=data["absence_days"],
            applicable_days=data["applicable_days"], amount=data["amount"], other_direction=data["other_direction"] or None,
            additional_payment=data["additional_payment"],
        )
        summary = month_summary(staff_id=staff.pk, payroll_year=year, payroll_month=month, extra=calc, strict=True)
        return _private_response(success_response({"calculation": calc.as_dict(), "summary": summary}))


class SalaryAdjustmentDetailView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = READ_CAPABILITIES
    require_any_capability = True

    def get(self, request, reference):
        return _private_response(success_response(SalaryAdjustmentDetailSerializer(_adjustment(reference, detail=True)).data))


class SalaryAdjustmentSubmitView(APIView):
    permission_classes = [HasCapability]
    required_capability = "payroll.manage"

    def post(self, request, reference):
        record = submit_salary_adjustment(adjustment=_adjustment(reference), actor=request.user)
        log_action(actor=request.user, action="PAYROLL_SALARY_ADJUSTMENT_SUBMITTED", instance=record, request=request, metadata=_audit_metadata(record))
        return _private_response(success_response(
            SalaryAdjustmentDetailSerializer(_adjustment(reference, detail=True)).data,
            message="Salary adjustment submitted for independent approval.",
        ))


class SalaryAdjustmentDecisionView(APIView):
    permission_classes = [HasCapability]
    required_capability = "payroll.approve"

    def post(self, request, reference):
        serializer = SalaryAdjustmentDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        before = _adjustment(reference)
        record = decide_salary_adjustment(
            adjustment=before, actor=request.user, approved=data["approved"], decision_note=data["decision_note"],
        )
        metadata = _audit_metadata(record)
        metadata.update(approval_decision_metadata(
            category=ApprovalCategory.PAYROLL, reference=record.reference,
            initiated_by=before.created_by, decided_by=request.user,
            previous_status=before.status, new_status=record.status, note=record.decision_note,
        ))
        log_action(
            actor=request.user,
            action="PAYROLL_SALARY_ADJUSTMENT_APPROVED" if data["approved"] else "PAYROLL_SALARY_ADJUSTMENT_REJECTED",
            instance=record, request=request, metadata=metadata,
        )
        message = (
            "Salary adjustment approved. It will be applied by the payroll run for its month; nothing has been paid."
            if data["approved"] else "Salary adjustment rejected."
        )
        return _private_response(success_response(SalaryAdjustmentDetailSerializer(_adjustment(reference, detail=True)).data, message=message))


class SalaryAdjustmentReverseView(APIView):
    """Withdraw a draft/pending record (payroll.manage) or reverse an approved one (payroll.approve)."""

    permission_classes = [HasCapability]
    required_capabilities = ("payroll.manage", "payroll.approve")
    require_any_capability = True

    def post(self, request, reference):
        serializer = SalaryAdjustmentReversalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        record = reverse_salary_adjustment(adjustment=_adjustment(reference), actor=request.user, reason=serializer.validated_data["reason"])
        log_action(
            actor=request.user, action="PAYROLL_SALARY_ADJUSTMENT_REVERSED", instance=record, request=request,
            metadata={**_audit_metadata(record), "reason": record.reversal_reason},
        )
        return _private_response(success_response(
            SalaryAdjustmentDetailSerializer(_adjustment(reference, detail=True)).data,
            message="Salary adjustment reversed. The record is kept for history and no longer affects pay.",
        ))
