# Generated for structured cancellation/refund enquiries.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bookings', '0002_guest_access_token'),
        ('payments', '0002_refunds_and_partial_refund_status'),
        ('enquiries', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='enquiry',
            name='enquiry_type',
            field=models.CharField(choices=[('GENERAL', 'General enquiry'), ('CANCELLATION', 'Cancellation / refund request')], db_index=True, default='GENERAL', max_length=20),
        ),
        migrations.AddField(
            model_name='enquiry',
            name='cancellation_reference',
            field=models.CharField(blank=True, db_index=True, max_length=60, null=True, unique=True),
        ),
        migrations.AddField(
            model_name='enquiry',
            name='public_access_token_hash',
            field=models.CharField(blank=True, db_index=True, default='', max_length=128),
        ),
        migrations.AddField(
            model_name='enquiry',
            name='public_access_expires_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='enquiry',
            name='booking_reference',
            field=models.CharField(blank=True, db_index=True, default='', max_length=60),
        ),
        migrations.AddField(
            model_name='enquiry',
            name='payment_reference',
            field=models.CharField(blank=True, db_index=True, default='', help_text='Guest-supplied Paystack transaction/payment reference, if available.', max_length=80),
        ),
        migrations.AddField(
            model_name='enquiry',
            name='receipt_reference',
            field=models.CharField(blank=True, default='', max_length=80),
        ),
        migrations.AddField(
            model_name='enquiry',
            name='cancellation_reason',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='enquiry',
            name='preferred_contact_method',
            field=models.CharField(blank=True, choices=[('EMAIL', 'Email'), ('PHONE', 'Phone'), ('WHATSAPP', 'WhatsApp')], default='', max_length=20),
        ),
        migrations.AddField(
            model_name='enquiry',
            name='refund_requested',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='enquiry',
            name='related_booking',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='cancellation_enquiries', to='bookings.booking'),
        ),
        migrations.AddField(
            model_name='enquiry',
            name='related_payment',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='cancellation_enquiries', to='payments.payment'),
        ),
        migrations.AddField(
            model_name='enquiry',
            name='cancellation_status',
            field=models.CharField(choices=[('NEW', 'New'), ('UNDER_REVIEW', 'Under review'), ('APPROVED', 'Approved'), ('REJECTED', 'Rejected'), ('CANCELLED', 'Booking cancelled'), ('REFUND_PENDING', 'Refund pending'), ('REFUND_PROCESSING', 'Refund processing'), ('REFUNDED', 'Refunded'), ('REFUND_FAILED', 'Refund failed'), ('CLOSED', 'Closed')], db_index=True, default='NEW', max_length=30),
        ),
        migrations.AddField(
            model_name='enquiry',
            name='refund_status',
            field=models.CharField(choices=[('NONE', 'No refund reviewed'), ('NOT_REQUIRED', 'No refund required'), ('DUE', 'Refund due'), ('MANUAL_REQUIRED', 'Manual refund required'), ('PENDING', 'Refund pending'), ('PROCESSING', 'Refund processing'), ('PROCESSED', 'Refund processed'), ('FAILED', 'Refund failed'), ('NEEDS_ATTENTION', 'Needs attention')], db_index=True, default='NONE', max_length=30),
        ),
        migrations.AddField(
            model_name='enquiry',
            name='calculated_cancellation_fee',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name='enquiry',
            name='calculated_refund_amount',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name='enquiry',
            name='paystack_refund_reference',
            field=models.CharField(blank=True, db_index=True, default='', max_length=100),
        ),
        migrations.AddField(
            model_name='enquiry',
            name='processed_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='processed_enquiries', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='enquiry',
            name='processed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='enquiry',
            name='resolution',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='enquiry',
            name='metadata',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='enquiry',
            name='email_events',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddIndex(
            model_name='enquiry',
            index=models.Index(fields=['enquiry_type', 'status'], name='enquiries_e_enquiry_5d4994_idx'),
        ),
        migrations.AddIndex(
            model_name='enquiry',
            index=models.Index(fields=['cancellation_status', 'refund_status'], name='enquiries_e_cancell_3fb2ee_idx'),
        ),
        migrations.AddIndex(
            model_name='enquiry',
            index=models.Index(fields=['booking_reference'], name='enquiries_e_booking_09324e_idx'),
        ),
        migrations.AddIndex(
            model_name='enquiry',
            index=models.Index(fields=['payment_reference'], name='enquiries_e_payment_0e8fec_idx'),
        ),
    ]
