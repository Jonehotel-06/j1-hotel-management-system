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


class ValidatedImageField(serializers.ImageField):
    """ImageField that decodes the upload BEFORE judging its file name.

    DRF's ``ImageField`` delegates to Django's form field, whose *first*
    validator is ``validate_image_file_extension`` — a check against Pillow's
    registered extensions. A perfectly valid JPEG whose name carries no
    extension (``blob``, which several mobile browsers send) or an extension
    Django does not list is refused on its NAME, before the bytes are ever read,
    and the staff member sees "Upload a valid image…" for a photo that is fine.

    This field therefore:

    1. runs the project's authoritative content validator first
       (:func:`apps.core.validators.validate_image_upload`) — it decodes with
       Pillow and rewrites the file's extension to the canonical one for the
       format it actually contains (``.jpg`` for JPEG, ``.png``, ``.webp``);
    2. then hands the renamed file to DRF, whose own content check still runs.

    A renamed non-image is still rejected: nothing is accepted on its extension
    alone. The result is that JPG/JPEG from any browser, camera or file manager
    uploads reliably, while a disguised executable never does.
    """

    def to_internal_value(self, data):
        from django.core.exceptions import ValidationError as DjangoValidationError

        from .validators import validate_image_upload

        if data is not None:
            try:
                validate_image_upload(data)
            except DjangoValidationError as exc:
                raise serializers.ValidationError(list(exc.messages))
        return super().to_internal_value(data)
