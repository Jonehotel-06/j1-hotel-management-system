# apps/core/serializers.py
"""Shared trivial serializers used for OpenAPI introspection on endpoints
that take no request body."""
from rest_framework import serializers


class EmptySerializer(serializers.Serializer):
    """No request body expected."""

    def to_representation(self, instance):
        return {}


class RejectUnknownFieldsMixin:
    """Reject request payloads containing fields the serializer does not define.

    DRF silently ignores unknown keys. On a *command* endpoint that turns a
    misnamed field into a misleading success: the request returns 2xx while the
    operation never happened (see audit D5, where ``{"assigned_to": 7}`` left a
    housekeeping task OPEN and unassigned).

    Apply this to action/dispatch serializers — the ones whose payload IS the
    instruction — so an unsupported field fails loudly with 400 instead of
    silently doing nothing. Do not apply it to model serializers used for
    partial updates, where additive client fields are merely ignored.
    """

    def validate(self, attrs):
        attrs = super().validate(attrs)
        unknown = sorted(set(getattr(self, "initial_data", None) or {}) - set(self.fields))
        if unknown:
            raise serializers.ValidationError(
                {
                    "unknown_fields": (
                        f"Unsupported field(s): {', '.join(unknown)}. "
                        f"Supported fields: {', '.join(sorted(self.fields))}."
                    )
                }
            )
        return attrs
