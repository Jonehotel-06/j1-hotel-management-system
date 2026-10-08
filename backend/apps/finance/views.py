"""Bounded, read-first finance operations API."""
from django.db.models import Count, Prefetch, Q
from django.utils.dateparse import parse_date
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.views import APIView

from apps.accounts.capabilities import has_capability
from apps.audit.services import log_action
from apps.core.pagination import StandardPagination
from apps.core.permissions import HasCapability
from apps.core.responses import success_response

from .models import ApprovalRequest, CashSession, Expense, ExpenseEvent, FinancialControlPolicy, FinancialLine, FinancialTransaction, Folio, FolioPosting
from .serializers import (
    ApprovalRequestSerializer,
    ApprovalReviewSerializer,
    CashSessionSerializer,
    ExpenseCreateSerializer,
    ExpenseDetailSerializer,
    ExpenseListSerializer,
    ExpenseReviewSerializer,
    FinancialControlPolicySerializer,
    FinancialTransactionDetailSerializer,
    FinancialTransactionSerializer,
    FolioPostingSerializer,
    FolioSerializer,
)
from .services.approval_service import review_approval
from .services.cash_session_service import review_cash_variance
from .services.expense_service import create_expense, post_expense, review_expense, submit_expense
from .services.folio_service import folio_balance_queryset
from .services.reporting_service import financial_summary


def _paginate(view, request, queryset, serializer_class):
    paginator = StandardPagination()
    page = paginator.paginate_queryset(queryset, request, view=view)
    return paginator.get_paginated_response(serializer_class(page, many=True).data)


def _parse_query_date(value, name):
    if not value:
        return None
    parsed = parse_date(value)
    if parsed is None:
        from rest_framework.exceptions import ValidationError
        raise ValidationError({name: "Use ISO date format YYYY-MM-DD."})
    return parsed


@extend_schema(tags=["Admin · Finance"], summary="List guest folios with computed balances")
class FolioListView(APIView):
    permission_classes = [HasCapability]
    required_capability = "folio.view"

    def get(self, request):
        queryset = folio_balance_queryset(
            Folio.objects.select_related("guest", "booking", "stay").order_by("-opened_at", "-pk")
        )
        if status_value := request.query_params.get("status"):
            queryset = queryset.filter(status=status_value)
        if booking_reference := request.query_params.get("booking"):
            queryset = queryset.filter(booking__booking_reference=booking_reference)
        if stay_reference := request.query_params.get("stay"):
            queryset = queryset.filter(stay__reference=stay_reference)
        if search := request.query_params.get("search"):
            queryset = queryset.filter(
                Q(reference__icontains=search)
                | Q(guest__first_name__icontains=search)
                | Q(guest__last_name__icontains=search)
                | Q(guest__email__icontains=search)
            )
        return _paginate(self, request, queryset, FolioSerializer)


@extend_schema(tags=["Admin · Finance"], summary="Get one folio with a computed balance")
class FolioDetailView(APIView):
    permission_classes = [HasCapability]
    required_capability = "folio.view"

    def get(self, request, reference):
        folio = folio_balance_queryset(
            Folio.objects.select_related("guest", "booking", "stay").filter(reference=reference)
        ).first()
        if folio is None:
            raise NotFound("Folio not found.")
        return success_response(FolioSerializer(folio).data)


@extend_schema(tags=["Admin · Finance"], summary="List immutable statement postings for a folio")
class FolioPostingListView(APIView):
    permission_classes = [HasCapability]
    required_capability = "folio.view"

    def get(self, request, reference):
        if not Folio.objects.filter(reference=reference).exists():
            raise NotFound("Folio not found.")
        queryset = (
            FolioPosting.objects.filter(folio__reference=reference)
            .select_related("transaction", "line")
            .order_by("-business_date", "-created_at", "-pk")
        )
        return _paginate(self, request, queryset, FolioPostingSerializer)


