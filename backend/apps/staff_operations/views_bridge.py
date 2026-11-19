"""
API endpoints for fingerprint reader bridge integration.

These views let the browser-based frontend query the local fingerprint reader
bridge running on the Windows 11 workstation, and submit captures through it.

Endpoints:
  GET    /api/admin/staff-operations/biometrics/bridge/status/     — reader status
  POST   /api/admin/staff-operations/biometrics/bridge/initialize/ — (re)initialize reader
  POST   /api/admin/staff-operations/biometrics/bridge/capture/    — capture fingerprint probe
  POST   /api/admin/staff-operations/biometrics/bridge/submit/     — submit capture to attendance backend

The bridge itself runs as a local WebSocket server on the workstation
(see services/fingerprint_bridge.py). These HTTP views are a convenience
layer that delegates to the singleton bridge instance when the browser
cannot open a WebSocket directly (e.g. same-origin restrictions).
"""

from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied

from apps.accounts.capabilities import has_capability
from apps.core.responses import success_response

from .biometric_models import FingerprintDevice, FingerprintDeviceEvent
from .services import fingerprint_device, fingerprint_service
from .services.fingerprint_bridge import (
    get_bridge,
    READER_STATUS_CONNECTED,
    READER_STATUS_NOT_CONNECTED,
    READER_STATUS_INITIALIZING,
    READER_STATUS_DRIVER_MISSING,
    READER_STATUS_SDK_UNAVAILABLE,
    READER_STATUS_PERMISSION_DENIED,
    READER_STATUS_BUSY,
    READER_STATUS_COMMUNICATION_FAILURE,
)

logger = logging.getLogger(__name__)


def _bridge_status_to_device_status(bridge_status: str) -> str:
    """Map bridge status to FingerprintDevice.Status values where applicable."""
    mapping = {
        READER_STATUS_CONNECTED: FingerprintDevice.Status.ACTIVE,
        READER_STATUS_NOT_CONNECTED: FingerprintDevice.Status.DISABLED,
        READER_STATUS_DRIVER_MISSING: FingerprintDevice.Status.FAULT,
        READER_STATUS_SDK_UNAVAILABLE: FingerprintDevice.Status.FAULT,
        READER_STATUS_PERMISSION_DENIED: FingerprintDevice.Status.FAULT,
        READER_STATUS_COMMUNICATION_FAILURE: FingerprintDevice.Status.FAULT,
    }
    return mapping.get(bridge_status, FingerprintDevice.Status.DISABLED)


class BridgeReaderStatusView(APIView):
    """
    Report the current fingerprint reader status from the local bridge.

    Returns one of:
      connected           — reader connected and ready
      initializing        — reader is initializing
      not_connected       — no reader detected
      driver_missing      — reader detected but driver missing
      sdk_unavailable     — reader+driver present but SDK missing
      permission_denied   — access to reader denied
      busy                — capture in progress
      communication_failure — reader communication failed
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_capability(request.user, "attendance.clock") and not has_capability(request.user, "attendance.manage"):
            raise DjangoPermissionDenied("This user cannot view fingerprint reader status.")

        bridge = get_bridge()
        resp = bridge._status_response()
        return success_response(resp)


class BridgeReaderInitializeView(APIView):
    """
    (Re)initialize the fingerprint reader via the local bridge.

    The bridge scans for the DigitalPersona 4500, loads the SDK, and opens
    the device. Returns the resulting status.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not has_capability(request.user, "attendance.manage"):
            raise DjangoPermissionDenied("This user cannot initialize fingerprint devices.")

        bridge = get_bridge()
        resp = bridge.initialize()

        # Also update/create a FingerprintDevice row so the backend device
        # registry reflects the physical reader state.
        if resp["connected"] and resp.get("device_serial"):
            try:
                from apps.accounts.models import Workstation  # noqa: F811
                ws = Workstation.objects.filter(department=Workstation.Department.FRONT_DESK, is_active=True).first()
                if ws:
                    fingerprint_device.register_device(
                        serial_number=resp["device_serial"],
                        label="DigitalPersona U.are.U 4500 (local bridge)",
                        workstation=ws,
                        actor=request.user,
                        vendor="HID Global / DigitalPersona",
                        model_name="U.are.U 4500",
                        notes="Discovered and registered via local fingerprint bridge.",
                    )
            except Exception as exc:
                logger.warning("Could not register discovered reader: %s", exc)

        status_code = status.HTTP_200_OK if resp["connected"] else status.HTTP_400_BAD_REQUEST
        return success_response(resp, status=status_code)


