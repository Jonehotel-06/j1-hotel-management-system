# apps/reviews/admin.py
from django.contrib import admin

from .models import Review


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ("booking", "guest_name", "rating", "status", "created_at")
    list_filter = ("rating", "status")
    search_fields = ("guest_name", "booking__booking_reference", "comment")
    readonly_fields = ("booking", "guest", "guest_name", "rating", "comment", "created_at", "updated_at")
