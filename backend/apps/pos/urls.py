from django.urls import path

from . import views, food_views

app_name = "pos"

urlpatterns = [
    path("kitchen-food/", food_views.KitchenFoodView.as_view(), name="kitchen-food"),
    path("kitchen-food/<int:pk>/", food_views.KitchenFoodDetailView.as_view(), name="kitchen-food-detail"),
    path("menu/", views.PosMenuView.as_view(), name="menu"),
    path("menu/barcode-lookup/", views.PosBarcodeLookupView.as_view(), name="menu-barcode-lookup"),
    path("menu/categories/", views.PosMenuCategoryManagementView.as_view(), name="menu-categories"),
    path("menu/categories/<int:pk>/", views.PosMenuCategoryManagementDetailView.as_view(), name="menu-category-detail"),
    path("menu/items/", views.PosMenuItemManagementView.as_view(), name="menu-items"),
    path("menu/items/<int:pk>/", views.PosMenuItemManagementDetailView.as_view(), name="menu-item-detail"),
    path("menu/modifiers/", views.PosMenuModifierManagementView.as_view(), name="menu-modifiers"),
    path("menu/modifiers/<int:pk>/", views.PosMenuModifierManagementDetailView.as_view(), name="menu-modifier-detail"),
    path("restaurant-tables/", views.PosRestaurantTableListCreateView.as_view(), name="restaurant-tables"),
    path("restaurant-tables/<int:pk>/", views.PosRestaurantTableDetailView.as_view(), name="restaurant-table-detail"),
    path("restaurant-table-sessions/", views.PosRestaurantTableSessionListCreateView.as_view(), name="restaurant-table-sessions"),
    path("restaurant-table-sessions/<str:reference>/", views.PosRestaurantTableSessionDetailView.as_view(), name="restaurant-table-session-detail"),
    path("restaurant-table-sessions/<str:reference>/close/", views.PosRestaurantTableSessionCloseView.as_view(), name="restaurant-table-session-close"),
    path("restaurant-table-sessions/<str:reference>/transfer/", views.PosRestaurantTableSessionTransferView.as_view(), name="restaurant-table-session-transfer"),
    path("room-service-stays/", views.PosRoomServiceStayListView.as_view(), name="room-service-stays"),
    path("orders/", views.PosOrderListCreateView.as_view(), name="orders"),
    # Bar/restaurant unopened-item returns. The static "orders/returns/…" route
    # must come before "orders/<str:reference>/" so the literal segment is not
    # captured as an order reference.
    path("orders/returns/eligible/", views.PosReturnableOrderListView.as_view(), name="order-returns-eligible"),
    path("orders/returns/", views.PosOrderReturnListCreateView.as_view(), name="order-returns"),
    path("orders/<str:reference>/return-preview/", views.PosOrderReturnPreviewView.as_view(), name="order-return-preview"),
    path("orders/<str:reference>/returns/", views.PosOrderReturnListCreateView.as_view(), name="order-return-create"),
    path("returns/<str:reference>/refund/", views.PosOrderReturnRefundConfirmView.as_view(), name="order-return-refund"),
    path("orders/<str:reference>/", views.PosOrderDetailView.as_view(), name="order-detail"),
    path("orders/<str:reference>/submit/", views.PosOrderSubmitView.as_view(), name="order-submit"),
    path("orders/<str:reference>/status/", views.PosOrderStatusView.as_view(), name="order-status"),
    path("orders/<str:reference>/tenders/", views.PosTenderCaptureView.as_view(), name="order-tenders"),
    path("kitchen-tickets/", views.KitchenTicketListView.as_view(), name="kitchen-tickets"),
    path("kitchen-tickets/<int:pk>/status/", views.KitchenTicketStatusView.as_view(), name="kitchen-ticket-status"),
    path("cash-sessions/open/", views.CashSessionOpenView.as_view(), name="cash-session-open"),
    path("cash-sessions/<str:reference>/close/", views.CashSessionCloseView.as_view(), name="cash-session-close"),
]
