# Department tag for Finance expenses (reporting only).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0010_alter_financialtransaction_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='expense',
            name='department',
            field=models.CharField(blank=True, db_index=True, default='', max_length=80),
        ),
    ]
