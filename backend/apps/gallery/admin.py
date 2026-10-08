# apps/gallery/admin.py
from django.contrib import admin

from .models import GalleryItem


@admin.register(GalleryItem)
class GalleryItemAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "is_active", "display_order", "created_at")
    list_filter = ("category", "is_active")