@extend_schema(tags=["Admin · Finance"], summary="List immutable financial transactions")
class FinancialTransactionListView(APIView):
    permission_classes = [HasCapability]
    required_capability = "reports.financial.view"

    def get(self, request):
        queryset = (
            FinancialTransaction.objects.select_related("initiated_by", "approved_by")
            .annotate(line_count=Count("lines"))
            .order_by("-business_date", "-created_at", "-pk")
        )
        if transaction_type := request.query_params.get("type"):
            queryset = queryset.filter(type=transaction_type)
        if status_value := request.query_params.get("status"):
            queryset = queryset.filter(status=status_value)
        if reference := request.query_params.get("reference"):
            queryset = queryset.filter(Q(reference__icontains=reference) | Q(source_reference__icontains=reference))
        start = _parse_query_date(request.query_params.get("start"), "start")
        end = _parse_query_date(request.query_params.get("end"), "end")
        if start:
            queryset = queryset.filter(business_date__gte=start)
        if end:
            queryset = queryset.filter(business_date__lte=end)
        return _paginate(self, request, queryset, FinancialTransactionSerializer)


@extend_schema(tags=["Admin · Finance"], summary="Summarize posted ledger evidence by business date")
class FinancialSummaryReportView(APIView):
    permission_classes = [HasCapability]
    required_capability = "reports.financial.view"

    def get(self, request):
        data = financial_summary(
            start_date=_parse_query_date(request.query_params.get("start"), "start"),
            end_date=_parse_query_date(request.query_params.get("end"), "end"),
            currency=request.query_params.get("currency", "NGN"),
        )
        return success_response(data)


@extend_schema(tags=["Admin · Finance"], summary="Get one immutable financial transaction")
class FinancialTransactionDetailView(APIView):
    permission_classes = [HasCapability]
    required_capability = "reports.financial.view"

    def get(self, request, reference):
        financial_transaction = (
            FinancialTransaction.objects.select_related("initiated_by", "approved_by")
            .prefetch_related(Prefetch("lines", queryset=FinancialLine.objects.select_related("folio")))
            .annotate(line_count=Count("lines"))
            .filter(reference=reference)
            .first()
        )
        if financial_transaction is None:
            raise NotFound("Financial transaction not found.")
        return success_response(FinancialTransactionDetailSerializer(financial_transaction).data)


@extend_schema(tags=["Admin · Finance"], summary="List permitted cashier sessions")
class CashSessionListView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("cash_session.open", "cash_session.close", "reports.financial.view")
    require_any_capability = True

    def get(self, request):
        queryset = CashSession.objects.select_related("cashier", "opened_by", "closed_by").order_by("-opened_at", "-pk")
        # Financial-report viewers can reconcile every drawer. Other cashier
        # roles only see their own sessions, avoiding cross-cashier exposure.
        if not has_capability(request.user, "reports.financial.view"):
            queryset = queryset.filter(cashier=request.user)
        if status_value := request.query_params.get("status"):
            queryset = queryset.filter(status=status_value)
        return _paginate(self, request, queryset, CashSessionSerializer)


@extend_schema(tags=["Admin · Finance"], summary="List sensitive-money approval requests")
class ApprovalRequestListView(APIView):
    permission_classes = [HasCapability]
    required_capability = "payment.refund.approve"

    def get(self, request):
        queryset = ApprovalRequest.objects.select_related("requester", "reviewer", "financial_transaction").order_by("-created_at", "-pk")
        if status_value := request.query_params.get("status"):
            queryset = queryset.filter(status=status_value)
        if request_type := request.query_params.get("type"):
            queryset = queryset.filter(type=request_type)
        return _paginate(self, request, queryset, ApprovalRequestSerializer)


