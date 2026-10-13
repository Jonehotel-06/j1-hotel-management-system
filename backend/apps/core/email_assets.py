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
import logging
import os
from urllib.parse import urlparse

from django.conf import settings

logger = logging.getLogger("apps")

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


# Hostnames that only ever resolve inside a development machine or the private
# network the application runs in. A mail client fetching the logo runs on the
# GUEST's device, so any of these produces a broken image in the delivered
# message — which is exactly how the payment receipt logo came to render as a
# grey placeholder in Gmail.
_DEV_HOSTS = frozenset({
    "localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]", "host.docker.internal",
})


def is_publicly_reachable(url: str) -> bool:
    """True only for an absolute http(s) URL on a routable, non-loopback host."""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    if not host or host in _DEV_HOSTS:
        return False
    # Bare LAN addresses are just as unreachable from a guest's phone.
    if host.startswith(("10.", "192.168.", "169.254.")):
        return False
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
        except (IndexError, ValueError):
            return False
        if 16 <= second <= 31:
            return False
    return True


def hosted_logo_url():
    """Public HTTPS URL for the logo, or ``""`` when none exists.

    Returning an empty string is deliberate: an unreachable URL is strictly
    worse than no URL, because the guest sees a broken image instead of the
    hotel's name. Callers render a wordmark fallback when this is empty.
    """
    configured = str(getattr(settings, "EMAIL_LOGO_URL", "") or "").strip()
    if configured:
        if is_publicly_reachable(configured):
            return configured
        logger.warning(
            "EMAIL_LOGO_URL (%s) is not a publicly reachable http(s) URL; "
            "guests would see a broken image. Set it to the deployed HTTPS "
            "origin of the logo asset.", configured,
        )
        return ""
    base = str(getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    if not base:
        return ""
    candidate = f"{base}/assets/icons/logo-official.png"
    if is_publicly_reachable(candidate):
        return candidate
    logger.warning(
        "FRONTEND_URL (%s) is a local/private origin, so it cannot be used as "
        "the email logo source. Set EMAIL_LOGO_URL (or a public FRONTEND_URL) "
        "before sending branded mail; falling back to an inline image.",
        base,
    )
    return ""


def logo_source(provider=None):
    """``src`` value for the logo ``<img>`` under the active provider.

    Preference order:

    1. ``cid:`` when the transport genuinely delivers inline MIME images;
    2. a publicly reachable hosted URL;
    3. ``cid:`` as the last resort even on an API transport — a CID the client
       cannot resolve is no worse than a URL it cannot fetch, and it keeps the
       attachment in the message;
    4. ``""`` so the template renders its wordmark fallback instead of a broken
       image.
    """
    from apps.notifications.providers import get_provider

    provider = provider or get_provider()
    if getattr(provider, "supports_inline_images", False) and logo_bytes():
        return f"cid:{LOGO_CID}"
    hosted = hosted_logo_url()
    if hosted:
        return hosted
    if logo_bytes():
        return f"cid:{LOGO_CID}"
    return ""


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
