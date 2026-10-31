"""Private payroll and payslip endpoints, separated from ordinary staff views."""
from decimal import Decimal, ROUND_HALF_UP
from html import escape
from io import BytesIO

from django.db.models import Count, DecimalField, ExpressionWrapper, F, OuterRef, Prefetch, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.views import APIView

from apps.accounts.capabilities import has_capability
from apps.accounts.models import User
from apps.audit.services import log_action
from apps.core.approval_audit import ApprovalCategory, approval_decision_metadata
from apps.core.pagination import StandardPagination
from apps.core.permissions import HasCapability
from apps.core.responses import success_response
from apps.finance.models import CashSession

from .models import (
    PayrollEvent, PayrollLine, PayrollPeriod, PayrollSalaryPayment,
    PayrollStockShortageCase, StaffCompensation, StaffLatenessDeduction, StaffProfile,
)
from .payroll_serializers import (
    PayrollPeriodCreateSerializer,
    PayrollPeriodDetailSerializer,
    PayrollPeriodListSerializer,
    PayrollPaymentSerializer,
    PayrollReviewSerializer,
    PayrollSalaryPaymentCreateSerializer,
    PayrollSalaryPaymentReversalSerializer,
    PayrollSalaryPaymentSerializer,
    PayrollStockShortageCreateSerializer,
    PayrollStockShortageDecisionSerializer,
    PayrollStockShortageDetailSerializer,
    PayrollStockShortageSerializer,
    LatenessPolicySerializer,
    LatenessPolicyUpdateSerializer,
    LatenessRevocationDecisionSerializer,
    LatenessRevocationRequestSerializer,
    StaffLatenessDeductionSerializer,
    StaffCompensationCreateSerializer,
    StaffCompensationSerializer,
    MyPayslipSerializer,
)
from .services.payroll_service import (
    create_compensation,
    create_payroll_period,
    create_stock_shortage_case,
    decide_lateness_revocation,
    decide_stock_shortage_case,
    get_lateness_policy,
    pay_payroll_period,
    request_lateness_revocation,
    record_payroll_salary_payment,
    reverse_payroll_salary_payment,
    review_payroll_period,
    submit_payroll_period,
    update_lateness_policy,
)


PAYROLL_MONEY = DecimalField(max_digits=16, decimal_places=2)


def _payroll_line_queryset():
    recorded = (
        PayrollSalaryPayment.objects.filter(line_id=OuterRef("pk"), reversal_of__isnull=True)
        .order_by().values("line_id").annotate(total=Sum("amount")).values("total")[:1]
    )
    reversed_amount = (
        PayrollSalaryPayment.objects.filter(line_id=OuterRef("pk"), reversal_of__isnull=False)
        .order_by().values("line_id").annotate(total=Sum("amount")).values("total")[:1]
    )
    return PayrollLine.objects.select_related("staff", "compensation").annotate(
        _salary_recorded=Coalesce(Subquery(recorded, output_field=PAYROLL_MONEY), Value(Decimal("0.00"), output_field=PAYROLL_MONEY), output_field=PAYROLL_MONEY),
        _salary_reversed=Coalesce(Subquery(reversed_amount, output_field=PAYROLL_MONEY), Value(Decimal("0.00"), output_field=PAYROLL_MONEY), output_field=PAYROLL_MONEY),
    ).annotate(
        salary_amount_paid=ExpressionWrapper(F("_salary_recorded") - F("_salary_reversed"), output_field=PAYROLL_MONEY),
        salary_balance=ExpressionWrapper(F("net_pay") - F("salary_amount_paid"), output_field=PAYROLL_MONEY),
    )


def _payroll_queryset(*, detail=False):
    recorded = (
        PayrollSalaryPayment.objects.filter(line__period_id=OuterRef("pk"), reversal_of__isnull=True)
        .order_by().values("line__period_id").annotate(total=Sum("amount")).values("total")[:1]
    )
    reversed_amount = (
        PayrollSalaryPayment.objects.filter(line__period_id=OuterRef("pk"), reversal_of__isnull=False)
        .order_by().values("line__period_id").annotate(total=Sum("amount")).values("total")[:1]
    )
    queryset = PayrollPeriod.objects.select_related(
        "created_by", "reviewed_by", "approved_by", "paid_by", "replaces", "cash_session",
        "accrual_transaction", "payment_transaction",
    ).annotate(
        line_count=Count("lines"),
        _salary_recorded=Coalesce(Subquery(recorded, output_field=PAYROLL_MONEY), Value(Decimal("0.00"), output_field=PAYROLL_MONEY), output_field=PAYROLL_MONEY),
        _salary_reversed=Coalesce(Subquery(reversed_amount, output_field=PAYROLL_MONEY), Value(Decimal("0.00"), output_field=PAYROLL_MONEY), output_field=PAYROLL_MONEY),
    ).annotate(
        salary_amount_paid=ExpressionWrapper(F("_salary_recorded") - F("_salary_reversed"), output_field=PAYROLL_MONEY),
        salary_balance=ExpressionWrapper(F("total_net") - F("salary_amount_paid"), output_field=PAYROLL_MONEY),
    )
    if detail:
        queryset = queryset.prefetch_related(
            Prefetch("lines", queryset=_payroll_line_queryset()),
            Prefetch("events", queryset=PayrollEvent.objects.select_related("actor")),
        )
    return queryset


