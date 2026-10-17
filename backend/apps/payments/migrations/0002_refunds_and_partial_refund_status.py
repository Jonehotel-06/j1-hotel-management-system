# Generated for production payment/refund workflow redesign.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q
from django.utils import timezone


class Migration(migrations.Migration):

    dependencies = [
        ('bookings', '0002_guest_access_token'),
        ('payments', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name='payment',
            name='reference',
            field=models.CharField(db_index=True, help_text='Internal payment reference. For Paystack payments, this is also the Paystack transaction reference.', max_length=60, unique=True),
        ),
        migrations.AlterField(
            model_name='payment',
            name='status',
            field=models.CharField(choices=[('PENDING', 'Pending'), ('SUCCESS', 'Success'), ('FAILED', 'Failed'), ('PARTIALLY_REFUNDED', 'Partially refunded'), ('REFUNDED', 'Refunded')], db_index=True, default='PENDING', max_length=20),
        ),
        migrations.CreateModel(
            name='Refund',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=12)),
                ('currency', models.CharField(default='NGN', max_length=3)),
                ('paystack_transaction_reference', models.CharField(db_index=True, max_length=60)),
                ('paystack_transaction_id', models.CharField(blank=True, db_index=True, default='', max_length=60)),
                ('paystack_refund_id', models.CharField(blank=True, db_index=True, default='', max_length=60)),
                ('paystack_refund_reference', models.CharField(blank=True, db_index=True, default='', max_length=100)),
                ('status', models.CharField(choices=[('PENDING', 'Pending'), ('PROCESSING', 'Processing'), ('PROCESSED', 'Processed'), ('FAILED', 'Failed'), ('NEEDS_ATTENTION', 'Needs attention')], db_index=True, default='PENDING', max_length=20)),
                ('customer_note', models.TextField(blank=True, default='')),
                ('merchant_note', models.TextField(blank=True, default='')),
                ('requested_at', models.DateTimeField(default=timezone.now)),
                ('submitted_at', models.DateTimeField(blank=True, null=True)),
                ('processed_at', models.DateTimeField(blank=True, null=True)),
                ('failed_at', models.DateTimeField(blank=True, null=True)),
                ('failure_reason', models.TextField(blank=True, default='')),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('booking', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='refunds', to='bookings.booking')),
                ('payment', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='refunds', to='payments.payment')),
                ('requested_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='refunds_requested', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['booking', 'status'], name='payments_re_booking_0e2e74_idx'), models.Index(fields=['payment', 'status'], name='payments_re_payment_4444a0_idx'), models.Index(fields=['paystack_refund_reference'], name='payments_re_paystac_f9aa6d_idx')],
            },
        ),
        migrations.AddConstraint(
            model_name='refund',
            constraint=models.UniqueConstraint(condition=Q(('status__in', ['PENDING', 'PROCESSING', 'NEEDS_ATTENTION'])), fields=('payment',), name='unique_active_refund_per_payment'),
        ),
        migrations.AddConstraint(
            model_name='refund',
            constraint=models.UniqueConstraint(condition=~Q(('paystack_refund_id', '')), fields=('paystack_refund_id',), name='unique_paystack_refund_id_when_present'),
        ),
        migrations.AddConstraint(
            model_name='refund',
            constraint=models.UniqueConstraint(condition=~Q(('paystack_refund_reference', '')), fields=('paystack_refund_reference',), name='unique_paystack_refund_ref_when_present'),
        ),
    ]
