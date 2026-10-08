# apps/core/models.py
"""Shared abstract model mixins. Core owns no tables itself."""
from django.db import models


class TimeStampedModel(models.Model):
    """Abstract base adding creation/update audit timestamps."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