@extend_schema(tags=["Admin · Finance"], summary="Review a sensitive-money approval request")
class ApprovalReviewView(APIView):
    permission_classes = [HasCapability]
    required_capability = "payment.refund.approve"

    def post(self, request, reference):
        serializer = ApprovalReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        approval = ApprovalRequest.objects.filter(reference=reference).first()
        if approval is None:
            raise NotFound("Approval request not found.")
        if approval.type == ApprovalRequest.Type.CASH_VARIANCE:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"reference": "Use the cash-session variance-review endpoint for this approval."})
        if approval.type == ApprovalRequest.Type.EXPENSE:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"reference": "Use the expense review endpoint for this approval."})
        approval = review_approval(approval=approval, reviewer=request.user, **serializer.validated_data)
        return success_response(ApprovalRequestSerializer(approval).data, message="Approval review recorded.")


@extend_schema(tags=["Admin · Finance"], summary="Review a material cashier variance and complete the drawer close")
class CashVarianceReviewView(APIView):
    permission_classes = [HasCapability]
    required_capability = "payment.refund.approve"

    def post(self, request, reference):
        serializer = ApprovalReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        cash_session = CashSession.objects.filter(reference=reference).first()
        if cash_session is None:
            raise NotFound("Cash session not found.")
        cash_session = review_cash_variance(
            cash_session=cash_session,
            reviewer=request.user,
            **serializer.validated_data,
        )
        return success_response(CashSessionSerializer(cash_session).data, message="Cash variance review recorded.")


def _expense_queryset(*, detail=False):
    queryset = Expense.objects.select_related(
        "requested_by",
        "approved_by",
        "posted_by",
        "approval_request",
        "financial_transaction",
        "cash_session",
    )
    if detail:
        queryset = queryset.prefetch_related(
            Prefetch(
                "events",
                queryset=ExpenseEvent.objects.select_related("actor").order_by("created_at", "pk"),
            )
        )
    return queryset


def _expense_or_404(request, reference, *, detail=False):
    expense = _expense_queryset(detail=detail).filter(reference=reference).first()
    if expense is None:
        raise NotFound("Expense not found.")
    if not (
        has_capability(request.user, "reports.financial.view")
        or has_capability(request.user, "expense.approve")
        or expense.requested_by_id == request.user.pk
    ):
        raise NotFound("Expense not found.")
    return expense


