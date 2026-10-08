# apps/payments/urls.py
from django.urls import path

from . import views

app_name = "payments"

urlpatterns = [
    path("initialize/", views.InitializePaymentView.as_view(), name="initialize"),
    path("verify/<str:reference>/", views.VerifyPaymentView.as_view(), name="verify"),
    path("webhook/", views.PaystackWebhookView.as_view(), name="webhook"),
]
