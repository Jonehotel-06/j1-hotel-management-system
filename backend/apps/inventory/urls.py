from django.urls import path
from . import views

app_name = "inventory"

urlpatterns = [
    path("locations/", views.StockLocationListCreateView.as_view(), name="locations"),
    path("locations/<int:pk>/", views.StockLocationDetailView.as_view(), name="location-detail"),
    path("items/", views.StockItemListCreateView.as_view(), name="items"),
    path("items/<int:pk>/", views.StockItemDetailView.as_view(), name="item-detail"),
    path("menu-items/", views.InventoryMenuItemDirectoryView.as_view(), name="menu-item-directory"),
    path("balances/", views.StockBalanceListView.as_view(), name="balances"),
    path("movements/", views.StockMovementListView.as_view(), name="movements"),
    path("recipes/", views.StockRecipeListCreateView.as_view(), name="recipes"),
    path("recipes/<str:reference>/retire/", views.StockRecipeRetireView.as_view(), name="recipe-retire"),
    path("consumption-requests/", views.StockConsumptionRequestListView.as_view(), name="consumption-requests"),
    path("consumption-requests/<str:reference>/process/", views.StockConsumptionRequestProcessView.as_view(), name="consumption-request-process"),
    path("stock-counts/", views.StockCountListCreateView.as_view(), name="stock-counts"),
    path("stock-counts/<str:reference>/", views.StockCountDetailView.as_view(), name="stock-count-detail"),
    path("stock-counts/<str:reference>/submit/", views.StockCountSubmitView.as_view(), name="stock-count-submit"),
    path("stock-counts/<str:reference>/approve/", views.StockCountApproveView.as_view(), name="stock-count-approve"),
    path("issues/", views.StockIssueView.as_view(), name="issues"),
    path("suppliers/", views.SupplierListCreateView.as_view(), name="suppliers"),
    path("suppliers/<int:pk>/", views.SupplierDetailView.as_view(), name="supplier-detail"),
    path("purchase-orders/", views.PurchaseOrderListCreateView.as_view(), name="purchase-orders"),
    path("purchase-orders/<str:reference>/", views.PurchaseOrderDetailView.as_view(), name="purchase-order-detail"),
    path("purchase-orders/<str:reference>/submit/", views.PurchaseOrderSubmitView.as_view(), name="purchase-order-submit"),
    path("purchase-orders/<str:reference>/approve/", views.PurchaseOrderApproveView.as_view(), name="purchase-order-approve"),
    path("purchase-orders/<str:reference>/ordered/", views.PurchaseOrderOrderedView.as_view(), name="purchase-order-ordered"),
    path("purchase-orders/<str:reference>/receipts/", views.GoodsReceiptCreateView.as_view(), name="goods-receipt-create"),
]
