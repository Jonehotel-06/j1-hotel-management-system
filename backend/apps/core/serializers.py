# apps/core/serializers.py
"""Shared trivial serializers used for OpenAPI introspection on endpoints
that take no request body."""
from rest_framework import serializers


class EmptySerializer(serializers.Serializer):
    """No request body expected."""

    def to_representation(self, instance):
        return {}
