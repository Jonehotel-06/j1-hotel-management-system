# apps/reviews/urls_admin.py
from django.urls import path

from . import views

app_name = "reviews_admin"

urlpatterns = [
    path("", views.ReviewAdminListView.as_view(), name="review-list"),
    path("stats/", views.ReviewStatsView.as_view(), name="review-stats"),
    path("<int:pk>/", views.ReviewAdminDetailView.as_view(), name="review-detail"),
]
