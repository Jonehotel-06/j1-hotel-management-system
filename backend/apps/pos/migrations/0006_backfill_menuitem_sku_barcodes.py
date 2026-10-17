# Backfill: existing unlabelled menu items adopt their SKU as barcode.

from django.db import migrations


def backfill_sku_barcodes(apps, schema_editor):
    from apps.pos.barcode import validate_barcode

    MenuItem = apps.get_model("pos", "MenuItem")
    claimed = set(
        MenuItem.objects.exclude(barcode__isnull=True).values_list("barcode", flat=True)
    )
    for item in MenuItem.objects.filter(barcode__isnull=True).iterator():
        try:
            candidate = validate_barcode(item.sku)
        except ValueError:
            continue
        if not candidate or candidate in claimed:
            continue
        MenuItem.objects.filter(pk=item.pk, barcode__isnull=True).update(barcode=candidate)
        claimed.add(candidate)


class Migration(migrations.Migration):
    dependencies = [
        ("pos", "0005_menuitem_barcode"),
    ]

    operations = [
        # Reverse is intentionally a no-op: assigned barcodes may since have
        # been printed on physical labels, so unassigning them in bulk would
        # orphan labels already stuck on products.
        migrations.RunPython(backfill_sku_barcodes, migrations.RunPython.noop),
    ]