class BridgeReaderCaptureView(APIView):
    """
    Capture a fingerprint minutiae probe from the local reader.

    The bridge performs the capture and returns ISO/IEC 19794-2 minutiae
    points. The browser then submits these to the attendance backend
    via the existing device-capture contract.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not has_capability(request.user, "attendance.clock") and not has_capability(request.user, "attendance.manage"):
            raise DjangoPermissionDenied("This user cannot perform fingerprint capture.")

        bridge = get_bridge()
        resp = bridge.capture(
            timeout_ms=int(request.data.get("timeout_ms", 30000)),
            finger_position=str(request.data.get("finger_position", "RIGHT_INDEX")),
        )

        if not resp.get("success"):
            error = resp.get("error", "UNKNOWN")
            message = resp.get("message", "Capture failed.")
            status_code = status.HTTP_400_BAD_REQUEST
            if error == "READER_NOT_CONNECTED":
                status_code = status.HTTP_409_CONFLICT
            return success_response({"success": False, "error": error, "message": message},
                                    status=status_code)

        return success_response({"success": True, "minutiae": resp.get("minutiae", []),
                                 "finger_position": resp.get("finger_position", "RIGHT_INDEX")})


class BridgeReaderSubmitCaptureView(APIView):
    """
    Submit a fingerprint capture (from the local bridge) to the attendance
    backend for verification and clock-in processing.

    This reuses the existing submit_device_capture contract so that captures
    from the physical reader flow through the same audit trail and attendance
    state machine as any other registered device.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        from django.utils import timezone

        if not has_capability(request.user, "attendance.clock") and not has_capability(request.user, "attendance.manage"):
            raise DjangoPermissionDenied("This user cannot submit fingerprint captures.")

        serial = str(request.data.get("serial_number", "")).strip()
        nonce = str(request.data.get("event_nonce", "")).strip()
        action = str(request.data.get("action", "CLOCK_IN")).upper()
        probe = request.data.get("minutiae") or request.data.get("probe")

        if not serial:
            raise ValidationError({"serial_number": "Device serial number is required."})
        if not nonce:
            raise ValidationError({"event_nonce": "Capture event nonce is required."})

        # Resolve the staff member: either explicitly or by 1:N template match.
        staff = fingerprint_service.lookup_staff_by_identifier(
            email=request.data.get("email", ""),
            employee_code=request.data.get("employee_code", ""),
            staff_id=request.data.get("staff_id"),
            staff_profile_id=request.data.get("staff_profile_id"),
            identifier=request.data.get("identifier", ""),
        )

        try:
            event, outcome, passed, attendance_record = fingerprint_device.submit_device_capture(
                serial_number=serial,
                event_nonce=nonce,
                captured_at=timezone.now(),
                action=action,
                staff=staff,
                probe=probe,
                actor=request.user if getattr(request.user, "is_authenticated", False) else None,
            )
        except ValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else {"detail": exc.messages})

        return success_response({
            "success": True,
            "outcome": outcome,
            "passed": passed,
            "event_id": event.pk,
            "attendance_reference": event.attendance_reference,
            "staff_id": staff.pk if staff else None,
            "staff_name": staff.full_name if staff else None,
            "match_score": getattr(event, "match_score", None),
        })


class BridgeReaderDevicesView(APIView):
    """
    List fingerprint devices registered in the backend, enriched with
    live status from the local bridge where available.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_capability(request.user, "attendance.clock") and not has_capability(request.user, "attendance.manage"):
            raise DjangoPermissionDenied("This user cannot view fingerprint devices.")

        bridge = get_bridge()
        bridge_status = bridge._status_response()

        devices = FingerprintDevice.objects.select_related("workstation", "registered_by").all()
        data = []
        for d in devices:
            # If the bridge has a live serial match, overlay its status.
            live = None
            if bridge_status.get("device_serial") and d.serial_number == bridge_status["device_serial"]:
                live = bridge_status["status"]
            data.append({
                "id": d.pk,
                "serial_number": d.serial_number,
                "label": d.label,
                "workstation_id": d.workstation_id,
                "workstation_reference": d.workstation.reference if d.workstation else "",
                "workstation_name": d.workstation.name if d.workstation else "",
                "vendor": d.vendor,
                "model_name": d.model_name,
                "status": d.status,
                "live_status": live or d.status.lower(),
                "live_detail": bridge_status.get("detail", "") if live else "",
                "last_seen_at": d.last_seen_at,
                "last_error": d.last_error,
                "notes": d.notes,
                "created_at": d.created_at,
            })
        return success_response(data)
