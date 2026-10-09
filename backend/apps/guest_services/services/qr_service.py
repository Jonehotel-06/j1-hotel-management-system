"""Issuance, lookup, and local SVG generation for guest-service QR links."""
from __future__ import annotations

import hashlib
import io
import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.rooms.models import Room

from ..models import ServiceQRLink


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _new_reference() -> str:
    return "QR-" + secrets.token_hex(10).upper()


def _normalise_table(value: str) -> tuple[str, str]:
    label = " ".join(str(value or "").split())
    if not label or len(label) > 40:
        raise ValidationError({"table_number": "Enter a table label of 1–40 characters."})
    return label, label.casefold()


@transaction.atomic
def create_service_qr_link(*, target_type, actor, room_id=None, table_number="", label=""):
    """Create/reactivate one stable location record and issue a new bearer token."""
    target_type = str(target_type or "").upper()
    room = None
    table_label = ""
    if target_type == ServiceQRLink.TargetType.ROOM:
        if not room_id or table_number:
            raise ValidationError({"target_type": "A room link needs a room and no table label."})
        room = Room.objects.select_for_update().filter(pk=room_id, is_active=True).first()
        if room is None:
            raise ValidationError({"room_id": "Select an active hotel room."})
        target_key = f"room:{room.pk}"
        target_label = (str(label or "").strip() or f"Room {room.room_number}")[:160]
        stored_table = ""
    elif target_type == ServiceQRLink.TargetType.TABLE:
        if room_id:
            raise ValidationError({"target_type": "A table link cannot reference a guest room."})
        table_label, normalized = _normalise_table(table_number)
        target_key = f"table:{normalized}"
        target_label = (str(label or "").strip() or f"Table {table_label}")[:160]
        stored_table = table_label
    else:
        raise ValidationError({"target_type": "Target type must be ROOM or TABLE."})

    link = ServiceQRLink.objects.select_for_update().filter(target_key=target_key).first()
    if link and link.is_active:
        raise ValidationError({"target_type": "An active QR link already exists for this location. Rotate it to reprint."})

    token = _new_token()
    if link:
        link.label = target_label
        link.token_hash = _token_digest(token)
        link.is_active = True
        link.updated_by = actor
        link.save(update_fields=["label", "token_hash", "is_active", "updated_by", "updated_at"])
        return link, token, False

    link = ServiceQRLink(
        reference=_new_reference(),
        target_key=target_key,
        target_type=target_type,
        room=room,
        table_number=stored_table,
        label=target_label,
        token_hash=_token_digest(token),
        created_by=actor,
        updated_by=actor,
    )
    try:
        # Convert a concurrent create of the same room/table into a clear
        # conflict instead of leaking a database integrity error to the caller.
        with transaction.atomic():
            link.save(force_insert=True)
    except IntegrityError as exc:
        raise ValidationError({"target_type": "A QR link for this location was created concurrently; refresh and retry."}) from exc
    return link, token, True


@transaction.atomic
def rotate_service_qr_link(*, link, actor):
    """Revoke the previous printed bearer and return a replacement token."""
    link = ServiceQRLink.objects.select_for_update().get(pk=link.pk)
    token = _new_token()
    link.token_hash = _token_digest(token)
    link.is_active = True
    link.updated_by = actor
    link.save(update_fields=["token_hash", "is_active", "updated_by", "updated_at"])
    return link, token


@transaction.atomic
def revoke_service_qr_link(*, link, actor):
    """Disable a lost or retired location QR without deleting request history."""
    link = ServiceQRLink.objects.select_for_update().get(pk=link.pk)
    if link.is_active:
        link.is_active = False
        link.updated_by = actor
        link.save(update_fields=["is_active", "updated_by", "updated_at"])
    return link


def service_qr_link_for_token(token: str, *, for_update=False):
    """Return an active link for a well-formed, unguessable raw bearer token."""
    token = str(token or "").strip()
    if not 32 <= len(token) <= 128:
        return None
    queryset = ServiceQRLink.objects.filter(token_hash=_token_digest(token), is_active=True)
    if for_update:
        queryset = queryset.select_for_update()
    return queryset.select_related("room").first()


def mark_service_qr_used(*, link):
    return ServiceQRLink.objects.filter(pk=link.pk, is_active=True).update(last_used_at=timezone.now())


def public_service_url(token: str) -> str:
    base = (getattr(settings, "SERVICE_QR_FRONTEND_URL", "") or settings.FRONTEND_URL).rstrip("/")
    return f"{base}/qr-service.html#token={token}"


def build_qr_svg(url: str) -> str:
    """Generate a self-contained SVG locally; no third-party QR image service."""
    import qrcode
    from qrcode.image.svg import SvgPathImage

    code = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    code.add_data(url)
    code.make(fit=True)
    image = code.make_image(image_factory=SvgPathImage)
    output = io.BytesIO()
    image.save(output)
    return output.getvalue().decode("utf-8")


def issued_qr_payload(*, link, token):
    """The raw token is returned only in the no-store create/rotate response."""
    url = public_service_url(token)
    return {
        "reference": link.reference,
        "target_type": link.target_type,
        "target_label": link.label,
        "service_url": url,
        "qr_svg": build_qr_svg(url),
    }
