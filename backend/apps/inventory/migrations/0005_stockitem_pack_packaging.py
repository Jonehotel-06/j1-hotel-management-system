from decimal import Decimal

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    """Additive supplier-packaging references on stock items.

    Both fields are nullable and carry no default, so existing rows are
    untouched and the migration is safe to apply and reverse on a live
    database. Stock balances, movements and costs are not modified.
    """

    dependencies = [
        ("inventory", "0004_pos_recipe_consumption"),
    ]

    operations = [
        migrations.AddField(
            model_name="stockitem",
            name="units_per_pack",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=10, null=True,
                validators=[django.core.validators.MinValueValidator(Decimal("0.01"))],
            ),
        ),
        migrations.AddField(
            model_name="stockitem",
            name="pack_price",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=12, null=True,
                validators=[django.core.validators.MinValueValidator(Decimal("0"))],
            ),
        ),
    ]