def _period(reference, *, detail=False):
    value = (_payroll_queryset(detail=detail).filter(reference=reference).first())
    if value is None:
        raise NotFound("Payroll run not found.")
    return value


def _private_response(response):
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["Pragma"] = "no-cache"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _page(view, request, queryset, serializer):
    paginator = StandardPagination()
    page = paginator.paginate_queryset(queryset, request, view=view)
    return _private_response(paginator.get_paginated_response(serializer(page, many=True).data))


@extend_schema(tags=["Admin · Payroll"], summary="View or add effective-dated compensation")
class StaffCompensationListCreateView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("payroll.view", "payroll.manage")
    require_any_capability = True
    pagination_class = StandardPagination

    def get(self, request):
        if request.query_params.get("options") == "staff":
            profiles = StaffProfile.objects.select_related("user").filter(user__is_active=True, user__is_superuser=False).exclude(user__role__in=[User.Role.GUEST, User.Role.ADMIN])
            if term := request.query_params.get("search"):
                profiles = profiles.filter(Q(user__first_name__icontains=term) | Q(user__last_name__icontains=term) | Q(employee_code__icontains=term))
            return _private_response(success_response([{"id": p.user_id, "staff_id": p.user_id, "full_name": p.user.full_name, "employee_code": p.employee_code, "department": p.department} for p in profiles.order_by("user_id")[:100]]))
        queryset = StaffCompensation.objects.exclude(staff__is_superuser=True).exclude(staff__role=User.Role.ADMIN).select_related("staff", "created_by", "staff__staff_profile").order_by(
            "staff__last_name", "staff__first_name", "-effective_from", "-pk"
        )
        if staff_id := request.query_params.get("staff_id"):
            queryset = queryset.filter(staff_id=staff_id)
        if department := request.query_params.get("department"):
            queryset = queryset.filter(staff__staff_profile__department__iexact=department)
        if search := request.query_params.get("search"):
            queryset = queryset.filter(
                Q(staff__email__icontains=search) | Q(staff__first_name__icontains=search)
                | Q(staff__last_name__icontains=search) | Q(staff__staff_profile__employee_code__icontains=search)
            )
        return _page(self, request, queryset, StaffCompensationSerializer)

    def post(self, request):
        if not has_capability(request.user, "payroll.manage"):
            raise PermissionDenied("This user cannot manage compensation records.")
        serializer = StaffCompensationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        staff = User.objects.filter(pk=data["staff_id"]).first()
        if staff is None:
            raise NotFound("Staff account not found.")
        compensation = create_compensation(
            staff=staff,
            actor=request.user,
            effective_from=data["effective_from"],
            currency=data["currency"],
            basic_salary=data["basic_salary"],
            housing_allowance=data["housing_allowance"],
            transport_allowance=data["transport_allowance"],
            overtime_rate=data["overtime_rate"],
            notes=data["notes"],
        )
        log_action(
            actor=request.user, action="PAYROLL_COMPENSATION_CREATED", instance=compensation, request=request,
            metadata={"staff_id": staff.pk, "employee_code": getattr(getattr(staff, "staff_profile", None), "employee_code", ""), "effective_from": data["effective_from"].isoformat(), "currency": data["currency"].upper()},
        )
        return _private_response(success_response(
            StaffCompensationSerializer(StaffCompensation.objects.select_related("staff", "created_by", "staff__staff_profile").get(pk=compensation.pk)).data,
            message="Effective-dated compensation recorded.", status=status.HTTP_201_CREATED,
        ))


