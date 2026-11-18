# apps/events/migrations/0002_seed_event_locations.py
"""Seed the three default event spaces. Locations remain fully configurable
afterwards via Django admin / API — this only ensures a fresh install has
Inside Bar, Outside Bar and Hotel Compound available."""
from django.db import migrations


DEFAULT_LOCATIONS = [
    {"code": "INSIDE_BAR", "name": "Inside Bar", "display_order": 1},
    {"code": "OUTSIDE_BAR", "name": "Outside Bar", "display_order": 2},
    {"code": "HOTEL_COMPOUND", "name": "Hotel Compound", "display_order": 3},
]


def seed(apps, schema_editor):
    EventLocation = apps.get_model("events", "EventLocation")
    for row in DEFAULT_LOCATIONS:
        EventLocation.objects.get_or_create(code=row["code"], defaults={**row, "is_active": True})


def unseed(apps, schema_editor):
    EventLocation = apps.get_model("events", "EventLocation")
    EventLocation.objects.filter(code__in=[r["code"] for r in DEFAULT_LOCATIONS]).delete()


class Migration(migrations.Migration):
    dependencies = [("events", "0001_initial")]
    operations = [migrations.RunPython(seed, unseed)]
