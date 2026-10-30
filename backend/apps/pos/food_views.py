"""Scoped kitchen food controls, reusing existing kitchen/menu capabilities."""
from django.db.models import Q
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from apps.core.pagination import StandardPagination
from apps.core.permissions import HasCapability
from apps.core.responses import success_response
from .models import FoodAvailabilityEvent, MenuItem
from .services.food_service import change_food, food_payload


class FoodChangeSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["READY", "NOT_AVAILABLE", "ENABLE_PORTIONS", "DISABLE_PORTIONS", "REPLENISH", "WASTE"])
    quantity = serializers.IntegerField(min_value=0, max_value=1000000, default=0)
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    idempotency_key = serializers.CharField(max_length=100)

    def validate(self, attrs):
        if attrs["action"] in {"REPLENISH", "WASTE"} and not attrs["quantity"]:
            raise serializers.ValidationError({"quantity": "A positive prepared quantity is required."})
        if attrs["action"] == "WASTE" and not attrs["reason"].strip():
            raise serializers.ValidationError({"reason": "A wastage reason is required."})
        if attrs["action"] not in {"REPLENISH", "WASTE"} and attrs["quantity"]:
            raise serializers.ValidationError({"quantity": "Quantity applies only to replenishment or wastage."})
        return attrs


class KitchenFoodView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("kitchen.queue.view", "kitchen.ticket.manage", "pos.menu.manage", "restaurant.menu.manage")
    require_any_capability = True

    def get(self, request):
        items = MenuItem.objects.filter(is_active=True, category__is_active=True, category__service_area="RESTAURANT").order_by("name", "pk")
        query = str(request.query_params.get("q") or "")[:80]
        if query:
            items = items.filter(Q(name__icontains=query) | Q(sku__icontains=query))
        paginator = StandardPagination()
        page = paginator.paginate_queryset(items, request, view=self)
        return paginator.get_paginated_response([food_payload(item) for item in page])


class KitchenFoodDetailView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("kitchen.ticket.manage", "pos.menu.manage", "restaurant.menu.manage")
    require_any_capability = True

    def post(self, request, pk):
        serializer = FoodChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            item, changed = change_food(item_id=pk, actor=request.user, **serializer.validated_data)
        except MenuItem.DoesNotExist:
            raise NotFound("Food item not found.")
        return success_response({**food_payload(item), "changed": changed})

    def get(self, request, pk):
        if not MenuItem.objects.filter(pk=pk, category__service_area="RESTAURANT").exists():
            raise NotFound("Food item not found.")
        events = FoodAvailabilityEvent.objects.filter(menu_item_id=pk).order_by("-pk")
        paginator = StandardPagination()
        page = paginator.paginate_queryset(events, request, view=self)
        return paginator.get_paginated_response([{
            "id": event.pk, "actor_id": event.actor_id, "created_at": event.created_at,
            "action": event.action, "quantity_delta": event.quantity_delta,
            "portions_after": event.portions_after, "ready_after": event.ready_after,
            "tracking_after": event.tracking_after, "reason": event.reason, "details": event.details,
        } for event in page])
