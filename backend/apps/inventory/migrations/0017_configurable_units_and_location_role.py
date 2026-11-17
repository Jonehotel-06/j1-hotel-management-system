"""Add configurable measurement units and a persisted Reception store flag.

No stock balance, movement, item or historical transaction is rewritten. Unit
rows are derived ONLY from base-unit tokens already present on real stock
items; no sample catalogue is inserted. ``is_reception`` is backfilled from an
existing active location with the legacy RECEPTION code so deployments keep
working until an administrator configures a different store.
"""
from django.db import migrations, models


def seed_from_existing_data(apps, schema_editor):
    db = schema_editor.connection.alias
    StockItem = apps.get_model("inventory", "StockItem")
    StockLocation = apps.get_model("inventory", "StockLocation")
    StockUnit = apps.get_model("inventory", "StockUnit")

    seen = set()
    rows = []
    for token in StockItem.objects.using(db).order_by().values_list("base_unit", flat=True).distinct():
        code = str(token or "").strip().upper()
        if not code or code.casefold() in seen or len(code) > 30:
            continue
        seen.add(code.casefold())
        rows.append(StockUnit(
            code=code,
            label=code,
            description="Configured from an existing stock item's base unit.",
            is_active=True,
        ))
    if rows:
        StockUnit.objects.using(db).bulk_create(rows, ignore_conflicts=True)

    legacy = (
        StockLocation.objects.using(db)
        .filter(code__iexact="RECEPTION", is_active=True)
        .order_by("pk")
        .first()
    )
    if legacy is not None:
        StockLocation.objects.using(db).filter(is_reception=True).exclude(pk=legacy.pk).update(is_reception=False)
        StockLocation.objects.using(db).filter(pk=legacy.pk).update(is_reception=True)


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0016_manual_issue_and_procurement_status"),
    ]

    operations = [
        migrations.CreateModel(
            name="StockUnit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("code", models.CharField(max_length=30, unique=True)),
                ("label", models.CharField(max_length=80)),
                ("description", models.CharField(blank=True, default="", max_length=255)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
            ],
            options={"ordering": ["code"]},
        ),
        migrations.AddField(
            model_name="stocklocation",
            name="is_reception",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.RunPython(seed_from_existing_data, migrations.RunPython.noop),
    ]
