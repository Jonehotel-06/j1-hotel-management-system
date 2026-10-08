# apps/core/email_assets.py
"""How the J-ONE logo is referenced inside transactional email HTML.

The bundled mark is PORTRAIT (507 x 900 px), so any square width/height pair
distorts it. ``LOGO_WIDTH``/``LOGO_HEIGHT`` keep the real aspect ratio at a
readable email size, and every template must use them.

Reference strategy depends on the active provider:

* providers that genuinely deliver inline MIME images (SMTP, SendGrid,
  Mailgun, Postmark) get ``cid:jone-logo`` plus a real inline attachment;
* providers that do not (Brevo's v3 API) get an HTTPS URL. A ``data:`` URI is
  never used — Gmail strips them, which is exactly why the logo appeared
  broken in delivered receipts.
"""
import os

from django.conf import settings

LOGO_CID = "jone-logo"
LOGO_FILENAME = "jone-logo.png"
LOGO_MIMETYPE = "image/png"

# Real asset is 507 x 900; this pair preserves that ratio (0.563).
LOGO_WIDTH = 25
LOGO_HEIGHT = 44

LOGO_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bookings", "services", "assets", "logo-official.png",
)


def logo_bytes():
    """Raw PNG bytes, or None when the asset is unreadable."""
    try:
        with open(LOGO_PATH, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def hosted_logo_url():
    """Public HTTPS URL for the logo (frontend-hosted, overridable)."""
    configured = str(getattr(settings, "EMAIL_LOGO_URL", "") or "").strip()
    if configured:
        return configured
    base = str(getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    return f"{base}/assets/icons/logo-official.png" if base else ""


def logo_source(provider=None):
    """``src`` value for the logo <img> under the active provider."""
    from apps.notifications.providers import get_provider

    provider = provider or get_provider()
    if getattr(provider, "supports_inline_images", False) and logo_bytes():
        return f"cid:{LOGO_CID}"
    return hosted_logo_url()


def logo_context(provider=None):
    """Template context every branded email template expects."""
    return {
        "logo_cid": LOGO_CID,
        "logo_src": logo_source(provider),
        "logo_width": LOGO_WIDTH,
        "logo_height": LOGO_HEIGHT,
    }


def inline_logo_attachments(html_body, provider=None):
    """Inline image parts needed by ``html_body``, for the active provider."""
    if f"cid:{LOGO_CID}" not in (html_body or ""):
        return []
    content = logo_bytes()
    if not content:
        return []
    return [(LOGO_CID, LOGO_FILENAME, content, LOGO_MIMETYPE)]