def _shortage_queryset(*, detail=False):
    queryset = PayrollStockShortageCase.objects.select_related(
        "department", "stock_count", "created_by", "decided_by",
    )
    if detail:
        queryset = queryset.prefetch_related("allocations__staff", "allocations__applied_payroll_line__period")
    return queryset


def _shortage(reference, *, detail=False):
    value = _shortage_queryset(detail=detail).filter(reference=reference).first()
    if value is None:
        raise NotFound("Stock-shortage case not found.")
    return value


class NotProcurementPayrollAccess(BasePermission):
    """Inventory authority must not expose payroll shortage cases to procurement."""

    def has_permission(self, request, view):
        return request.user.role != User.Role.PROCUREMENT_OFFICER


@extend_schema(tags=["Admin · Payroll"], summary="List or record a department stock-shortage case")
class PayrollStockShortageListCreateView(APIView):
    permission_classes = [HasCapability, NotProcurementPayrollAccess]
    required_capabilities = ("payroll.view", "payroll.manage", "payroll.approve", "inventory.manage")
    require_any_capability = True
    pagination_class = StandardPagination

    def get(self, request):
        if request.query_params.get("options") == "stock":
            from apps.inventory.models import StockCountLine
            from .services.payroll_shortage_service import purchase_basis
            from django.utils import timezone
            from django.core.exceptions import ValidationError as DjangoValidationError
            rows = StockCountLine.objects.select_related("item", "stock_count__location").filter(
                stock_count__status="POSTED", variance__lt=0,
            ).filter(payroll_recovery_claim__isnull=True).exclude(
                stock_count_id__in=PayrollStockShortageCase.objects.filter(
                    discovered_at__isnull=True, stock_count__isnull=False,
                ).exclude(status="REJECTED").values("stock_count_id"),
            ).order_by("item__name", "pk")
            if search := request.query_params.get("search"):
                rows = rows.filter(Q(item__name__icontains=search) | Q(item__sku__icontains=search))
            result = []
            from rest_framework import serializers
            page = serializers.IntegerField(min_value=1).run_validation(request.query_params.get("page", 1))
            page_size = serializers.IntegerField(min_value=1, max_value=100).run_validation(request.query_params.get("page_size", 25))
            from .services.payroll_shortage_service import validate_discovery
            at = serializers.DateTimeField().run_validation(request.query_params.get("discovered_at"))
            validate_discovery(at)
            total = rows.count()
            for line in rows[(page-1)*page_size:page*page_size]:
                row = {"id": line.pk, "item_id": line.item_id, "name": line.item.name,
                       "stock_count_reference": line.stock_count.reference,
                       "quantity": str(-line.variance), "currency": line.item.currency, "location": line.stock_count.location.name}
                try:
                    cost, evidence = purchase_basis(line, at)
                    row.update(unit_cost=str(cost), amount=str((-line.variance * cost).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)), cost_basis=evidence)
                except DjangoValidationError as exc:
                    row["error"] = "; ".join(exc.messages)
                result.append(row)
            response = success_response(result)
            response.data["pagination"] = {"page": page, "page_size": page_size, "count": total, "has_next": page*page_size < total}
            return _private_response(response)
        if request.query_params.get("options") == "staff":
            from .models import Department
            from .services.payroll_shortage_service import eligible_staff, validate_discovery
            from rest_framework import serializers
            at = serializers.DateTimeField().run_validation(request.query_params.get("discovered_at"))
            validate_discovery(at)
            raw_ids = request.query_params.get("department_ids", request.query_params.get("department_id", ""))
            ids = serializers.ListField(child=serializers.IntegerField(min_value=1), allow_empty=False).run_validation(raw_ids.split(",") if raw_ids else [])
            departments = list(Department.objects.filter(pk__in=ids, is_active=True).order_by("pk"))
            if len(ids) != len(set(ids)) or len(departments) != len(ids):
                raise serializers.ValidationError({"department_ids": "Select distinct active departments."})
            return _private_response(success_response([row for dept in departments for row in eligible_staff(at, dept)]))
        queryset = _shortage_queryset().order_by("-incident_date", "-created_at", "-pk")
        if status_filter := request.query_params.get("status"):
            queryset = queryset.filter(status=status_filter.upper())
        if department_id := request.query_params.get("department_id"):
            queryset = queryset.filter(Q(department_id=department_id) | Q(responsible_departments__id=department_id)).distinct()
        if search := request.query_params.get("search"):
            queryset = queryset.filter(
                Q(reference__icontains=search) | Q(item_summary__icontains=search)
                | Q(department_name__icontains=search) | Q(department_code__icontains=search)
                | Q(responsible_departments__name__icontains=search) | Q(responsible_departments__code__icontains=search)
            ).distinct()
        return _page(self, request, queryset, PayrollStockShortageSerializer)

    def post(self, request):
        serializer = PayrollStockShortageCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        case = create_stock_shortage_case(
            actor=request.user,
            department_id=data.get("department_id"), department_ids=data.get("department_ids"),
            item_summary=data["item_summary"],
            total_amount=data.get("total_amount"),
            stock_count_reference=data["stock_count_reference"],
            incident_date=data["incident_date"],
            effective_date=data["effective_date"],
            evidence_reference=data["evidence_reference"],
            notes=data["notes"],
            discovered_at=data["discovered_at"], responsibility_type=data["responsibility_type"],
            staff_ids=data["staff_ids"], stock_count_line_id=data["stock_count_line_id"],
        )
        log_action(
            actor=request.user, action="PAYROLL_STOCK_SHORTAGE_CASE_CREATED", instance=case, request=request,
            metadata={
                "reference": case.reference, "department": case.department_name,
                "department_code": case.department_code, "total_amount": str(case.total_amount),
                "stock_count": case.stock_count.reference if case.stock_count else "",
            },
        )
        return _private_response(success_response(
            PayrollStockShortageDetailSerializer(_shortage(case.reference, detail=True)).data,
            message="Stock-shortage case recorded for Admin approval. No payroll deduction has been made yet.",
            status=status.HTTP_201_CREATED,
        ))


