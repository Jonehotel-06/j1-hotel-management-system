# apps/finance/migrations/0023_guest_collection_reference_per_booking.py
"""Re-key external payment references per booking, not globally.

WHY THIS MIGRATION EXISTS
=========================
``GuestCollectionKey.external_reference_key`` carried a UNIQUE constraint over a
hash of the raw reference, so the *same* bank transfer could only ever settle
one folio. One real transfer legitimately pays for several (a group paying two
rooms, one slip covering the room and the laundry), and the second collection
was refused with "This payment reference has already been recorded."

The key now contains the booking (see
``payment_collection_service.external_reference_key``) and the provider is
stored alongside it, so a card slip ``1234`` and a bank reference ``1234`` never
collide.

Existing rows are re-keyed in place: each stored hash is replaced by the new
``sha256("<booking_id>:<normalised reference>")`` value, and the provider is
backfilled from the payment it points at. Calculation is per row and idempotent
- a row already holding the new-style key is left alone - so the migration is
safe to re-run on a database that was partly migrated. Historical replay
protection is therefore preserved: the money already recorded stays protected
against a second collection under the same slip.
"""
from __future__ import annotations

import hashlib

from django.db import migrations


def rekey(apps, schema_editor):
    GuestCollectionKey = apps.get_model("finance", "GuestCollectionKey")
    db = schema_editor.connection.alias

    for record in GuestCollectionKey.objects.using(db).select_related("payment").iterator():
        changed = False

        reference = str(record.external_reference or "").strip()
        if reference:
            new_key = hashlib.sha256(
                f"{record.booking_id}:{reference.casefold()}".encode()
            ).hexdigest()
            if record.external_reference_key != new_key:
                record.external_reference_key = new_key
                changed = True
        elif record.external_reference_key:
            # A row with no raw reference cannot have been deduped on one; the
            # old code stored a hash anyway. Clearing it keeps "no reference"
            # distinct from "a reference we cannot re-derive".
            record.external_reference_key = None
            changed = True

        provider = str(getattr(record.payment, "provider", "") or "").upper()
        if provider and record.provider != provider:
            record.provider = provider
            changed = True

        if changed:
            record.save(update_fields=["external_reference_key", "provider"])


def noop(apps, schema_editor):
    """Reverse is intentionally empty.

    The previous key was a global hash that cannot be reconstructed per booking
    without guessing, and leaving the clearer key in place is harmless: the
    field is nullable and only ever compared against itself.
    """


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0023_guestcollectionkey_provider"),
    ]

    operations = [
        migrations.RunPython(rekey, noop),
    ]
