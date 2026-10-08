# apps/core/responses.py
"""Helper for building the standard success envelope from views."""
from rest_framework import status as drf_status
from rest_framework.response import Response


def success_response(data=None, message="Success", status=drf_status.HTTP_200_OK):
    payload = {"success": True, "message": message}
    if data is not None:
        payload["data"] = data
    return Response(payload, status=status)