@extend_schema(tags=["Admin · Payroll"], summary="View a department stock-shortage case and its shares")
class PayrollStockShortageDetailView(APIView):
    permission_classes = [HasCapability, NotProcurementPayrollAccess]
    required_capabilities = ("payroll.view", "payroll.manage", "payroll.approve", "inventory.manage")
    require_any_capability = True

    def get(self, request, reference):
        return _private_response(success_response(
            PayrollStockShortageDetailSerializer(_shortage(reference, detail=True)).data
        ))


@extend_schema(tags=["Admin · Payroll"], summary="Admin-only decision for a department stock-shortage case")
class PayrollStockShortageDecisionView(APIView):
    """Only the ADMIN role may make every department employee responsible."""

    permission_classes = [IsAuthenticated]

    def post(self, request, reference):
        serializer = PayrollStockShortageDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        case = decide_stock_shortage_case(
            case=_shortage(reference), actor=request.user,
            approved=data["approved"], decision_note=data["decision_note"], allocations=data["allocations"],
        )
        log_action(
            actor=request.user,
            action="PAYROLL_STOCK_SHORTAGE_APPROVED" if data["approved"] else "PAYROLL_STOCK_SHORTAGE_REJECTED",
            instance=case, request=request,
            metadata={
                "reference": case.reference, "department": case.department_name,
                "total_amount": str(case.total_amount), "staff_count": case.allocations.count(),
                "decision_note": case.decision_note,
            },
        )
        return _private_response(success_response(
            PayrollStockShortageDetailSerializer(_shortage(case.reference, detail=True)).data,
            message=(
                "Admin approved the department-wide stock-shortage shares. They will be deducted once by the next eligible payroll run."
                if data["approved"] else "Admin rejected the stock-shortage case. No payroll deduction will be made."
            ),
        ))


def _lateness_queryset():
    return StaffLatenessDeduction.objects.select_related(
        "staff", "staff__staff_profile", "attendance_record", "shift",
        "applied_payroll_line__period", "reimbursement_payroll_line__period",
        "revocation_requested_by", "decided_by",
    )


def _lateness(reference):
    value = _lateness_queryset().filter(reference=reference).first()
    if value is None:
        raise NotFound("Lateness deduction not found.")
    return value


@extend_schema(tags=["Admin · Payroll"], summary="View or update the automatic lateness deduction policy")
class LatenessPolicyView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("payroll.view", "payroll.manage", "payroll.approve")
    require_any_capability = True

    def get(self, request):
        return _private_response(success_response(LatenessPolicySerializer(get_lateness_policy()).data))

    def patch(self, request):
        serializer = LatenessPolicyUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        policy = update_lateness_policy(
            actor=request.user,
            is_enabled=data["is_enabled"],
            grace_minutes=data["grace_minutes"],
            rate_per_minute=data["rate_per_minute"],
        )
        log_action(
            actor=request.user, action="PAYROLL_LATENESS_POLICY_UPDATED", instance=policy, request=request,
            metadata={
                "is_enabled": policy.is_enabled, "grace_minutes": policy.grace_minutes,
                "deduction_percent": str(policy.deduction_percent),
            },
        )
        return _private_response(success_response(
            LatenessPolicySerializer(policy).data,
            message="Lateness deduction policy updated.",
        ))


