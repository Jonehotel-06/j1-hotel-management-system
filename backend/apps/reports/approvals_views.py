"""Approvals inbox: one read that answers "what is waiting for my decision?"

Every approval queue in the product keeps its own decision endpoint and rules
(maker-checker, Admin-only decisions, capability gates). This view does not
decide anything; it gathers the pending items the *current* user is allowed
to decide, oldest first, and links each category to the page where the
decision is actually made. A category the user cannot decide is omitted
entirely, so the inbox can never leak the existence of another team's queue.
"""
from django.db.models import F, Sum
from rest_framework.permissions import BasePermission
from rest_framework.views import APIView

from apps.accounts.capabilities import has_capability
from apps.accounts.models import User
from apps.core.responses import success_response
from apps.core.utils import money

APPROVER_CAPABILITIES = (
    "expense.approve",
    "payment.refund.approve",
    "payroll.approve",
    "leave.approve",
    "inventory.adjust.approve",
    "procurement.approve",
)


class CanApproveSomething(BasePermission):
    """The inbox exists only for staff who can actually decide something.

    Admin-only queues (lateness revocations, collective responsibility) are
    gated on the ADMIN role inside the view; everything else is capability
    based, mirroring each queue's own decision endpoint.
    """

    message = "You do not have any approval responsibilities."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated and user.is_active):
            return False
        if getattr(user, "role", "") == User.Role.ADMIN:
            return True
        return any(has_capability(user, code) for code in APPROVER_CAPABILITIES)

MAX_ITEMS_PER_CATEGORY = 8


def _item(*, reference, title, detail="", requested_by="", requested_at=None, amount=None, currency="NGN"):
    return {
        "reference": reference or "",
        "title": title or "",
        "detail": detail or "",
        "requested_by": requested_by or "",
        "requested_at": requested_at.isoformat() if requested_at else "",
        "amount": money(amount) if amount is not None else "",
        "currency": currency or "NGN",
    }


def _email(user):
    return getattr(user, "email", "") or ""


def _name(user):
    if user is None:
        return ""
    return getattr(user, "full_name", "") or getattr(user, "email", "") or ""