@extend_schema(tags=["Admin · Finance"], summary="List or create controlled expense drafts")
class ExpenseListCreateView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("expense.manage", "expense.approve")
    require_any_capability = True

    def get(self, request):
        queryset = _expense_queryset()
        if not (
            has_capability(request.user, "reports.financial.view")
            or has_capability(request.user, "expense.approve")
        ):
            queryset = queryset.filter(requested_by=request.user)
        if status_value := request.query_params.get("status"):
            queryset = queryset.filter(status=status_value)
        if category := request.query_params.get("category"):
            queryset = queryset.filter(category__iexact=category)
        start = _parse_query_date(request.query_params.get("start"), "start")
        end = _parse_query_date(request.query_params.get("end"), "end")
        if start:
            queryset = queryset.filter(incurred_on__gte=start)
        if end:
            queryset = queryset.filter(incurred_on__lte=end)
        return _paginate(
            self,
            request,
            queryset.order_by("-incurred_on", "-created_at", "-pk"),
            ExpenseListSerializer,
        )

    def post(self, request):
        if not has_capability(request.user, "expense.manage"):
            raise PermissionDenied("This user cannot create expense records.")
        serializer = ExpenseCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        cash_session = None
        if reference := data.get("cash_session_reference"):
            cash_session = CashSession.objects.filter(reference=reference).first()
            if cash_session is None:
                raise NotFound("Cash session not found.")
        key = f"expense:{request.user.pk}:{data['idempotency_key']}"
        expense, created = create_expense(
            actor=request.user,
            category=data["category"],
            amount=data["amount"],
            incurred_on=data.get("incurred_on"),
            payment_method=data["payment_method"],
            accounting_class=data["accounting_class"],
            currency=data.get("currency", "NGN"),
            payee=data.get("payee", ""),
            supplier_reference=data.get("supplier_reference", ""),
            description=data.get("description", ""),
            receipt_url=data.get("receipt_url", ""),
            notes=data.get("notes", ""),
            idempotency_key=key,
            cash_session=cash_session,
        )
        if created:
            log_action(
                actor=request.user,
                action="EXPENSE_DRAFT_CREATED",
                instance=expense,
                request=request,
                metadata={"reference": expense.reference, "amount": str(expense.amount)},
            )
        detail = ExpenseDetailSerializer(
            _expense_or_404(request, expense.reference, detail=True)
        ).data
        return success_response(
            detail,
            message="Expense draft created." if created else "Existing expense draft returned.",
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class ExpenseDetailView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("expense.manage", "expense.approve")
    require_any_capability = True

    def get(self, request, reference):
        return success_response(
            ExpenseDetailSerializer(_expense_or_404(request, reference, detail=True)).data
        )


class ExpenseSubmitView(APIView):
    permission_classes = [HasCapability]
    required_capability = "expense.manage"

    def post(self, request, reference):
        expense = submit_expense(
            expense=_expense_or_404(request, reference),
            actor=request.user,
        )
        log_action(
            actor=request.user,
            action="EXPENSE_SUBMITTED",
            instance=expense,
            request=request,
            metadata={"reference": expense.reference, "status": expense.status},
        )
        message = (
            "Expense submitted for independent approval."
            if expense.status == Expense.Status.SUBMITTED
            else "Expense approved under the active policy."
        )
        return success_response(
            ExpenseDetailSerializer(_expense_or_404(request, expense.reference, detail=True)).data,
            message=message,
        )


class ExpenseReviewView(APIView):
    permission_classes = [HasCapability]
    required_capability = "expense.approve"

    def post(self, request, reference):
        serializer = ExpenseReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        expense = _expense_queryset().filter(reference=reference).first()
        if expense is None:
            raise NotFound("Expense not found.")
        expense = review_expense(
            expense=expense,
            reviewer=request.user,
            **serializer.validated_data,
        )
        log_action(
            actor=request.user,
            action="EXPENSE_REVIEWED",
            instance=expense,
            request=request,
            metadata={"reference": expense.reference, "status": expense.status},
        )
        return success_response(
            ExpenseDetailSerializer(_expense_or_404(request, expense.reference, detail=True)).data,
            message="Expense approved."
            if expense.status == Expense.Status.APPROVED
            else "Expense rejected.",
        )


class ExpensePostView(APIView):
    permission_classes = [HasCapability]
    required_capability = "expense.manage"

    def post(self, request, reference):
        expense, created = post_expense(
            expense=_expense_or_404(request, reference),
            actor=request.user,
        )
        if created:
            log_action(
                actor=request.user,
                action="EXPENSE_POSTED",
                instance=expense,
                request=request,
                metadata={
                    "reference": expense.reference,
                    "financial_transaction": expense.financial_transaction.reference,
                },
            )
        return success_response(
            ExpenseDetailSerializer(_expense_or_404(request, expense.reference, detail=True)).data,
            message="Expense posted to the immutable ledger."
            if created
            else "Expense was already posted.",
        )


@extend_schema(tags=["Admin · Finance"], summary="List effective-dated finance control policies")
class FinancialControlPolicyListCreateView(APIView):
    permission_classes = [HasCapability]
    required_capability = "finance.controls.manage"

    def get(self, request):
        queryset = FinancialControlPolicy.objects.all().order_by("-effective_from", "-pk")
        return _paginate(self, request, queryset, FinancialControlPolicySerializer)

    def post(self, request):
        serializer = FinancialControlPolicySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        policy = serializer.save()
        log_action(
            actor=request.user,
            action="FINANCE_CONTROL_POLICY_CREATED",
            instance=policy,
            metadata={"effective_from": policy.effective_from.isoformat(), "policy_name": policy.name},
            request=request,
            summary=f"Finance control policy {policy.name} created",
        )
        return success_response(
            FinancialControlPolicySerializer(policy).data,
            message="Effective-dated finance control policy created.",
            status=status.HTTP_201_CREATED,
        )