@extend_schema(tags=["Admin · Payroll"], summary="List automatic lateness deductions")
class StaffLatenessDeductionListView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("payroll.view", "payroll.manage", "payroll.approve")
    require_any_capability = True
    pagination_class = StandardPagination

    def get(self, request):
        queryset = _lateness_queryset().exclude(staff__is_superuser=True).exclude(staff__role=User.Role.ADMIN).order_by("-business_date", "-created_at", "-pk")
        if status_filter := request.query_params.get("status"):
            queryset = queryset.filter(status=status_filter.upper())
        if staff_id := request.query_params.get("staff_id"):
            queryset = queryset.filter(staff_id=staff_id)
        if search := request.query_params.get("search"):
            queryset = queryset.filter(
                Q(reference__icontains=search) | Q(staff__email__icontains=search)
                | Q(staff__first_name__icontains=search) | Q(staff__last_name__icontains=search)
                | Q(staff__staff_profile__employee_code__icontains=search)
                | Q(attendance_record__reference__icontains=search)
            )
        return _page(self, request, queryset, StaffLatenessDeductionSerializer)


@extend_schema(tags=["Admin · Payroll"], summary="Record a reason and request revocation of a lateness deduction")
class StaffLatenessDeductionRevocationRequestView(APIView):
    permission_classes = [HasCapability]
    required_capability = "payroll.manage"

    def post(self, request, reference):
        serializer = LatenessRevocationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        deduction = request_lateness_revocation(
            deduction=_lateness(reference), actor=request.user,
            reason=data["reason"], evidence_reference=data["evidence_reference"],
        )
        log_action(
            actor=request.user, action="PAYROLL_LATENESS_REVOCATION_REQUESTED", instance=deduction, request=request,
            metadata={
                "reference": deduction.reference, "staff_id": deduction.staff_id,
                "amount": str(deduction.amount), "reason": deduction.revocation_reason,
                "evidence_reference": deduction.revocation_evidence_reference,
            },
        )
        return _private_response(success_response(
            StaffLatenessDeductionSerializer(_lateness(deduction.reference)).data,
            message="Revocation request recorded. It now needs a final decision from an Admin.",
        ))


@extend_schema(tags=["Admin · Payroll"], summary="Admin-only final decision on a lateness-deduction revocation")
class StaffLatenessDeductionRevocationDecisionView(APIView):
    """A manager records the reason; only an ADMIN-role account decides."""

    permission_classes = [IsAuthenticated]

    def post(self, request, reference):
        serializer = LatenessRevocationDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        deduction = decide_lateness_revocation(
            deduction=_lateness(reference), actor=request.user,
            approved=data["approved"], decision_note=data["decision_note"],
        )
        log_action(
            actor=request.user,
            action="PAYROLL_LATENESS_REVOCATION_APPROVED" if data["approved"] else "PAYROLL_LATENESS_REVOCATION_REJECTED",
            instance=deduction, request=request,
            metadata={
                "reference": deduction.reference, "staff_id": deduction.staff_id,
                "amount": str(deduction.amount), "status": deduction.status,
                "decision_note": deduction.decision_note,
            },
        )
        return _private_response(success_response(
            StaffLatenessDeductionSerializer(_lateness(deduction.reference)).data,
            message=(
                "Revocation approved. The deduction is cancelled or will be added back on a later payroll run."
                if data["approved"] else "Revocation rejected. The lateness deduction stands."
            ),
        ))

