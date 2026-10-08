"""Collision-resistant, human-traceable finance record references."""
import secrets

from django.utils import timezone


def generate_finance_reference(prefix: str) -> str:
    """Generate a reference short enough for all finance reference fields.

    Database uniqueness remains the final concurrency guard. The 48-bit random
    suffix makes a collision operationally negligible while preserving a useful
    date/prefix when staff reconcile source documents.
    """
    return f"{prefix}-{timezone.now():%Y%m%d}-{secrets.token_hex(6).upper()}"
