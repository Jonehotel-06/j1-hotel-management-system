# apps/core/approvals.py
"""The single administrative-override policy for maker-checker approvals.

WHY THIS MODULE EXISTS
======================
The J-ONE approval architecture is maker-checker: the person who raises a
controlled transaction must not be the person who authorises it. That rule is
correct and stays in force for every ordinary role.

It produced two real problems in practice:

1. **The hotel's owner/administrator was locked out of their own system.** With
   no second administrator on payroll, a purchase order, expense, payroll run
   or stock-count variance raised by the administrator could never be approved
   by anybody, so the business simply stopped at that screen.

2. **The exemption was applied inconsistently, in the wrong layer.** An ad-hoc
   ``actor.role != "ADMIN"`` appeared inside one service (purchase orders) and
   nowhere else. It was invisible to reviewers, it was not configurable, it
   recorded no evidence that an override had happened, and it compared a raw
   role string instead of asking the capability framework.

This module replaces that with ONE centrally enforced policy:

* :func:`require_independent_approver` is the only place a workflow asks
  "may this actor authorise a transaction they raised?". Every controlled
  approval calls it. There is no per-service ``if admin`` left anywhere.
* The exemption is granted to the **administrator role only**. A manager,
  general manager, accountant or storekeeper who happens to hold the approval
  capability is still refused when they are the maker.
* The exemption is **configuration-controlled** (``ALLOW_ADMIN_APPROVAL_OVERRIDE``),
  so a future owner can turn it off without touching code.
* When the exemption is used, it is **recorded as append-only evidence** on the
  workflow's own audit trail, marked with the administrator's identity and an
  explicit override reason. Nothing is hidden and no other control is weakened:
  the transaction is still validated, still posted once, and still fully
  traceable to both the creator and the approving administrator.

WHAT IT DELIBERATELY DOES NOT DO
================================
The exemption relaxes exactly one rule — "the approver must be a different
person from the maker". It never:

* grants a capability the actor does not hold (an administrator only reaches
  this code after the workflow's own capability check);
* skips validation of the underlying transaction;
* suppresses or rewrites audit history;
* allows a second approval, payment or stock movement for the same source key.
"""
from __future__ import annotations

from django.conf import settings
from django.core.exceptions import PermissionDenied

from apps.audit.services import log_action

#: Set ``ALLOW_ADMIN_APPROVAL_OVERRIDE=False`` to require two distinct people
#: for every controlled approval, including the administrator. Defaults to True
#: because a single-owner hotel would otherwise be unable to operate.
SETTING_NAME = "ALLOW_ADMIN_APPROVAL_OVERRIDE"

#: Recorded on audit evidence so an override can be filtered and reported on.
OVERRIDE_MARKER = "ADMIN_OVERRIDE"

#: Appended to the human-readable review note for the same reason.
OVERRIDE_NOTE = "Administrator override: the creator and the approver are the same account."


def administrator_role() -> str:
    """The role code that carries the administrative override."""
    from apps.accounts.models import User

    return User.Role.ADMIN


def is_administrator(user) -> bool:
    """True when ``user`` is an active administrator account.

    Compares the persisted role, NOT a JWT claim, a permission label or a
    frontend flag — a forged request cannot make an ordinary account look like
    an administrator here.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if not getattr(user, "is_active", False):
        return False
    return getattr(user, "role", None) == administrator_role()


def admin_override_enabled() -> bool:
    """Whether the configured policy permits an administrative override."""
    return bool(getattr(settings, SETTING_NAME, True))


def may_override_separation(*, actor) -> bool:
    """Whether this actor is exempt from the separate-approver requirement."""
    return admin_override_enabled() and is_administrator(actor)


def require_independent_approver(*, requester, actor, workflow: str) -> dict | None:
    """Enforce maker-checker for one controlled approval.

    Returns ``None`` on the normal path (the actor is a different person from
    the requester). Returns an override-evidence dict when an administrator
    authorised their own transaction, which the caller MUST attach to its own
    append-only event so the override is auditable.

    Raises :class:`~django.core.exceptions.PermissionDenied` when the actor is
    the requester and holds no override.
    """
    requester_id = getattr(requester, "pk", None)
    actor_id = getattr(actor, "pk", None)
    if requester_id is None or actor_id is None or requester_id != actor_id:
        return None
    if not may_override_separation(actor=actor):
        raise PermissionDenied(
            f"A requester cannot approve their own {workflow}. "
            "A different authorising account is required."
        )
    evidence = {
        "marker": OVERRIDE_MARKER,
        "workflow": workflow,
        "override_actor_id": actor_id,
        "override_actor_role": getattr(actor, "role", ""),
        "requester_id": requester_id,
        "reason": OVERRIDE_NOTE,
        "policy_setting": SETTING_NAME,
    }
    log_action(
        actor=actor,
        action=OVERRIDE_MARKER,
        instance=None,
        summary=f"Administrator override on {workflow} (creator = approver)",
        metadata=evidence,
    )
    return evidence


def override_note(note: str, evidence: dict | None) -> str:
    """Fold the override disclosure into a stored review note."""
    note = (note or "").strip()
    if not evidence:
        return note
    return f"{note} {OVERRIDE_NOTE}".strip() if note else OVERRIDE_NOTE


def override_event_details(details: dict | None, evidence: dict | None) -> dict:
    """Merge override evidence into an append-only event payload."""
    payload = dict(details or {})
    if evidence:
        payload["admin_override"] = evidence
    return payload
