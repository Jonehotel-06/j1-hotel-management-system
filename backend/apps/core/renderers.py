# apps/core/renderers.py
"""Project-wide JSON envelope.

Every API response uses a predictable shape:

    success: {"success": true, "message": "...", "data": ...}
    error:   {"success": false, "code": "...", "message": "...", "errors": {...}}

Views/helpers already building the envelope (success_response, pagination,
the exception handler) include a "success" key and are passed through
untouched; bare serializer payloads get wrapped automatically.
"""
from rest_framework.renderers import JSONRenderer


class JOneJSONRenderer(JSONRenderer):
    def render(self, data, accepted_media_type=None, renderer_context=None):
        if data is None:
            return b""
        response = (renderer_context or {}).get("response")
        status_code = getattr(response, "status_code", 200)

        if isinstance(data, dict) and "success" in data:
            payload = data
        elif 200 <= status_code < 300:
            payload = {"success": True, "data": data}
        else:
            # Non-2xx payloads should always come from the custom exception
            # handler (already enveloped). If one slips through, wrap it.
            payload = {"success": False, "message": "Request failed.", "errors": data}
        return super().render(payload, accepted_media_type, renderer_context)
