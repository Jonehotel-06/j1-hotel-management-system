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


# ---------------------------------------------------------------------------
# Service-area stores (Bar / Kitchen / Reception)
#
# A POS service area is served by ONE operational store. The hotel runs
# Main Store -> Bar Store -> Bar POS, so a bar sale consumes the Bar Store; the
# same holds for the kitchen. Resolution order:
#
#   1. the persisted ``operational_role`` once a location is explicitly
#      classified with it (``BAR``/``KITCHEN``/``MAIN``);
#   2. the legacy code/name aliases below, which cover the names the hotel
#      already uses ("Bar Store", "Kitchen Store", ...);
#   3. ``HotelSettings``-free settings override ``POS_<AREA>_STORE_CODE`` for a
#      deployment whose store is named something else entirely.
#
# NOTHING here creates, renames or reclassifies a location. Resolution is
# read-only; when no store matches, callers fall back to the configured
# per-product store rather than guessing.
# ---------------------------------------------------------------------------
SERVICE_AREA_STORE_ALIASES = {
    "BAR": ("BAR", "BAR STORE", "BAR-STORE", "BAR_STORE", "BARSTORE", "DRINKS STORE"),
    "RESTAURANT": ("KITCHEN", "KITCHEN STORE", "KITCHEN-STORE", "KITCHEN_STORE", "KITCHENSTORE",
                   "RESTAURANT", "RESTAURANT STORE"),
    "RECEPTION": ("RECEPTION", "RECEPTION STORE", "RECEPTION-STORE", "RECEPTION_STORE", "FRONT DESK"),
    "MINIMART": ("MINIMART", "MINI MART", "MINI-MART", "RECEPTION", "RECEPTION STORE"),
}


def _setting_store_code(area):
    from django.conf import settings

    return str(getattr(settings, f"POS_{area}_STORE_CODE", "") or "").strip()


def service_area_store(area):
    """The single active store that serves a POS service area, or ``None``.

    Returns the first active ``StockLocation`` matching, in order: an explicit
    settings override, the operational-role classification, then the legacy
    code/name aliases. ``None`` means "this hotel has no dedicated store for
    that area", in which case per-product stock configuration stays
    authoritative (no deduction is ever redirected).
    """
    area = str(area or "").strip().upper()
    if not area:
        return None
    aliases = SERVICE_AREA_STORE_ALIASES.get(area, ())
    explicit = _setting_store_code(area)
    if explicit:
        row = StockLocation.objects.filter(code__iexact=explicit, is_active=True).order_by("pk").first()
        if row is not None:
            return row
    role = area if area in StockLocation.OperationalRole.values else ""
    if role:
        row = StockLocation.objects.filter(operational_role=role, is_active=True).order_by("pk").first()
        if row is not None:
            return row
    if area == "RECEPTION":
        row = StockLocation.objects.filter(is_reception=True, is_active=True).order_by("pk").first()
        if row is not None:
            return row
    if not aliases:
        return None
    query = Q()
    for alias in aliases:
        query |= Q(code__iexact=alias) | Q(name__iexact=alias)
    return StockLocation.objects.filter(query, is_active=True).order_by("pk").first()
