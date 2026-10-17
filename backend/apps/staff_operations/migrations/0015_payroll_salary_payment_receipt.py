# Optional receipt evidence on recorded salary payments (nullable additions).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('staff_operations', '0014_lateness_deductions'),
    ]

    operations = [
        migrations.AddField(
            model_name='payrollsalarypayment',
            name='receipt_url',
            field=models.URLField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='payrollsalarypayment',
            name='receipt_file',
            field=models.FileField(blank=True, null=True, upload_to='payroll_receipts/%Y/%m/'),
        ),
    ]
