# apps/core/approval_audit.py
"""Uniform approval-decision audit evidence (Batch 07).

The five controlled approval workflows — leave, stock-count variances,
purchase orders, payroll runs and expenses — each keep their own decision
endpoint, validation rules and append-only history (owned by their respective
batches). What Batch 07 adds is ONE uniform evidence shape on the audit-log
entry every decision already writes, so a reviewer can answer, for any
approval, without opening five different modules:

* request/transaction reference;
* approval category;
* initiating staff member;
* approving (deciding) staff member;
* previous status;
* new status;
* date and time of the decision;
* comments / approval notes where the workflow supports them.

This module only builds the metadata dict. It changes no workflow logic, no
status transition and no permission check: callers merge the result into the
``metadata`` of the ``log_action`` call they already make, keeping every key
they already wrote. Existing history rows are untouched.
"""
from __future__ import annotations

from django.utils import timezone


class ApprovalCategory:
    """Stable category codes for the five Batch 07 approval workflows."""

    LEAVE = "leave"
    STOCK_COUNT = "stock_count"
    PURCHASE_ORDER = "purchase_order"
    PAYROLL = "payroll"
    EXPENSE = "expense"


def actor_ref(user) -> dict:
    """Small, stable identity reference for a staff account in audit metadata."""
    if user is None:
        return {"id": None, "email": ""}
    return {
        "id": getattr(user, "pk", None),
        "email": getattr(user, "email", "") or "",
    }


def approval_decision_metadata(
    *,
    category: str,
    reference: str,
    initiated_by=None,
    decided_by=None,
    previous_status: str = "",
    new_status: str = "",
    note: str = "",
) -> dict:
    """Build the uniform Batch 07 evidence block for one approval decision.

    ``initiated_by`` is the staff member who raised the request (the maker);
    ``decided_by`` is the staff member who approved or rejected it (the
    checker). ``previous_status`` must be read BEFORE the service call applies
    the transition; ``new_status`` is read from the returned record.
    """
    return {
        "approval_category": str(category or ""),
        "reference": str(reference or ""),
        "initiated_by": actor_ref(initiated_by),
        "decided_by": actor_ref(decided_by),
        "previous_status": str(previous_status or ""),
        "new_status": str(new_status or ""),
        "decided_at": timezone.now().isoformat(),
        "note": str(note or ""),
    }