@extend_schema(tags=["Admin · Payroll"], summary="List or prepare a payroll run")
class PayrollPeriodListCreateView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("payroll.view", "payroll.manage", "payroll.approve")
    require_any_capability = True
    pagination_class = StandardPagination

    def get(self, request):
        queryset = _payroll_queryset().order_by("-starts_on", "-created_at", "-pk")
        for field in ("status", "department", "currency"):
            if value := request.query_params.get(field):
                queryset = queryset.filter(**{field: value.upper() if field == "status" else value})
        if starts_after := request.query_params.get("starts_after"):
            queryset = queryset.filter(starts_on__gte=starts_after)
        if starts_before := request.query_params.get("starts_before"):
            queryset = queryset.filter(starts_on__lt=starts_before)
        return _page(self, request, queryset, PayrollPeriodListSerializer)

    def post(self, request):
        if not has_capability(request.user, "payroll.manage"):
            raise PermissionDenied("This user cannot prepare payroll.")
        serializer = PayrollPeriodCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        replaces = None
        if data["replaces_reference"]:
            replaces = PayrollPeriod.objects.filter(reference=data["replaces_reference"]).first()
            if replaces is None:
                raise NotFound("Payroll run to replace was not found.")
        period, created = create_payroll_period(
            actor=request.user,
            starts_on=data["starts_on"],
            ends_on=data["ends_on"],
            department=data["department"],
            adjustments=data["adjustments"],
            idempotency_key=data["idempotency_key"],
            replaces=replaces,
        )
        if created:
            log_action(
                actor=request.user, action="PAYROLL_RUN_CREATED", instance=period, request=request,
                metadata={"reference": period.reference, "line_count": period.lines.count(), "department": period.department},
            )
        period = _period(period.reference, detail=True)
        return _private_response(success_response(
            PayrollPeriodDetailSerializer(period).data,
            message="Payroll run prepared." if created else "Existing payroll run returned.",
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        ))


class PayrollPeriodDetailView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("payroll.view", "payroll.manage", "payroll.approve")
    require_any_capability = True

    def get(self, request, reference):
        return _private_response(success_response(PayrollPeriodDetailSerializer(_period(reference, detail=True)).data))


class PayrollSubmitView(APIView):
    permission_classes = [HasCapability]
    required_capability = "payroll.manage"

    def post(self, request, reference):
        period = submit_payroll_period(period=_period(reference), actor=request.user)
        log_action(actor=request.user, action="PAYROLL_RUN_SUBMITTED", instance=period, request=request, metadata={"reference": period.reference})
        return _private_response(success_response(PayrollPeriodDetailSerializer(_period(reference, detail=True)).data, message="Payroll submitted for independent review."))


class PayrollReviewView(APIView):
    permission_classes = [HasCapability]
    required_capability = "payroll.approve"

    def post(self, request, reference):
        serializer = PayrollReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        before = _period(reference)
        period = review_payroll_period(
            period=before, reviewer=request.user, approved=data["approved"], review_note=data["review_note"]
        )
        metadata = {"reference": period.reference, "review_note": data["review_note"]}
        metadata.update(approval_decision_metadata(
            category=ApprovalCategory.PAYROLL, reference=period.reference,
            initiated_by=before.created_by, decided_by=request.user,
            previous_status=before.status, new_status=period.status,
            note=period.review_note,
        ))
        log_action(
            actor=request.user, action="PAYROLL_RUN_APPROVED" if data["approved"] else "PAYROLL_RUN_REJECTED",
            instance=period, request=request, metadata=metadata,
        )
        message = "Payroll approved and accrued to the immutable ledger." if data["approved"] else "Payroll run rejected. Create a replacement run to correct it."
        return _private_response(success_response(PayrollPeriodDetailSerializer(_period(reference, detail=True)).data, message=message))


class PayrollPayView(APIView):
    permission_classes = [HasCapability]
    required_capability = "payroll.manage"

    def post(self, request, reference):
        serializer = PayrollPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        cash_session = None
        if data["cash_session_reference"]:
            cash_session = CashSession.objects.filter(reference=data["cash_session_reference"]).first()
            if cash_session is None:
                raise NotFound("Cash session not found.")
        period = pay_payroll_period(
            period=_period(reference), actor=request.user, method=data["method"],
            external_reference=data["external_reference"], cash_session=cash_session,
        )
        log_action(
            actor=request.user, action="PAYROLL_RUN_PAID", instance=period, request=request,
            metadata={"reference": period.reference, "payment_method": period.payment_method,
                      "external_reference": period.external_reference},
        )
        return _private_response(success_response(PayrollPeriodDetailSerializer(_period(reference, detail=True)).data, message="Payroll settlement posted to the immutable ledger."))


