"""Backfill per-room check-in evidence for bookings already in house.

Before room-level check-in existed, a booking-level ``checked_in_at`` was the
only record and every ``BookingRoom`` row had no arrival timestamp of its own.
Without this backfill an in-house legacy booking would look as if NONE of its
rooms had arrived, so the first checkout would mark occupied rooms as
"did not arrive" and leave housekeeping thinking nothing was used.

Every assignment of a booking that is CHECKED_IN or CHECKED_OUT is stamped
with that booking's own ``checked_in_at`` — the truthful value we do have.
Bookings that never reached check-in are left untouched (NULL means "never
arrived", which is exactly right for them).
"""
from django.db import migrations


def backfill_room_check_ins(apps, schema_editor):
    Booking = apps.get_model("bookings", "Booking")
    BookingRoom = apps.get_model("bookings", "BookingRoom")

    checked_in_statuses = ["CHECKED_IN", "CHECKED_OUT"]
    bookings = Booking.objects.filter(
        status__in=checked_in_statuses, checked_in_at__isnull=False
    ).only("pk", "checked_in_at")

    updated = 0
    for booking in bookings.iterator(chunk_size=500):
        updated += BookingRoom.objects.filter(
            booking_id=booking.pk, checked_in_at__isnull=True
        ).update(checked_in_at=booking.checked_in_at)

    if updated:
        print(f"  room check-in backfill: {updated} room assignment(s) stamped from their booking.")


def noop(apps, schema_editor):
    """Irreversible by design.

    Clearing the timestamps again would erase genuine arrival evidence recorded
    after this migration ran, so reversing is deliberately a no-op rather than
    a guess.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("bookings", "0005_bookingroom_arrival_note_bookingroom_checked_in_at_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_room_check_ins, noop),
    ]
