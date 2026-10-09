"""Private payroll and payslip endpoints, separated from ordinary staff views."""
from decimal import Decimal
from html import escape
from io import BytesIO

from django.db.models import Count, DecimalField, ExpressionWrapper, F, OuterRef, Prefetch, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.accounts.capabilities import has_capability
from apps.accounts.models import User
from apps.audit.services import log_action
from apps.core.pagination import StandardPagination
from apps.core.permissions import HasCapability
from apps.core.responses import success_response
from apps.finance.models import CashSession

from .models import (
    PayrollEvent, PayrollLine, PayrollPeriod, PayrollSalaryPayment, PayrollStatutoryRuleEvent,
    PayrollStatutoryRuleSet, PayrollTaxIdentity, StaffCompensation, StaffProfile,
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
    PayrollStatutoryRuleReviewSerializer,
    PayrollStatutoryRuleSetCreateSerializer,
    PayrollStatutoryRuleSetDetailSerializer,
    PayrollStatutoryRuleSetSerializer,
    PayrollTaxIdentityCreateSerializer,
    PayrollTaxIdentitySerializer,
    StaffCompensationCreateSerializer,
    StaffCompensationSerializer,
    MyPayslipSerializer,
)
from .services.payroll_service import (
    create_compensation,
    create_payroll_period,
    create_payroll_tax_identity,
    create_statutory_rule_set,
    pay_payroll_period,
    record_payroll_salary_payment,
    reverse_payroll_salary_payment,
    review_payroll_period,
    review_statutory_rule_set,
    submit_payroll_period,
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
    return PayrollLine.objects.select_related("staff", "compensation", "tax_identity").annotate(
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
        "created_by", "reviewed_by", "approved_by", "paid_by", "replaces", "cash_session", "statutory_rules",
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


def _rule_set(reference, *, detail=False):
    queryset = PayrollStatutoryRuleSet.objects.select_related("created_by", "reviewed_by")
    if detail:
        queryset = queryset.prefetch_related(Prefetch("events", queryset=PayrollStatutoryRuleEvent.objects.select_related("actor")))
    value = queryset.filter(reference=reference).first()
    if value is None:
        raise NotFound("Statutory payroll ruleset not found.")
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
        queryset = StaffCompensation.objects.select_related("staff", "created_by", "staff__staff_profile").order_by(
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
            pension_applicable=data["pension_applicable"],
            pension_applicability_note=data["pension_applicability_note"],
            minimum_wage_applicable=data["minimum_wage_applicable"],
            minimum_wage_applicability_note=data["minimum_wage_applicability_note"],
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


@extend_schema(tags=["Admin · Payroll"], summary="View or record confidential staff Tax ID registration")
class PayrollTaxIdentityListCreateView(APIView):
    permission_classes = [HasCapability]
    required_capability = "payroll.manage"
    pagination_class = StandardPagination

    def get(self, request):
        queryset = PayrollTaxIdentity.objects.select_related("staff", "created_by", "staff__staff_profile").order_by(
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
        return _page(self, request, queryset, PayrollTaxIdentitySerializer)

    def post(self, request):
        serializer = PayrollTaxIdentityCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        staff = User.objects.filter(pk=data["staff_id"]).first()
        if staff is None:
            raise NotFound("Staff account not found.")
        identity = create_payroll_tax_identity(
            staff=staff, actor=request.user, tax_id=data["tax_id"],
            effective_from=data["effective_from"], registered_on=data["registered_on"],
            evidence_reference=data["evidence_reference"],
            change_reported_on=data.get("change_reported_on"),
            change_reason=data.get("change_reason", ""), notes=data.get("notes", ""),
        )
        log_action(
            actor=request.user, action="PAYROLL_TAX_ID_RECORDED", instance=identity, request=request,
            metadata={"reference": identity.reference, "staff_id": staff.pk, "effective_from": identity.effective_from.isoformat(), "evidence_reference": identity.evidence_reference, "change_reported_on": identity.change_reported_on.isoformat() if identity.change_reported_on else ""},
        )
        result = PayrollTaxIdentity.objects.select_related("staff", "created_by", "staff__staff_profile").get(pk=identity.pk)
        return _private_response(success_response(
            PayrollTaxIdentitySerializer(result).data,
            message="Confidential Tax ID registration recorded; only its masked form is shown here.",
            status=status.HTTP_201_CREATED,
        ))


@extend_schema(tags=["Admin · Payroll"], summary="View or propose effective-dated statutory payroll rules")
class PayrollStatutoryRuleSetListCreateView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("payroll.view", "payroll.manage", "payroll.approve", "payroll.rules.manage", "payroll.rules.review")
    require_any_capability = True
    pagination_class = StandardPagination

    def get(self, request):
        queryset = PayrollStatutoryRuleSet.objects.select_related("created_by", "reviewed_by").order_by("-effective_from", "-created_at", "-pk")
        if status_filter := request.query_params.get("status"):
            queryset = queryset.filter(status=status_filter.upper())
        page = StandardPagination()
        items = page.paginate_queryset(queryset, request, view=self)
        response = page.get_paginated_response(PayrollStatutoryRuleSetSerializer(items, many=True).data)
        return _private_response(response)

    def post(self, request):
        if not has_capability(request.user, "payroll.rules.manage"):
            raise PermissionDenied("This user cannot propose statutory payroll rules.")
        serializer = PayrollStatutoryRuleSetCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        rule_set = create_statutory_rule_set(actor=request.user, **serializer.validated_data)
        log_action(
            actor=request.user, action="PAYROLL_STATUTORY_RULES_PROPOSED", instance=rule_set, request=request,
            metadata={"reference": rule_set.reference, "jurisdiction": rule_set.jurisdiction, "effective_from": rule_set.effective_from.isoformat(), "source_references": rule_set.source_references},
        )
        return _private_response(success_response(
            PayrollStatutoryRuleSetDetailSerializer(_rule_set(rule_set.reference, detail=True)).data,
            message="Statutory payroll rules proposed for independent review.", status=status.HTTP_201_CREATED,
        ))


@extend_schema(tags=["Admin · Payroll"], summary="View an immutable statutory payroll ruleset and review history")
class PayrollStatutoryRuleSetDetailView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("payroll.view", "payroll.manage", "payroll.approve", "payroll.rules.manage", "payroll.rules.review")
    require_any_capability = True

    def get(self, request, reference):
        return _private_response(success_response(PayrollStatutoryRuleSetDetailSerializer(_rule_set(reference, detail=True)).data))


@extend_schema(tags=["Admin · Payroll"], summary="Independently approve or reject proposed statutory payroll rules")
class PayrollStatutoryRuleSetReviewView(APIView):
    permission_classes = [HasCapability]
    required_capability = "payroll.rules.review"

    def post(self, request, reference):
        rule_set = _rule_set(reference)
        serializer = PayrollStatutoryRuleReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        rule_set = review_statutory_rule_set(
            rule_set=rule_set, reviewer=request.user,
            approved=serializer.validated_data["approved"],
            review_note=serializer.validated_data["review_note"],
        )
        log_action(
            actor=request.user, action="PAYROLL_STATUTORY_RULES_REVIEWED", instance=rule_set, request=request,
            metadata={"reference": rule_set.reference, "status": rule_set.status, "review_note": rule_set.review_note},
        )
        return _private_response(success_response(
            PayrollStatutoryRuleSetDetailSerializer(_rule_set(rule_set.reference, detail=True)).data,
            message="Statutory payroll rule review recorded.",
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
        period = review_payroll_period(
            period=_period(reference), reviewer=request.user, approved=data["approved"], review_note=data["review_note"]
        )
        log_action(
            actor=request.user, action="PAYROLL_RUN_APPROVED" if data["approved"] else "PAYROLL_RUN_REJECTED",
            instance=period, request=request, metadata={"reference": period.reference, "review_note": data["review_note"]},
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
            notes=data["notes"],
            idempotency_key=data["idempotency_key"],
            cash_session_reference=data["cash_session_reference"],
        )
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
            _payroll_line_queryset()
            .filter(
                staff=request.user,
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
            .filter(period__reference=reference, staff=request.user)
            .first()
        )
        if line is None and has_capability(request.user, "payroll.view"):
            line = (
                PayrollLine.objects.select_related("period", "staff", "compensation")
                .filter(period__reference=reference, staff_id=request.query_params.get("staff_id"))
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
        rows.append(["Gross cash pay", str(line.gross_pay)])
        for item in line.benefits_in_kind:
            if Decimal(str(item.get("period_taxable_value", "0.00"))) > Decimal("0.00"):
                rows.append(["Non-cash taxable benefit · " + escape(str(item.get("description", ""))), str(item.get("period_taxable_value", "0.00"))])
        if line.taxable_benefits > Decimal("0.00"):
            rows.append(["Gross taxable emoluments (cash + benefits)", str(line.taxable_gross_pay)])
        rows.append(["Deductions from cash pay", ""])
        for item in line.deductions:
            rows.append(["Deduction · " + escape(str(item.get("label", ""))), "−" + str(item.get("amount", "0.00"))])
        rows.extend([["Total deductions", str(line.deductions_total)], ["NET CASH PAY", str(line.net_pay)]])
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