@extend_schema(tags=["Admin · Payroll"], summary="View payment history or record a manual salary payment")
class PayrollSalaryPaymentListCreateView(APIView):
    """Private per-employee history and entry point for manual salary records."""

    permission_classes = [HasCapability]
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    required_capabilities = ("payroll.view", "payroll.manage", "payroll.approve")
    require_any_capability = True
    pagination_class = StandardPagination

    def _line(self, reference, line_id):
        period = _period(reference)
        line = PayrollLine.objects.filter(pk=line_id, period_id=period.pk).first()
        if line is None:
            raise NotFound("Payroll employee line not found.")
        return period, line

    def get(self, request, reference, line_id):
        _, line = self._line(reference, line_id)
        queryset = PayrollSalaryPayment.objects.filter(line=line).select_related(
            "line", "recorded_by", "financial_transaction", "cash_session", "reversal_of",
            "reversal_of__recorded_by", "reversal_of__financial_transaction", "reversed_by",
        ).order_by("-payment_date", "-created_at", "-pk")
        return _page(self, request, queryset, PayrollSalaryPaymentSerializer)

    def post(self, request, reference, line_id):
        serializer = PayrollSalaryPaymentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        period, line = self._line(reference, line_id)
        payment, created = record_payroll_salary_payment(
            period=period,
            line=line,
            actor=request.user,
            amount=data["amount"],
            method=data["method"],
            payment_date=data.get("payment_date"),
            external_reference=data["external_reference"],
            evidence_reference=data["evidence_reference"],
            receipt_url=data.get("receipt_url", ""),
            notes=data["notes"],
            idempotency_key=data["idempotency_key"],
            cash_session_reference=data["cash_session_reference"],
        )
        receipt_file = request.FILES.get("receipt")
        if created and receipt_file is not None:
            payment.receipt_file = receipt_file
            payment.save(update_fields=["receipt_file"])
        if created:
            log_action(
                actor=request.user,
                action="PAYROLL_SALARY_PAYMENT_RECORDED",
                instance=payment,
                request=request,
                metadata={
                    "reference": payment.reference,
                    "period_reference": period.reference,
                    "line_id": line.pk,
                    "amount": str(payment.amount),
                    "method": payment.method,
                    "financial_reference": payment.financial_transaction.reference,
                },
            )
        payment = PayrollSalaryPayment.objects.select_related(
            "line", "recorded_by", "financial_transaction", "cash_session", "reversal_of", "reversed_by",
        ).get(pk=payment.pk)
        return _private_response(success_response(
            PayrollSalaryPaymentSerializer(payment).data,
            message="Manual salary payment recorded." if created else "Existing salary payment returned.",
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        ))


@extend_schema(tags=["Admin · Payroll"], summary="Independently authorize a salary-payment reversal")
class PayrollSalaryPaymentReverseView(APIView):
    """Independent, append-only correction of one employee payment record."""

    permission_classes = [HasCapability]
    required_capability = "payroll.approve"

    def post(self, request, reference, line_id, payment_reference):
        period = _period(reference)
        payment = PayrollSalaryPayment.objects.select_related(
            "line", "line__period", "recorded_by", "financial_transaction", "cash_session",
        ).filter(
            reference=payment_reference, line_id=line_id, line__period_id=period.pk,
            reversal_of__isnull=True,
        ).first()
        if payment is None:
            raise NotFound("Original salary payment not found.")
        serializer = PayrollSalaryPaymentReversalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        reversal, created = reverse_payroll_salary_payment(
            payment=payment,
            actor=request.user,
            correction_reason=data["correction_reason"],
            idempotency_key=data["idempotency_key"],
            payment_date=data.get("payment_date"),
            external_reference=data["external_reference"],
            evidence_reference=data["evidence_reference"],
            cash_session_reference=data["cash_session_reference"],
        )
        if created:
            log_action(
                actor=request.user,
                action="PAYROLL_SALARY_PAYMENT_REVERSED",
                instance=reversal,
                request=request,
                metadata={
                    "reference": reversal.reference,
                    "original_reference": payment.reference,
                    "period_reference": period.reference,
                    "line_id": line_id,
                    "amount": str(reversal.amount),
                    "correction_reason": reversal.correction_reason,
                    "financial_reference": reversal.financial_transaction.reference,
                },
            )
        reversal = PayrollSalaryPayment.objects.select_related(
            "line", "recorded_by", "financial_transaction", "cash_session", "reversal_of", "reversed_by",
        ).get(pk=reversal.pk)
        return _private_response(success_response(
            PayrollSalaryPaymentSerializer(reversal).data,
            message="Salary-payment reversal recorded." if created else "Existing salary-payment reversal returned.",
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        ))


