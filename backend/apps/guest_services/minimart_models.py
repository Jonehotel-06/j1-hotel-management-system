"""Retail configuration and links; inventory, POS and finance remain authoritative."""
from decimal import Decimal
from django.db import models
from django.db.models import Q
from apps.core.models import TimeStampedModel


class MiniMartProduct(TimeStampedModel):
    item = models.OneToOneField("inventory.StockItem", on_delete=models.PROTECT, related_name="minimart_product")
    location = models.ForeignKey("inventory.StockLocation", on_delete=models.PROTECT, related_name="minimart_products")
    selling_price = models.DecimalField(max_digits=12, decimal_places=2)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(selling_price__gt=Decimal("0")), name="minimart_positive_price")]

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.item_id and self.item.item_type != "SELLABLE_PRODUCT":
            raise ValidationError({"item": "Classify the inventory item as a sellable product first."})
        if self.item_id and self.item.currency.upper() != "NGN":
            raise ValidationError({"item": "Mini Mart currently supports NGN only."})

    def __str__(self):
        return str(self.item)


class MiniMartSale(TimeStampedModel):
    order = models.OneToOneField("pos.PosOrder", on_delete=models.PROTECT, related_name="minimart_sale")
    guest = models.ForeignKey("bookings.Guest", null=True, blank=True, on_delete=models.PROTECT, related_name="minimart_sales")
    booking = models.ForeignKey("bookings.Booking", null=True, blank=True, on_delete=models.PROTECT, related_name="minimart_sales")
    room_number = models.CharField(max_length=200, blank=True, default="")
    source_key = models.CharField(max_length=96, unique=True)
    payload_fingerprint = models.CharField(max_length=64)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["created_at"], name="minimart_sale_created_idx")]
        constraints = [models.CheckConstraint(condition=Q(booking__isnull=True) | Q(guest__isnull=False), name="minimart_booking_requires_guest")]

    def save(self, *args, **kwargs):
        if self.pk:
            from django.core.exceptions import ValidationError
            raise ValidationError("Mini Mart sales evidence is append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        from django.core.exceptions import ValidationError
        raise ValidationError("Mini Mart sales evidence cannot be deleted.")


class MiniMartSaleLine(models.Model):
    sale = models.ForeignKey(MiniMartSale, on_delete=models.PROTECT, related_name="lines")
    product = models.ForeignKey(MiniMartProduct, on_delete=models.PROTECT, related_name="sale_lines")
    order_line = models.OneToOneField("pos.PosOrderLine", on_delete=models.PROTECT, related_name="minimart_line")
    movement = models.OneToOneField("inventory.StockMovement", on_delete=models.PROTECT, related_name="minimart_line")

    def save(self, *args, **kwargs):
        if self.pk:
            from django.core.exceptions import ValidationError
            raise ValidationError("Mini Mart sales evidence is append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        from django.core.exceptions import ValidationError
        raise ValidationError("Mini Mart sales evidence cannot be deleted.")
