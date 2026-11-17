"""Add Mini Mart categories and product description without replacing stock data.

Existing MiniMartProduct rows keep their OneToOne StockItem, location, price,
sales evidence and active state. Categories are derived from the category text
already recorded on those linked stock items; no sample categories are
inserted and no inventory table is recreated.
"""
import django.db.models.deletion
from django.db import migrations, models


def backfill_categories(apps, schema_editor):
    db = schema_editor.connection.alias
    Product = apps.get_model("guest_services", "MiniMartProduct")
    Category = apps.get_model("guest_services", "MiniMartCategory")
    by_name = {}
    for product in Product.objects.using(db).select_related("item").iterator(chunk_size=200):
        name = str(getattr(product.item, "category", "") or "").strip()
        if not name:
            continue
        key = name.casefold()
        category = by_name.get(key)
        if category is None:
            category = Category.objects.using(db).filter(name__iexact=name).first()
            if category is None:
                category = Category.objects.using(db).create(name=name, is_active=True)
            by_name[key] = category
        if product.category_id is None:
            Product.objects.using(db).filter(pk=product.pk).update(category=category)


class Migration(migrations.Migration):
    dependencies = [
        ("guest_services", "0009_minimartproduct_minimartsale_minimartsaleline_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="MiniMartCategory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=120, unique=True)),
                ("description", models.CharField(blank=True, default="", max_length=255)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.AddField(
            model_name="minimartproduct",
            name="category",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="minimart_products",
                to="guest_services.minimartcategory",
            ),
        ),
        migrations.AddField(
            model_name="minimartproduct",
            name="description",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.RunPython(backfill_categories, migrations.RunPython.noop),
    ]
