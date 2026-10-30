from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('gallery', '0001_initial')]

    operations = [
        migrations.AddField(
            model_name='galleryitem',
            name='homepage_section',
            field=models.CharField(blank=True, choices=[('hero', 'Hero'), ('welcome', 'Welcome to J-ONE'), ('experience', 'Experience'), ('find_us', 'Find Us')], max_length=20, null=True, unique=True),
        ),
    ]
