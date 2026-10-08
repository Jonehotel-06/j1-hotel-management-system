from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]
    operations = [migrations.AlterField(
        model_name="user", name="role",
        field=models.CharField(choices=[("ADMIN", "Administrator"), ("MANAGER", "Manager"), ("RECEPTIONIST", "Receptionist"), ("GUEST", "Guest")], db_index=True, default="RECEPTIONIST", max_length=20),
    )]
