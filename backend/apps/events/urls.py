# apps/events/urls.py
from django.urls import path

from . import views

app_name = "events"

urlpatterns = [
    path("locations/", views.EventLocationListView.as_view(), name="locations"),
    path("", views.EventListCreateView.as_view(), name="list"),
    path("<str:reference>/", views.EventDetailView.as_view(), name="detail"),
    path("<str:reference>/payments/", views.EventPaymentView.as_view(), name="payments"),
    path("<str:reference>/cancel/", views.EventCancelView.as_view(), name="cancel"),
    path("<str:reference>/receipt/", views.EventReceiptView.as_view(), name="receipt"),
]