class MyPayslipListView(APIView):
    """Authenticated staff see only their own approved payroll lines."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = (
            _payroll_line_queryset().exclude(staff__role=User.Role.ADMIN)
            .filter(
                staff=request.user, staff__is_superuser=False,
                period__status__in=(
                    PayrollPeriod.Status.APPROVED,
                    PayrollPeriod.Status.PARTIALLY_PAID,
                    PayrollPeriod.Status.PAID,
                ),
            )
            .select_related("period")
            .order_by("-period__ends_on", "-pk")
        )
        return _page(self, request, queryset, MyPayslipSerializer)


class PayrollPayslipView(APIView):
    """Private PDF: employee may see their own finalized payslip; payroll roles may audit it."""

    permission_classes = [IsAuthenticated]

    def get(self, request, reference):
        line = (
            PayrollLine.objects.select_related("period", "staff", "compensation")
            .exclude(staff__role=User.Role.ADMIN).filter(period__reference=reference, staff=request.user, staff__is_superuser=False)
            .first()
        )
        if line is None and has_capability(request.user, "payroll.view"):
            line = (
                PayrollLine.objects.select_related("period", "staff", "compensation")
                .exclude(staff__role=User.Role.ADMIN).filter(period__reference=reference, staff_id=request.query_params.get("staff_id"), staff__is_superuser=False)
                .first()
            )
        if line is None:
            raise NotFound("Payslip not found.")
        if line.period.status not in {
            PayrollPeriod.Status.APPROVED, PayrollPeriod.Status.PARTIALLY_PAID, PayrollPeriod.Status.PAID,
        }:
            raise NotFound("Payslip is not available until payroll is approved.")

        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        output = BytesIO()
        document = SimpleDocTemplate(output, pagesize=A4, rightMargin=22 * mm, leftMargin=22 * mm, topMargin=20 * mm, bottomMargin=20 * mm)
        styles = getSampleStyleSheet()
        styles.add(ParagraphStyle(name="JoneTitle", parent=styles["Title"], alignment=TA_CENTER, textColor=colors.HexColor("#373435")))
        elements = [
            Paragraph("J-ONE HOTEL &amp; LODGE", styles["JoneTitle"]),
            Paragraph("CONFIDENTIAL PAYSLIP", styles["Heading2"]),
            Spacer(1, 8 * mm),
        ]
        meta = [
            ["Employee", escape(line.employee_name), "Employee code", escape(line.employee_code or "—")],
            ["Department", escape(line.department or "—"), "Payroll reference", escape(line.period.reference)],
            ["Period", f"{line.period.starts_on:%d %b %Y} – {line.period.ends_on:%d %b %Y}", "Status", line.period.get_status_display()],
        ]
        table = Table(meta, colWidths=[30 * mm, 55 * mm, 32 * mm, 48 * mm])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F3F1EF")),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D8D5D2")),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("PADDING", (0, 0), (-1, -1), 7),
        ]))
        elements.extend([table, Spacer(1, 8 * mm)])
        rows = [["Earnings", "Amount (" + line.currency + ")"], ["Basic pay", str(line.basic_pay)]]
        if line.housing_pay > Decimal("0.00"):
            rows.append(["Housing allowance", str(line.housing_pay)])
        if line.transport_pay > Decimal("0.00"):
            rows.append(["Transport allowance", str(line.transport_pay)])
        if line.overtime_pay > Decimal("0.00"):
            rows.append([f"Overtime ({line.overtime_hours} hours × {line.overtime_rate})", str(line.overtime_pay)])
        for item in line.allowances:
            rows.append(["Allowance · " + escape(str(item.get("label", ""))), str(item.get("amount", "0.00"))])
        if line.bonus > Decimal("0.00"):
            rows.append(["Bonus", str(line.bonus)])
        rows.append(["Gross pay", str(line.gross_pay)])
        rows.append(["Deductions", ""])
        for item in line.deductions:
            rows.append(["Deduction · " + escape(str(item.get("label", ""))), "−" + str(item.get("amount", "0.00"))])
        rows.extend([["Total deductions", str(line.deductions_total)], ["NET PAY", str(line.net_pay)]])
        pay = Table(rows, colWidths=[125 * mm, 40 * mm])
        pay.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#373435")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D8D5D2")),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#E9E4DD")),
            ("PADDING", (0, 0), (-1, -1), 7),
        ]))
        elements.extend([pay, Spacer(1, 6 * mm), Paragraph("This statement is generated from an approved payroll run. Contact Human Resources or Accounts for a documented correction; historical payroll records are not edited.", styles["BodyText"])])
        document.build(elements)
        response = HttpResponse(output.getvalue(), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="payslip-{line.period.reference}-{line.employee_code or line.staff_id}.pdf"'
        response["Cache-Control"] = "private, no-store, max-age=0"
        response["Pragma"] = "no-cache"
        response["X-Content-Type-Options"] = "nosniff"
        return response
