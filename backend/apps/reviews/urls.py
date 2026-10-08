# apps/reviews/urls.py
from django.urls import path

from . import views

app_name = "reviews"

# Public guest flow — verification-gated, throttled, no listing endpoint.
urlpatterns = [
    path("verify/", views.ReviewVerifyView.as_view(), name="verify"),
    path("", views.ReviewSubmitView.as_view(), name="submit"),
]
