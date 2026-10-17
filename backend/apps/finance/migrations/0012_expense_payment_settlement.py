# Supplier-payment settlement evidence on Finance expenses (nullable additions).

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0011_expense_department'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='expense',
            name='paid_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='expense',
            name='paid_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='expenses_paid_out', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='expense',
            name='payment_reference',
            field=models.CharField(blank=True, default='', max_length=160),
        ),
        migrations.AddField(
            model_name='expense',
            name='payment_receipt_url',
            field=models.URLField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='expense',
            name='payment_receipt_file',
            field=models.FileField(blank=True, null=True, upload_to='finance_receipts/%Y/%m/'),
        ),
        migrations.AlterField(
            model_name='expenseevent',
            name='type',
            field=models.CharField(choices=[('CREATED', 'Created'), ('SUBMITTED', 'Submitted'), ('APPROVED', 'Approved'), ('REJECTED', 'Rejected'), ('POSTED', 'Posted'), ('PAID', 'Supplier paid')], db_index=True, max_length=16),
        ),
        migrations.AddField(
            model_name='expense',
            name='settlement_transaction',
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='expense_settlement', to='finance.financialtransaction'),
        ),
    ]
