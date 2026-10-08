# Generated for linking refund attempts to cancellation enquiries.

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ('enquiries', '0002_cancellation_request_workflow'),
        ('payments', '0002_refunds_and_partial_refund_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='refund',
            name='cancellation_request',
            field=models.ForeignKey(blank=True, help_text='Cancellation/refund enquiry that authorized this refund, when applicable.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='refunds', to='enquiries.enquiry'),
        ),
        migrations.AddConstraint(
            model_name='refund',
            constraint=models.UniqueConstraint(condition=Q(('cancellation_request__isnull', False)), fields=('cancellation_request',), name='unique_refund_per_cancellation_request'),
        ),
    ]
