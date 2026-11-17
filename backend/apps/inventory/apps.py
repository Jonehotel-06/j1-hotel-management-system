from django.apps import AppConfig


class InventoryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.inventory"
    verbose_name = "Inventory & Procurement"

    def ready(self):
        # Configured unit labels are memoised for flat list endpoints. Invalidate
        # that process-local cache on model writes and after every migration run
        # (migrations seed units with bulk_create, which emits no model signal).
        from django.db.models.signals import post_delete, post_migrate, post_save

        from .models import StockUnit
        from .services.inventory_service import clear_unit_label_cache

        post_save.connect(clear_unit_label_cache, sender=StockUnit, dispatch_uid="inventory.unit_label.save")
        post_delete.connect(clear_unit_label_cache, sender=StockUnit, dispatch_uid="inventory.unit_label.delete")
        post_migrate.connect(clear_unit_label_cache, dispatch_uid="inventory.unit_label.migrate")
