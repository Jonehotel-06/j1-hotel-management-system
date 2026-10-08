# apps/core/views_diagnostics.py
"""TEMPORARY diagnostic endpoint — wraps `manage.py smtp_check` over HTTP so it
can be run without shell access on a managed hosting plan.

DELETE THIS FILE (and its URL) once email delivery is confirmed working.
Staff-only; never prints the SMTP password (call_command reuses the same
safe command that already redacts it).
"""
import io

from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import IsStaffRole


class AdminSmtpDiagnosticView(APIView):
    permission_classes = [IsStaffRole]

    def get(self, request):
        recipient = (request.query_params.get("recipient") or "").strip()
        if not recipient:
            return Response(
                {"error": "Pass ?recipient=you@example.com in the query string."},
                status=400,
            )
        out, err = io.StringIO(), io.StringIO()
        try:
            call_command("smtp_check", recipient, stdout=out, stderr=err)
            return Response(
                {"success": True, "output": out.getvalue()}, status=200
            )
        except CommandError as exc:
            return Response(
                {"success": False, "output": out.getvalue(), "error": str(exc)},
                status=502,
            )