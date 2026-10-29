"""Read-only guest catalogue: restaurant/bar menu and guest-facing services.

Both endpoints sit behind the verified portal session (cookie or header), so an
expired, checked-out or cross-guest session gets nothing. Responses contain only
customer-facing fields. SKU, barcode, tax settings, recipes, costs, margins and ingredient-level
inventory data never leave the server through these views. The available menu
quantity is a non-reserving snapshot; submission rechecks it under locks.

Prices are the stored POS selling price (``MenuItem.base_price``). The POS never
appends customer tax (owner policy, see ``pos.services.order_service``), so the
displayed price is the price charged.
"""
from django.conf import settings
from django.db.models import Prefetch, Q
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from apps.core.responses import success_response
from apps.guest_services.models import ServiceRequest
from apps.hotel.models import Facility
from apps.pos.models import MenuCategory, MenuItem, MenuModifier

from .authentication import PortalSessionAuthentication
from .permissions import HasPortalSession

# Guest-facing ordering routes: the existing portal request flow, not a new POS path.
MENU_ORDER_CATEGORY = {
    MenuCategory.ServiceArea.RESTAURANT: ServiceRequest.Category.ROOM_SERVICE,
    MenuCategory.ServiceArea.BAR: ServiceRequest.Category.FOOD_BEVERAGE,
}


def _money(value) -> str:
    return f"{value:.2f}"


def _menu_item_payload(item, service_area, stock_summary, *, ordering_enabled=True):
    modifiers = [
        {"name": modifier.name, "price_delta": _money(modifier.price_delta)}
        for modifier in getattr(item, "_portal_modifiers", [])
    ]
    summary = stock_summary or {}
    available = bool(
        item.is_available and item.base_price > 0 and ordering_enabled and summary.get("tracked")
        and not summary.get("unconfigured") and summary.get("available_sufficient_for_one")
    )
    quantity = int(summary.get("available_quantity") or 0) if summary.get("tracked") else 0
    return {
        "id": item.pk,
        "name": item.name,
        "description": item.description or "",
        "price": _money(item.base_price),
        "currency": item.currency or "NGN",
        "is_available": available,
        "availability_label": "Available" if available else "Currently unavailable",
        "available_quantity": min(100, max(quantity, 0)),
        "preparation_minutes": item.preparation_minutes,
        "modifiers": modifiers,
        "service_area": service_area,
        "order_category": MENU_ORDER_CATEGORY.get(service_area),
    }


@extend_schema(tags=["Guest Portal · Catalogue"], summary="Active restaurant and bar menu for a verified guest")
class PortalMenuView(APIView):
    authentication_classes = [PortalSessionAuthentication]
    permission_classes = [HasPortalSession]

    def get(self, request):
        area = (request.query_params.get("area") or "").strip().upper()
        if area and area not in MenuCategory.ServiceArea.values:
            raise ValidationError({"area": "Area must be RESTAURANT or BAR."})
        query = (request.query_params.get("q") or "").strip()[:80]

        categories = MenuCategory.objects.filter(is_active=True)
        if area:
            categories = categories.filter(service_area=area)
        items = MenuItem.objects.filter(is_active=True).prefetch_related(
            Prefetch("modifiers", queryset=MenuModifier.objects.filter(is_active=True).order_by("sort_order", "name"),
                     to_attr="_portal_modifiers"),
        )
        if query:
            items = items.filter(Q(name__icontains=query) | Q(description__icontains=query))
        categories = categories.prefetch_related(Prefetch("items", queryset=items, to_attr="_portal_items")).order_by(
            "sort_order", "name"
        )

        categories = list(categories)
        from apps.inventory.services.inventory_service import pos_stock_summaries

        all_ids = [item.pk for category in categories for item in getattr(category, "_portal_items", [])]
        summaries = pos_stock_summaries(all_ids, strict=True)
        enabled = set(getattr(settings, "PORTAL_ENABLED_SERVICE_CATEGORIES", None) or ServiceRequest.Category.values)
        payload = []
        for category in categories:
            visible = [
                _menu_item_payload(item, category.service_area, summaries.get(item.pk),
                                   ordering_enabled=MENU_ORDER_CATEGORY.get(category.service_area) in enabled)
                for item in getattr(category, "_portal_items", [])
            ]
            if not visible:
                continue
            payload.append({
                "id": category.pk,
                "name": category.name,
                "slug": category.slug,
                "service_area": category.service_area,
                "description": category.description or "",
                "items": visible,
            })
        return success_response({
            "categories": payload,
            "currency": "NGN",
            "filters": {"area": area or None, "q": query or None},
        })


@extend_schema(tags=["Guest Portal · Catalogue"], summary="Guest-facing services a verified guest can view or request")
class PortalServicesView(APIView):
    """Services listed for guests.

    * ``request`` entries are the request categories already enabled for the portal
      (``PORTAL_ENABLED_SERVICE_CATEGORIES``). Acting on one opens the existing
      portal request flow with that category selected; it routes to the same
      department as before.
    * ``facility`` entries are the public hotel facilities. The hotel has not
      configured prices, hours or turnaround for them, so none are shown or invented.
    """

    authentication_classes = [PortalSessionAuthentication]
    permission_classes = [HasPortalSession]

    def get(self, request):
        from apps.guest_services.services.request_service import _DEFAULT_TEAM

        enabled = set(getattr(settings, "PORTAL_ENABLED_SERVICE_CATEGORIES", None) or ServiceRequest.Category.values)
        requests_catalogue = []
        for value, label in ServiceRequest.Category.choices:
            if value not in enabled:
                continue
            team = _DEFAULT_TEAM.get(value, ServiceRequest.OwnerTeam.FRONT_DESK)
            requests_catalogue.append({
                "kind": "request",
                "key": f"request:{value}",
                "name": label,
                "category": value,
                "description": "",
                "pricing_method": "not_listed",
                "price": None,
                "availability": "Ask the front desk for availability",
                "hours": None,
                "turnaround": None,
                "instructions": None,
                "action": "request_service",
                "action_label": "Request Service",
                "department": team,
                "department_label": ServiceRequest.OwnerTeam(team).label,
            })

        facilities = []
        for facility in Facility.objects.filter(is_active=True).order_by("display_order", "name"):
            facilities.append({
                "kind": "facility",
                "key": f"facility:{facility.pk}",
                "name": facility.name,
                "category": "FACILITY",
                "description": facility.description or "",
                "pricing_method": "not_listed",
                "price": None,
                "availability": "Open during hotel operating hours",
                "hours": None,
                "turnaround": None,
                "instructions": None,
                "action": "view_details",
                "action_label": "View Details",
                "department": None,
                "department_label": None,
            })
        return success_response(
            {"services": facilities + requests_catalogue},
            message="Guest services.",
            status=status.HTTP_200_OK,
        )
