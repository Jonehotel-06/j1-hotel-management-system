from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [("bookings", "0001_initial")]
    operations = [
        migrations.AddField("booking", "guest_access_token_hash", models.CharField(blank=True, db_index=True, default="", help_text="Hash of the unguessable token used for guest self-service access.", max_length=128)),
        migrations.AddField("booking", "guest_access_expires_at", models.DateTimeField(blank=True, null=True)),
    ]
