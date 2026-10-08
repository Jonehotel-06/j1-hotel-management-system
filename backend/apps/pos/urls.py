from django.urls import path

from . import views

app_name = "pos"

urlpatterns = [
    path("menu/", views.PosMenuView.as_view(), name="menu"),
    path("menu/categories/", views.PosMenuCategoryManagementView.as_view(), name="menu-categories"),
    path("menu/categories/<int:pk>/", views.PosMenuCategoryManagementDetailView.as_view(), name="menu-category-detail"),
    path("menu/items/", views.PosMenuItemManagementView.as_view(), name="menu-items"),
    path("menu/items/<int:pk>/", views.PosMenuItemManagementDetailView.as_view(), name="menu-item-detail"),
    path("menu/modifiers/", views.PosMenuModifierManagementView.as_view(), name="menu-modifiers"),
    path("menu/modifiers/<int:pk>/", views.PosMenuModifierManagementDetailView.as_view(), name="menu-modifier-detail"),
    path("room-service-stays/", views.PosRoomServiceStayListView.as_view(), name="room-service-stays"),
    path("orders/", views.PosOrderListCreateView.as_view(), name="orders"),
    path("orders/<str:reference>/", views.PosOrderDetailView.as_view(), name="order-detail"),
    path("orders/<str:reference>/submit/", views.PosOrderSubmitView.as_view(), name="order-submit"),
    path("orders/<str:reference>/status/", views.PosOrderStatusView.as_view(), name="order-status"),
    path("orders/<str:reference>/tenders/", views.PosTenderCaptureView.as_view(), name="order-tenders"),
    path("kitchen-tickets/", views.KitchenTicketListView.as_view(), name="kitchen-tickets"),
    path("cash-sessions/open/", views.CashSessionOpenView.as_view(), name="cash-session-open"),
    path("cash-sessions/<str:reference>/close/", views.CashSessionCloseView.as_view(), name="cash-session-close"),
]
