"""Operational store roles and read-only classification of stock issue history."""
import re

from django.db.models import Q

from ..models import StockDepartment, StockLocation, StockMovement

KITCHEN_ISSUE = "KITCHEN_ISSUE"
MANUAL_STOCK_OUT = "MANUAL_STOCK_OUT"
LEGACY_KITCHEN = "LEGACY_KITCHEN"
KITCHEN_DEPARTMENTS = (StockDepartment.KITCHEN, StockDepartment.KITCHEN_ASSISTANT)
_LOCATION_ALIASES = {
    StockLocation.OperationalRole.MAIN: ("MAIN", "MAIN STORE", "MAIN-STORE", "MAIN_STORE", "MAINSTORE"),
    StockLocation.OperationalRole.KITCHEN: ("KITCHEN", "KITCHEN STORE", "KITCHEN-STORE", "KITCHEN_STORE", "KITCHENSTORE"),
}


def location_role(location):
    return location.operational_role or legacy_location_role(location)


def legacy_location_role(location):
    for role, aliases in _LOCATION_ALIASES.items():
        if location.code.upper() in aliases or location.name.upper() in aliases:
            return role
    return StockLocation.OperationalRole.GENERAL


def locations_with_role(role):
    legacy = Q()
    for alias in _LOCATION_ALIASES[role]:
        legacy |= Q(code__iexact=alias) | Q(name__iexact=alias)
    return Q(operational_role=role) | (Q(operational_role="") & legacy)


def kitchen_destination(text):
    # The old UI and API also accepted an unstructured destination. Catch a
    # named kitchen handover there; don't interpret arbitrary audit notes.
    return bool(re.search(r"\bkitchen(?:store)?\b", str(text or "").replace("_", " "), re.I))


def _metadata_equals(field, value, lookup="exact"):
    # On both SQLite and MySQL, NOT over a missing JSON path is SQL UNKNOWN,
    # not TRUE. Guard existence before using JSON predicates inside negation.
    return Q(**{f"metadata__{field}__isnull": False, f"metadata__{field}__{lookup}": value})


def kitchen_issue_query():
    return _metadata_equals("mode", KITCHEN_ISSUE) | Q(
        type=StockMovement.Type.ISSUE, preparation_batch__isnull=False,
        department__in=KITCHEN_DEPARTMENTS,
    )


def legacy_kitchen_query():
    # Historical discrepancies are shown separately, not re-written. The old
    # transfer stored the destination name in its immutable audit metadata.
    return (
        Q(type=StockMovement.Type.ISSUE, department__in=KITCHEN_DEPARTMENTS,
          source_key__startswith="stock-issue:")
        | (Q(source_reference__startswith="MSO-") & (
            Q(department__in=KITCHEN_DEPARTMENTS) | _metadata_equals("reason", "KITCHEN")
            | _metadata_equals("destination", "kitchen", "icontains")
            | _metadata_equals("destination_location_name", "kitchen", "icontains")
        ))
    ) & ~kitchen_issue_query()


def manual_stock_out_query():
    return (_metadata_equals("mode", MANUAL_STOCK_OUT) | Q(source_reference__startswith="MSO-")) & ~(
        kitchen_issue_query() | legacy_kitchen_query()
    )


def movement_workflow(movement):
    meta = movement.metadata or {}
    if meta.get("mode") == KITCHEN_ISSUE or (
        movement.preparation_batch_id and movement.department in KITCHEN_DEPARTMENTS
    ):
        return KITCHEN_ISSUE
    if movement.type == StockMovement.Type.ISSUE and movement.source_key.startswith("stock-issue:") and movement.department in KITCHEN_DEPARTMENTS:
        return LEGACY_KITCHEN
    if movement.source_reference.startswith("MSO-") and (
        movement.department in KITCHEN_DEPARTMENTS or meta.get("reason") == "KITCHEN"
        or "kitchen" in str(meta.get("destination") or "").lower()
        or "kitchen" in str(meta.get("destination_location_name") or "").lower()
    ):
        return LEGACY_KITCHEN
    if meta.get("mode") == MANUAL_STOCK_OUT or movement.source_reference.startswith("MSO-"):
        return MANUAL_STOCK_OUT
    return ""
