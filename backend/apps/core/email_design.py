# apps/core/email_design.py
"""Shared J-ONE HOTEL & LODGE branded email builder (presentation only).

Every guest-facing transactional email that is not a full receipt or review
invitation (booking-pending, booking cancellation, password reset, refund
updates, cancellation-request lifecycle notices) renders through this one
builder so the whole product speaks with a single, professional visual
identity — the same header lockup, typography, detail styling and footer the
receipt and review-invitation templates already use.

The builder produces BOTH bodies for a proper ``multipart/alternative`` email:

* a plain-text body (assembled in code, so spacing is always clean); and
* a table-based, email-safe HTML body rendered from
  ``apps/core/templates/emails/notice.html`` (inline critical styles, MSO
  button fallback, 600px container, dark-mode hints — no JavaScript, no
  external CSS, no framework).

No business logic lives here: callers pass already-computed, display-ready
strings. All dynamic values are auto-escaped by the Django template engine.
"""
from django.template.loader import render_to_string


def render_notice_email(
    *,
    category,
    title,
    greeting,
    paragraphs,
    details=None,
    cta_label="",
    cta_url="",
    footnote="",
    preheader="",
):
    """Return ``(text_body, html_body)`` for a branded guest notice.

    ``category``    small-caps label shown in the header (e.g. "Booking Update").
    ``title``       the main heading.
    ``greeting``    e.g. ``"Hello Ada,"``.
    ``paragraphs``  iterable of body paragraphs (plain strings).
    ``details``     optional iterable of ``{"label": ..., "value": ...}`` rows
                    rendered as a bordered summary card; rows with an empty
                    value are dropped.
    ``cta_label`` / ``cta_url``  optional single call-to-action button.
    ``footnote``    optional small print under the body.
    ``preheader``   optional inbox preview text (defaults to the title).
    """
    # Lazy imports: presentation helper must not force app loading order.
    from apps.core.email_assets import logo_context
    from apps.hotel.models import HotelSettings

    hotel = HotelSettings.get_settings()
    hotel_location = ", ".join(
        part for part in (hotel.address, hotel.city, hotel.state, hotel.country) if part
    )
    clean_paragraphs = [str(p) for p in (paragraphs or []) if p]
    clean_details = [
        {"label": str(d.get("label", "")), "value": str(d.get("value", ""))}
        for d in (details or [])
        if d and str(d.get("value", "")).strip()
    ]

    context = {
        "hotel_name": hotel.hotel_name,
        "hotel_location": hotel_location,
        "hotel_phone": hotel.phone,
        "hotel_email": hotel.email,
        **logo_context(),
        "category": category,
        "title": title,
        "greeting": greeting,
        "paragraphs": clean_paragraphs,
        "details": clean_details,
        "cta_label": cta_label,
        "cta_url": cta_url,
        "footnote": footnote,
        "preheader": preheader or title,
    }
    html_body = render_to_string("emails/notice.html", context)

    # Plain-text alternative — required fallback for text-only clients.
    lines = []
    if greeting:
        lines += [greeting, ""]
    for paragraph in clean_paragraphs:
        lines += [paragraph, ""]
    if clean_details:
        for row in clean_details:
            lines.append(f"{row['label']}: {row['value']}")
        lines.append("")
    if cta_url:
        lines += [f"{cta_label}: {cta_url}" if cta_label else cta_url, ""]
    if footnote:
        lines += [footnote, ""]
    lines.append(hotel.hotel_name)
    contact = " · ".join(part for part in (hotel.phone, hotel.email) if part)
    if contact:
        lines.append(contact)
    text_body = "\n".join(lines)

    return text_body, html_body