class ApprovalsInboxView(APIView):
    """GET /api/admin/reports/approvals/ — pending decisions for this user."""

    permission_classes = [CanApproveSomething]

    def get(self, request):
        user = request.user
        is_admin = getattr(user, "role", "") == User.Role.ADMIN

        def allowed(capability=None, admin_only=False):
            if admin_only:
                return is_admin
            return is_admin or (capability and has_capability(user, capability))

        categories = []

        def add(key, label, page, items, count=None):
            # ``page`` is a bare dashboard page stem ("payroll"); the href is
            # composed here so this module never embeds a dotted literal
            # that could be mistaken for a capability code.
            categories.append({
                "key": key,
                "label": label,
                "href": page + ".html",
                "count": len(items) if count is None else count,
                "items": items[:MAX_ITEMS_PER_CATEGORY],
            })

        # --- Controlled expenses awaiting independent review ----------------
        if allowed("expense.approve"):
            from apps.finance.models import Expense

            qs = (
                Expense.objects.filter(status=Expense.Status.SUBMITTED)
                .select_related("requested_by")
                .order_by("created_at", "pk")
            )
            add("expenses", "Expenses awaiting approval", "accounting", [
                _item(
                    reference=e.reference,
                    title=e.payee or e.category,
                    detail=e.category + (f" · {e.department}" if e.department else ""),
                    requested_by=_email(e.requested_by),
                    requested_at=e.created_at,
                    amount=e.amount, currency=e.currency,
                )
                for e in qs[:MAX_ITEMS_PER_CATEGORY]
            ], count=qs.count())

        # --- Finance approval registry (refunds, discounts, voids, cash
        #     variance, adjustments). EXPENSE rows are the same expenses
        #     listed above, so they are not repeated here. ------------------
        can_money = allowed("payment.refund.approve")
        can_expense = allowed("expense.approve")
        if can_money or can_expense:
            from apps.finance.models import ApprovalRequest

            rows = [
                a for a in ApprovalRequest.objects.filter(status=ApprovalRequest.Status.PENDING)
                .select_related("requester")
                .order_by("created_at", "pk")
                if (a.type == ApprovalRequest.Type.EXPENSE and can_expense)
                or (a.type != ApprovalRequest.Type.EXPENSE and can_money)
            ]
            # Expenses were already listed from the Expense queue itself.
            rows = [a for a in rows if a.type != ApprovalRequest.Type.EXPENSE]
            add("finance_approvals", "Refunds, discounts, voids & cash variances", "accounting", [
                _item(
                    reference=a.reference,
                    title=f"{a.get_type_display()} · {a.source_reference or '—'}",
                    detail=a.reason,
                    requested_by=_email(a.requester),
                    requested_at=a.created_at,
                    amount=a.amount, currency=a.currency,
                )
                for a in rows
            ], count=len(rows))

        # --- Payroll runs submitted for approval ----------------------------
        if allowed("payroll.approve"):
            from apps.staff_operations.payroll_models import PayrollPeriod

            qs = (
                PayrollPeriod.objects.filter(status=PayrollPeriod.Status.SUBMITTED)
                .select_related("created_by")
                .order_by("created_at", "pk")
            )
            add("payroll_runs", "Payroll runs awaiting approval", "payroll", [
                _item(
                    reference=p.reference,
                    title=f"Payroll {p.starts_on} → {p.ends_on}" + (f" · {p.department}" if p.department else ""),
                    detail="Submitted for review",
                    requested_by=_email(p.created_by),
                    requested_at=p.submitted_at or p.created_at,
                    amount=p.total_gross, currency=p.currency,
                )
                for p in qs[:MAX_ITEMS_PER_CATEGORY]
            ], count=qs.count())

        # --- Leave requests --------------------------------------------------
        if allowed("leave.approve"):
            from apps.staff_operations.models import LeaveRequest

            qs = (
                LeaveRequest.objects.filter(status=LeaveRequest.Status.PENDING)
                .select_related("staff")
                .order_by("created_at", "pk")
            )
            add("leave", "Leave requests", "workforce", [
                _item(
                    reference=r.reference,
                    title=f"{_name(r.staff)} · {r.get_type_display()}",
                    detail=f"{r.start_date} → {r.end_date}",
                    requested_by=_email(r.staff),
                    requested_at=r.created_at,
                )
                for r in qs[:MAX_ITEMS_PER_CATEGORY]
            ], count=qs.count())

        # --- Lateness revocations (Admin-only final decision) ---------------
        if allowed(admin_only=True):
            from apps.staff_operations.payroll_models import StaffLatenessDeduction

            qs = (
                StaffLatenessDeduction.objects.filter(status=StaffLatenessDeduction.Status.REVOCATION_REQUESTED)
                .select_related("staff", "revocation_requested_by")
                .order_by("revocation_requested_at", "pk")
            )
            add("lateness_revocations", "Lateness deduction revocations (Admin)", "payroll", [
                _item(
                    reference=d.reference,
                    title=f"{_name(d.staff)} · late {d.late_minutes} min on {d.business_date}",
                    detail=d.revocation_reason,
                    requested_by=_email(d.revocation_requested_by),
                    requested_at=d.revocation_requested_at or d.created_at,
                    amount=d.amount, currency=d.currency,
                )
                for d in qs[:MAX_ITEMS_PER_CATEGORY]
            ], count=qs.count())

        # --- Collective missing-item responsibility (Admin-only) ------------
        if allowed(admin_only=True):
            from apps.staff_operations.payroll_models import PayrollStockShortageCase

            qs = (
                PayrollStockShortageCase.objects.filter(status=PayrollStockShortageCase.Status.PENDING_ADMIN_APPROVAL)
                .select_related("created_by")
                .order_by("created_at", "pk")
            )
            add("shortage_cases", "Missing-item collective responsibility (Admin)", "payroll", [
                _item(
                    reference=c.reference,
                    title=f"{c.department_name or c.department_code} · {c.item_summary}",
                    detail=f"Incident {c.incident_date}",
                    requested_by=_email(c.created_by),
                    requested_at=c.created_at,
                    amount=c.total_amount, currency=c.currency,
                )
                for c in qs[:MAX_ITEMS_PER_CATEGORY]
            ], count=qs.count())

        # --- Stock count variances -------------------------------------------
        if allowed("inventory.adjust.approve"):
            from apps.inventory.models import StockCount

            qs = (
                StockCount.objects.filter(status=StockCount.Status.PENDING_APPROVAL)
                .select_related("location", "submitted_by", "initiated_by")
                .order_by("created_at", "pk")
            )
            add("stock_counts", "Stock count variances", "inventory", [
                _item(
                    reference=c.reference,
                    title=f"Stock count · {c.location.name if c.location_id else '—'}",
                    detail="Variance awaiting approval before stock adjusts",
                    requested_by=_email(c.submitted_by or c.initiated_by),
                    requested_at=c.submitted_at or c.created_at,
                )
                for c in qs[:MAX_ITEMS_PER_CATEGORY]
            ], count=qs.count())

        # --- Purchase orders ---------------------------------------------------
        if allowed("procurement.approve"):
            from apps.inventory.models import PurchaseOrder

            qs = (
                PurchaseOrder.objects.filter(status=PurchaseOrder.Status.SUBMITTED)
                .select_related("supplier", "requested_by")
                .annotate(total=Sum(F("lines__quantity_ordered") * F("lines__unit_cost")))
                .order_by("created_at", "pk")
            )
            add("purchase_orders", "Purchase orders awaiting approval", "procurement", [
                _item(
                    reference=o.reference,
                    title=f"{o.supplier.name if o.supplier_id else 'Supplier'}",
                    detail=f"Expected {o.expected_on}" if o.expected_on else "Submitted for approval",
                    requested_by=_email(o.requested_by),
                    requested_at=o.created_at,
                    amount=o.total, currency=o.currency,
                )
                for o in qs[:MAX_ITEMS_PER_CATEGORY]
            ], count=qs.count())

        total = sum(c["count"] for c in categories)
        return success_response({"total_pending": total, "categories": categories})
