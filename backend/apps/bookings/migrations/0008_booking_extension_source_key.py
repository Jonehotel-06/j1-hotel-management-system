"""Add a nullable, unique retry key for stay-extension confirmations.

Historical extensions keep NULL. The field never changes dates, payments or
balances; it only lets an HTTP retry converge on the original history row.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("bookings", "0007_booking_extension"),
    ]

    operations = [
        migrations.AddField(
            model_name="bookingextension",
            name="source_key",
            field=models.CharField(
                blank=True,
                db_index=True,
                max_length=180,
                null=True,
                unique=True,
            ),
        ),
        migrations.AddField(
            model_name="bookingextension",
            name="breakdown",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
