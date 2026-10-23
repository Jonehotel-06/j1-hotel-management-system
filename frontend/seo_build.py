#!/usr/bin/env python3
# seo_build.py
"""J-ONE frontend — authoritative SEO layer (single source of truth).

WHAT THIS FILE OWNS
    * The per-page <title>, meta description, canonical URL, robots directive,
      Open Graph / Twitter card tags and JSON-LD structured data of every
      public page.
    * sitemap.xml and robots.txt.

WHY IT EXISTS
    Before this module the same thirteen public pages each carried a hand-typed
    head with no canonical, no structured data, inconsistent Open Graph tags and
    two of them describing the hotel as being in "Abuja". Maintaining that by
    hand across pages — and keeping the sitemap in step with it — is what let
    those errors in. One registry, applied by the existing build (build.py),
    cannot drift: the page head, the sitemap, robots.txt and the automated SEO
    tests all read from this file.

HOW IT IS APPLIED
    `build.py` calls `apply_to_html(raw, path)` for every registered page. The
    generated tags live between `<!-- SEO:BEGIN ... -->` and `<!-- SEO:END -->`
    markers and are rewritten on every build. Any hand-written/legacy
    title/description/og/twitter/canonical/robots/JSON-LD tag outside those
    markers is removed first, so a page can never end up with two <title>
    elements or conflicting canonicals.

RULES BAKED IN (see docs/SEO.md)
    * Canonical origin is https://jonehotels.com (the URL Cloudflare serves
      with HTTP 200). Cloudflare serves extensionless URLs and 307-redirects
      /rooms.html -> /rooms, so canonicals and sitemap entries are extension-
      less: pointing them at a redirecting URL would be a defect.
    * Only verified business facts are published. Nothing here invents an
      address, phone number, coordinate, opening time, rating, review, price or
      facility: values are taken from the live hotel settings payload
      (GET https://api.jonehotels.com/api/hotel/), the verified in-room amenity
      list (GET /api/rooms/) or the official details supplied by the owner.
    * No aggregateRating / review markup. There are no verified public ratings,
      so emitting them would be fabricated structured data.
    * Pages that are transactional, private or authenticated are registered as
      `indexable=False`: they get an explicit `noindex` directive and are
      excluded from the sitemap.

USAGE
    python3 seo_build.py            # rewrite every registered page + sitemap + robots
    python3 seo_build.py --check    # validate only, exit 1 on any SEO defect
"""
from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent

# ---------------------------------------------------------------------------#
# Verified business facts.
#
# Sources (checked 2026-10-23):
#   * official business details supplied by the hotel owner
#   * GET https://api.jonehotels.com/api/hotel/   (name, phone, email, country)
#   * GET https://api.jonehotels.com/api/rooms/   (in-room amenities)
#
# NOT published anywhere in this file, deliberately:
#   * latitude/longitude  — no verified coordinates exist in the repository or
#     the public API, and inventing them would place the hotel at a wrong point
#     on Google Maps;
#   * check-in/check-out times in JSON-LD — the hotel settings payload currently
#     reports 12:00 for both while the contact page states check-in from 2:00 PM.
#     A conflict has to be resolved by the owner before it is published as fact;
#   * priceRange, ratings, reviews, opening hours, social profiles — none of
#     these are verified.
# ---------------------------------------------------------------------------#
ORIGIN = "https://jonehotels.com"

SITE = {
    "origin": ORIGIN,
    "name": "J-ONE Hotel & Lodge",
    "trade_name": "J-ONE HOTEL & LODGE",
    "phone": "+2348032112874",
    "phone_display": "+234 803 211 2874",
    "email": "jonathanonu76@gmail.com",
    "street": "Plot 566 Mgbowo Street, off Ezike Street",
    "locality": "Enugu",
    "region": "Enugu State",
    "country": "NG",
    "currency": "NGN",
    "locale": "en_NG",
    "language": "en-NG",
    "logo": "/assets/icons/logo-official.png",
    "logo_width": 507,
    "logo_height": 900,
    "og_image": "/assets/images/og-j-one-hotel-enugu.jpg",
    "og_image_width": 1200,
    "og_image_height": 630,
    "og_image_alt": "J-ONE Hotel & Lodge, a hotel on Mgbowo Street in Enugu, Nigeria",
    # Photographs actually shipped with the site (real hotel imagery).
    "images": [
        "/assets/images/hero-1.jpg",
        "/assets/images/intro.jpg",
        "/assets/images/experience.jpg",
        "/assets/images/hero-3.jpg",
    ],
    # In-room amenities confirmed by the live room catalogue. Published as
    # amenityFeature only — never as a facility the hotel does not list.
    "amenities": [
        "Air conditioning",
        "Free Wi-Fi",
        "Television",
        "Refrigerator",
        "Water heater",
        "24/7 power supply",
    ],
}

# Google Maps destination for the verified address. A plain search URL (not a
# place ID) — it cannot claim to be the Business Profile, it just finds the
# address. The site replaces it with the configured google_maps_url whenever
# the owner sets one in Hotel Settings.
SITE["maps_url"] = (
    "https://www.google.com/maps/search/?api=1&query="
    + "J-ONE+Hotel+%26+Lodge%2C+Plot+566+Mgbowo+Street%2C+Enugu%2C+Enugu+State%2C+Nigeria"
)

ROBOTS_INDEX = "index, follow, max-image-preview:large, max-snippet:-1, max-video-preview:-1"
ROBOTS_NOINDEX = "noindex, follow"

# ---------------------------------------------------------------------------#
# Page registry.
#
#   canonical   extensionless public URL (Cloudflare serves it with HTTP 200)
#   sitemap     include in sitemap.xml (only for genuinely public, indexable,
#               canonical, non-redirecting pages)
#   breadcrumb  (label, path) of the page itself, when it is not the homepage
#   hotel_node  emit the full Hotel entity on this page (homepage + contact keep
#               the NAP facts on the page that actually shows them)
# ---------------------------------------------------------------------------#
PAGES: dict[str, dict] = {
    "index.html": {
        "canonical": "/",
        "title": "J-ONE Hotel & Lodge | Hotel in Enugu, Nigeria",
        "description": (
            "J-ONE Hotel & Lodge is a contemporary hotel on Mgbowo Street, Enugu, "
            "with air-conditioned rooms and suites, 24/7 power, free Wi-Fi and direct "
            "online booking."
        ),
        "indexable": True,
        "sitemap": True,
        "hotel_node": True,
    },
    "rooms.html": {
        "canonical": "/rooms",
        "title": "Rooms & Suites in Enugu | J-ONE Hotel & Lodge",
        "description": (
            "Rooms and suites at J-ONE Hotel & Lodge, Enugu: Peony Patios, Deluxe Room, "
            "J One Special and Kings and Queens Court. Air conditioning, free Wi-Fi, "
            "24/7 power."
        ),
        "indexable": True,
        "sitemap": True,
        "breadcrumb": ("Rooms & Suites", "/rooms"),
    },
    "facilities.html": {
        "canonical": "/facilities",
        "title": "Hotel Facilities in Enugu | J-ONE Hotel & Lodge",
        "description": (
            "In-room air conditioning, free Wi-Fi, TV, refrigerator, water heater and "
            "24/7 power at J-ONE Hotel & Lodge, Enugu, plus space for dining and "
            "gatherings."
        ),
        "indexable": True,
        "sitemap": True,
        "breadcrumb": ("Facilities", "/facilities"),
    },
    "gallery.html": {
        "canonical": "/gallery",
        "title": "Photo Gallery | J-ONE Hotel & Lodge, Enugu",
        "description": (
            "See the rooms, exterior, dining and facilities at J-ONE Hotel & Lodge on "
            "Mgbowo Street, Enugu, before you book your stay."
        ),
        "indexable": True,
        "sitemap": True,
        "breadcrumb": ("Gallery", "/gallery"),
    },
    "offers.html": {
        "canonical": "/offers",
        "title": "Hotel Offers & Special Rates in Enugu | J-ONE Hotel & Lodge",
        "description": (
            "Current offers and special rates at J-ONE Hotel & Lodge, Enugu. Expired "
            "offers are removed automatically — check live availability and book direct."
        ),
        "indexable": True,
        "sitemap": True,
        "breadcrumb": ("Offers", "/offers"),
    },
    "about.html": {
        "canonical": "/about",
        "title": "About J-ONE Hotel & Lodge | Enugu, Nigeria",
        "description": (
            "J-ONE Hotel & Lodge is a contemporary hotel on Mgbowo Street, Enugu, built "
            "around clean, comfortable rooms, privacy and attentive hospitality."
        ),
        "indexable": True,
        "sitemap": True,
        "breadcrumb": ("About", "/about"),
    },
    "contact.html": {
        "canonical": "/contact",
        "title": "Contact & Directions | J-ONE Hotel & Lodge, Enugu",
        "description": (
            "Contact J-ONE Hotel & Lodge: Plot 566 Mgbowo Street, off Ezike Street, "
            "Enugu State, Nigeria. Call +234 803 211 2874, email us or send an enquiry."
        ),
        "indexable": True,
        "sitemap": True,
        "breadcrumb": ("Contact", "/contact"),
        "hotel_node": True,
    },
    "policies.html": {
        "canonical": "/policies",
        "title": "Hotel Policies | J-ONE Hotel & Lodge, Enugu",
        "description": (
            "The stay policies that apply at J-ONE Hotel & Lodge, Enugu — check-in and "
            "check-out, cancellation, children, identification and house rules."
        ),
        "indexable": True,
        "sitemap": True,
        "breadcrumb": ("Hotel Policies", "/policies"),
    },
    "cancellation-policy.html": {
        "canonical": "/cancellation-policy",
        "title": "Cancellation Policy | J-ONE Hotel & Lodge, Enugu",
        "description": (
            "How cancellation works at J-ONE Hotel & Lodge, Enugu: deadlines, review by "
            "hotel staff and how refund requests are handled."
        ),
        "indexable": True,
        "sitemap": True,
        "breadcrumb": ("Cancellation Policy", "/cancellation-policy"),
    },
    "refund-policy.html": {
        "canonical": "/refund-policy",
        "title": "Refund Policy | J-ONE Hotel & Lodge, Enugu",
        "description": (
            "The refund policy for stays at J-ONE Hotel & Lodge, Enugu, including how "
            "Paystack payments are refunded once the hotel approves a request."
        ),
        "indexable": True,
        "sitemap": True,
        "breadcrumb": ("Refund Policy", "/refund-policy"),
    },
    "privacy.html": {
        "canonical": "/privacy",
        "title": "Privacy Policy | J-ONE Hotel & Lodge",
        "description": (
            "How J-ONE Hotel & Lodge collects, uses and protects the personal "
            "information you share when you enquire, book or stay with us."
        ),
        "indexable": True,
        "sitemap": True,
        "breadcrumb": ("Privacy Policy", "/privacy"),
    },
    "terms.html": {
        "canonical": "/terms",
        "title": "Terms & Conditions | J-ONE Hotel & Lodge",
        "description": (
            "The terms and conditions that govern use of the J-ONE Hotel & Lodge website "
            "and the bookings made through it."
        ),
        "indexable": True,
        "sitemap": True,
        "breadcrumb": ("Terms & Conditions", "/terms"),
    },
    # ---- Public but not search landing pages: crawlable, never in the sitemap.
    "review.html": {
        "canonical": "/review",
        "title": "Leave a Review | J-ONE Hotel & Lodge, Enugu",
        "description": (
            "Stayed with us? Tell J-ONE Hotel & Lodge how we did — your feedback goes "
            "directly to hotel management."
        ),
        "indexable": True,
        "sitemap": False,
    },
    # ---- Transactional / private: explicit noindex, never in the sitemap.
    "booking.html": {
        "canonical": "/booking",
        "title": "Book Your Stay | J-ONE Hotel & Lodge, Enugu",
        "description": (
            "Check live availability and book your stay online at J-ONE Hotel & Lodge, "
            "Enugu. Secure direct booking with instant confirmation."
        ),
        "indexable": False,
        "sitemap": False,
    },
    "booking-review.html": {
        "canonical": "/booking-review",
        "title": "Review Your Booking | J-ONE Hotel & Lodge",
        "description": "Review your booking details and continue to secure payment.",
        "indexable": False,
        "sitemap": False,
    },
    "booking-confirmation.html": {
        "canonical": "/booking-confirmation",
        "title": "Booking Confirmation | J-ONE Hotel & Lodge",
        "description": "Your booking confirmation and receipt.",
        "indexable": False,
        "sitemap": False,
    },
    "cancellation-result.html": {
        "canonical": "/cancellation-result",
        "title": "Cancellation / Refund Status | J-ONE Hotel & Lodge",
        "description": "Check the status of a cancellation or refund request.",
        "indexable": False,
        "sitemap": False,
    },
    "payment-verify.html": {
        "canonical": "/payment-verify",
        "title": "Payment Verification | J-ONE Hotel & Lodge",
        "description": "Verifying your payment.",
        "indexable": False,
        "sitemap": False,
    },
    "my-bookings.html": {
        "canonical": "/my-bookings",
        "title": "Your Stays | J-ONE Hotel & Lodge",
        "description": "Find your bookings, references and receipts.",
        "indexable": False,
        "sitemap": False,
    },
    "login.html": {
        "canonical": "/login",
        "title": "Staff Sign In | J-ONE Hotel & Lodge",
        "description": "Staff sign in.",
        "indexable": False,
        "sitemap": False,
    },
    "register.html": {
        "canonical": "/register",
        "title": "Guest Checkout | J-ONE Hotel & Lodge",
        "description": "Complete your booking.",
        "indexable": False,
        "sitemap": False,
    },
    "room-details.html": {
        "canonical": "/room-details",
        "title": "Room Details | J-ONE Hotel & Lodge, Enugu",
        "description": (
            "Room details, amenities and live availability at J-ONE Hotel & Lodge, "
            "Enugu."
        ),
        "indexable": False,
        "sitemap": False,
    },
    "offline.html": {
        "canonical": "/offline",
        "title": "You're Offline | J-ONE Hotel & Lodge",
        "description": "You are offline.",
        "indexable": False,
        "sitemap": False,
    },
}

# Error pages are intentionally not registered above (they already ship a
# noindex directive and must never appear in a sitemap).

# Paths robots.txt must keep out of the crawler. Prefix matching is used by
# crawlers, so one extensionless rule also covers the legacy `.html` form.
ROBOTS_DISALLOW = [
    "/dashboard",          # authenticated staff console
    "/portal",             # verified-email guest portal
    "/login",              # staff sign-in
    "/register",           # guest checkout shell
    "/qr-service",         # bearer-link guest request form (URL carries a token)
]
ROBOTS_NOTE = {
    "/dashboard": "authenticated staff console",
    "/portal": "verified-email guest portal",
    "/login": "staff sign-in",
    "/register": "guest checkout shell",
    "/qr-service": "bearer-link guest request form (URL carries a token)",
}

SITEMAP_PATH = "/sitemap.xml"
ROBOTS_PATH = "/robots.txt"

SEO_BEGIN = "<!-- SEO:BEGIN — generated by frontend/seo_build.py; edit PAGES there, not here -->"
SEO_END = "<!-- SEO:END -->"

_MANAGED_BLOCK = re.compile(
    re.escape("<!-- SEO:BEGIN") + r".*?" + re.escape(SEO_END) + r"[ \t]*\n?",
    re.S,
)

# Legacy/hand-written tag forms this module now owns. Removed before the managed
# block is inserted so a page can never carry two titles or two canonicals.
_OWNED_TAGS = [
    re.compile(r"[ \t]*<title>.*?</title>[ \t]*\n?", re.S | re.I),
    re.compile(r'[ \t]*<meta\s+name="description"[^>]*>[ \t]*\n?', re.I),
    re.compile(r'[ \t]*<meta\s+name="robots"[^>]*>[ \t]*\n?', re.I),
    re.compile(r'[ \t]*<link\s+rel="canonical"[^>]*>[ \t]*\n?', re.I),
    re.compile(r'[ \t]*<meta\s+property="og:[^"]*"[^>]*>[ \t]*\n?', re.I),
    re.compile(r'[ \t]*<meta\s+name="twitter:[^"]*"[^>]*>[ \t]*\n?', re.I),
    re.compile(
        r'[ \t]*<script\s+type="application/ld\+json"[^>]*>.*?</script>[ \t]*\n?',
        re.S | re.I,
    ),
]


# ---------------------------------------------------------------------------#
# Helpers
# ---------------------------------------------------------------------------#
def absolute(path: str) -> str:
    """Turn a site-relative path into an absolute canonical-origin URL."""
    if path.startswith(("http://", "https://")):
        return path
    return ORIGIN + ("/" if path == "/" else "/" + path.lstrip("/"))


def canonical_url(page: str) -> str:
    return absolute(PAGES[page]["canonical"])


def _attr(value: str) -> str:
    return html.escape(str(value), quote=True)


def _json_ld(obj) -> str:
    """Serialise JSON-LD safely for inline embedding."""
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    # "</" inside a <script> would terminate the element early.
    return raw.replace("</", "<\\/")


def hotel_node() -> dict:
    return {
        "@type": "Hotel",
        "@id": ORIGIN + "/#hotel",
        "name": SITE["name"],
        "alternateName": ["J-ONE Hotel and Lodge", "J One Hotel", "Jone Hotels"],
        "url": ORIGIN + "/",
        "telephone": SITE["phone"],
        "email": SITE["email"],
        "image": [absolute(p) for p in SITE["images"]],
        "logo": {
            "@type": "ImageObject",
            "url": absolute(SITE["logo"]),
            "width": SITE["logo_width"],
            "height": SITE["logo_height"],
        },
        "address": {
            "@type": "PostalAddress",
            "streetAddress": SITE["street"],
            "addressLocality": SITE["locality"],
            "addressRegion": SITE["region"],
            "addressCountry": SITE["country"],
        },
        "currenciesAccepted": SITE["currency"],
        "hasMap": SITE["maps_url"],
        "amenityFeature": [
            {"@type": "LocationFeatureSpecification", "name": name, "value": True}
            for name in SITE["amenities"]
        ],
        "areaServed": {"@type": "City", "name": SITE["locality"]},
    }


def website_node() -> dict:
    return {
        "@type": "WebSite",
        "@id": ORIGIN + "/#website",
        "url": ORIGIN + "/",
        "name": SITE["name"],
        "inLanguage": SITE["language"],
        "publisher": {"@id": ORIGIN + "/#hotel"},
    }


def webpage_node(page: str) -> dict:
    spec = PAGES[page]
    url = canonical_url(page)
    node = {
        "@type": "WebPage",
        "@id": url + "#webpage",
        "url": url,
        "name": _plain_title(spec["title"]),
        "description": spec["description"],
        "isPartOf": {"@id": ORIGIN + "/#website"},
        "about": {"@id": ORIGIN + "/#hotel"},
        "inLanguage": SITE["language"],
        "primaryImageOfPage": {
            "@type": "ImageObject",
            "url": absolute(spec.get("og_image") or SITE["og_image"]),
        },
    }
    return node


def breadcrumb_node(page: str) -> dict:
    spec = PAGES[page]
    items = [{"@type": "ListItem", "position": 1, "name": SITE["name"], "item": ORIGIN + "/"}]
    if page != "index.html":
        label, path = spec.get("breadcrumb", (_plain_title(spec["title"]), spec["canonical"]))
        items.append({"@type": "ListItem", "position": 2, "name": label, "item": absolute(path)})
    return {"@type": "BreadcrumbList", "@id": canonical_url(page) + "#breadcrumb", "itemListElement": items}


def _plain_title(title: str) -> str:
    return html.unescape(title)


def structured_data(page: str) -> dict:
    spec = PAGES[page]
    graph = []
    if page == "index.html":
        graph.append(hotel_node())
        graph.append(website_node())
    elif spec.get("hotel_node"):
        graph.append(hotel_node())
    graph.append(webpage_node(page))
    if page != "index.html":
        graph.append(breadcrumb_node(page))
    return {"@context": "https://schema.org", "@graph": graph}


# ---------------------------------------------------------------------------#
# Head generation
# ---------------------------------------------------------------------------#
def render_block(page: str) -> str:
    spec = PAGES[page]
    url = canonical_url(page)
    title = spec["title"]
    description = spec["description"]
    image = absolute(spec.get("og_image") or SITE["og_image"])
    image_alt = spec.get("og_image_alt") or SITE["og_image_alt"]
    robots = ROBOTS_INDEX if spec.get("indexable", True) else ROBOTS_NOINDEX

    lines = [
        SEO_BEGIN,
        f"<title>{_attr(title)}</title>",
        f'<meta name="description" content="{_attr(description)}">',
        f'<meta name="robots" content="{_attr(robots)}">',
        f'<link rel="canonical" href="{_attr(url)}">',
        '<meta property="og:type" content="website">',
        f'<meta property="og:site_name" content="{_attr(SITE["name"])}">',
        f'<meta property="og:locale" content="{_attr(SITE["locale"])}">',
        f'<meta property="og:title" content="{_attr(title)}">',
        f'<meta property="og:description" content="{_attr(description)}">',
        f'<meta property="og:url" content="{_attr(url)}">',
        f'<meta property="og:image" content="{_attr(image)}">',
        f'<meta property="og:image:type" content="image/jpeg">',
        f'<meta property="og:image:width" content="{SITE["og_image_width"]}">',
        f'<meta property="og:image:height" content="{SITE["og_image_height"]}">',
        f'<meta property="og:image:alt" content="{_attr(image_alt)}">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{_attr(title)}">',
        f'<meta name="twitter:description" content="{_attr(description)}">',
        f'<meta name="twitter:image" content="{_attr(image)}">',
        f'<script type="application/ld+json">{_json_ld(structured_data(page))}</script>',
        SEO_END,
    ]
    return "\n".join(lines)


def apply_to_html(raw: str, page: str) -> str:
    """Rewrite one page's <head> SEO signals. Idempotent."""
    if page not in PAGES:
        return raw

    head_start = raw.find("<head>")
    head_end = raw.find("</head>")
    if head_start == -1 or head_end == -1:
        raise SystemExit(f"{page}: no <head> element to apply SEO to")

    head = raw[head_start:head_end]
    tail = raw[head_end:]

    head = _MANAGED_BLOCK.sub("", head)
    for pattern in _OWNED_TAGS:
        head = pattern.sub("", head)
    # Collapse the blank lines the removals can leave behind.
    head = re.sub(r"\n{3,}", "\n\n", head).rstrip() + "\n"

    block = render_block(page)
    # Insert at the end of <head>: everything that already exists (charset in
    # the first bytes, viewport, theme boot, stylesheet) keeps its position.
    head = head + block + "\n"
    return raw[:head_start] + head + tail


# ---------------------------------------------------------------------------#
# sitemap.xml / robots.txt
# ---------------------------------------------------------------------------#
def sitemap_xml() -> str:
    urls = [
        canonical_url(page)
        for page, spec in PAGES.items()
        if spec.get("sitemap") and spec.get("indexable", True)
    ]
    # Keep the homepage first, the rest in registry order (already meaningful).
    urls = [ORIGIN + "/"] + [u for u in urls if u != ORIGIN + "/"]
    body = "\n".join(f"  <url><loc>{html.escape(u)}</loc></url>" for u in urls)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!-- J-ONE Hotel & Lodge — generated by frontend/seo_build.py. Do not edit by hand. -->\n"
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{body}\n"
        "</urlset>\n"
    )


def robots_txt() -> str:
    lines = [
        "# robots.txt — J-ONE Hotel & Lodge (https://jonehotels.com)",
        "# Generated by frontend/seo_build.py. Do not edit by hand.",
        "#",
        "# Public hotel pages, CSS, JS, images, sitemap.xml and robots.txt are all",
        "# crawlable: blocking them would stop Google rendering the site correctly.",
        "# robots.txt is not an access control — every private route below is also",
        "# protected by authentication and an X-Robots-Tag/robots noindex directive.",
        "",
        "User-agent: *",
        "Allow: /",
        "",
    ]
    for path in ROBOTS_DISALLOW:
        note = ROBOTS_NOTE.get(path)
        if note:
            lines.append(f"# {note}")
        lines.append(f"Disallow: {path}")
    lines += ["", f"Sitemap: {absolute(SITEMAP_PATH)}", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------#
# Validation — also run standalone with --check
# ---------------------------------------------------------------------------#
def _strip_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value)


def validate(root: Path = ROOT) -> list[str]:
    problems: list[str] = []
    titles: dict[str, str] = {}
    descriptions: dict[str, str] = {}

    for page, spec in PAGES.items():
        path = root / page
        if not path.exists():
            problems.append(f"{page}: registered page does not exist")
            continue

        raw = path.read_text(encoding="utf-8")
        title = _plain_title(spec["title"])
        description = html.unescape(spec["description"])

        if not title.strip():
            problems.append(f"{page}: empty title")
        if len(title) > 70:
            problems.append(f"{page}: title is {len(title)} chars (keep <= 70)")
        if title in titles:
            problems.append(f"{page}: duplicate title (also used by {titles[title]})")
        else:
            titles[title] = page

        if spec.get("indexable", True):
            if not (50 <= len(description) <= 165):
                problems.append(
                    f"{page}: meta description is {len(description)} chars (want 50–165)"
                )
        elif not description.strip():
            problems.append(f"{page}: empty meta description")
        if description in descriptions:
            problems.append(f"{page}: duplicate meta description (also {descriptions[description]})")
        else:
            descriptions[description] = page

        url = canonical_url(page)
        if not url.startswith("https://jonehotels.com/"):
            problems.append(f"{page}: canonical is not on the canonical origin: {url}")
        if url.endswith(".html") or "?" in url or url.endswith("//"):
            problems.append(f"{page}: canonical must be a clean extensionless URL: {url}")
        if "www." in url:
            problems.append(f"{page}: canonical must not use the www host: {url}")

        # The rendered head must contain exactly one of each critical tag.
        for needle, label in (
            (f'<link rel="canonical" href="{url}">', "canonical"),
            (f"<title>{_attr(spec['title'])}</title>", "title"),
        ):
            count = raw.count(needle)
            if count != 1:
                problems.append(f"{page}: expected exactly one {label}, found {count}")
        if raw.count("<title>") != 1:
            problems.append(f"{page}: multiple <title> elements")
        if raw.count('rel="canonical"') != 1:
            problems.append(f"{page}: multiple canonical links")

        expected_robots = ROBOTS_INDEX if spec.get("indexable", True) else ROBOTS_NOINDEX
        if f'<meta name="robots" content="{expected_robots}">' not in raw:
            problems.append(f"{page}: robots directive is not {expected_robots!r}")
        if spec.get("indexable", True) and "noindex" in raw.lower():
            problems.append(f"{page}: indexable page carries a noindex directive")

        # Open Graph sanity
        if f'<meta property="og:url" content="{url}">' not in raw:
            problems.append(f"{page}: og:url missing or not the canonical URL")
        if 'name="twitter:card"' not in raw:
            problems.append(f"{page}: twitter:card missing")

        # JSON-LD must be present, valid JSON, and match the registered facts.
        blocks = re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', raw, re.S
        )
        if len(blocks) != 1:
            problems.append(f"{page}: expected exactly one JSON-LD block, found {len(blocks)}")
        for block in blocks:
            try:
                data = json.loads(block.replace("<\\/", "</"))
            except json.JSONDecodeError as exc:
                problems.append(f"{page}: invalid JSON-LD ({exc})")
                continue
            graph = data.get("@graph") or [data]
            for node in graph:
                if node.get("@type") == "Hotel":
                    if node.get("name") != SITE["name"]:
                        problems.append(f"{page}: Hotel.name disagrees with the brand")
                    if node.get("telephone") != SITE["phone"]:
                        problems.append(f"{page}: Hotel.telephone disagrees with the verified number")
                    addr = node.get("address") or {}
                    if addr.get("addressLocality") != SITE["locality"]:
                        problems.append(f"{page}: Hotel address locality is wrong")
                    if addr.get("addressCountry") != SITE["country"]:
                        problems.append(f"{page}: Hotel address country is wrong")
                if "aggregateRating" in json.dumps(node) or '"review"' in json.dumps(node):
                    problems.append(f"{page}: fabricated rating/review markup is not allowed")

    # og:image must exist on disk (a broken sharing image is a silent defect).
    og_path = root / SITE["og_image"].lstrip("/")
    if not og_path.exists():
        problems.append(f"missing Open Graph image {SITE['og_image']}")

    # Sitemap must be exactly the indexable, sitemap-flagged pages.
    sitemap_file = root / SITEMAP_PATH.lstrip("/")
    if not sitemap_file.exists():
        problems.append("sitemap.xml is missing")
    else:
        xml = sitemap_file.read_text(encoding="utf-8")
        if not xml.startswith('<?xml version="1.0" encoding="UTF-8"?>'):
            problems.append("sitemap.xml is not a valid XML document declaration")
        if 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"' not in xml:
            problems.append("sitemap.xml is missing the sitemap namespace")
        locs = re.findall(r"<loc>(.*?)</loc>", xml)
        if len(locs) != len(set(locs)):
            problems.append("sitemap.xml contains duplicate <loc> entries")
        expected = {canonical_url(p) for p, s in PAGES.items() if s.get("sitemap") and s.get("indexable", True)}
        for loc in locs:
            if loc not in expected:
                problems.append(f"sitemap.xml lists an unexpected URL: {loc}")
            if loc.endswith(".html") or "?" in loc:
                problems.append(f"sitemap.xml lists a redirecting/query URL: {loc}")
        missing = expected - set(locs)
        for loc in sorted(missing):
            problems.append(f"sitemap.xml is missing {loc}")
        # Nothing noindex may be listed.
        for page, spec in PAGES.items():
            if spec.get("indexable", True):
                continue
            if canonical_url(page) in locs:
                problems.append(f"sitemap.xml lists a noindex page: {page}")

    # robots.txt
    robots_file = root / ROBOTS_PATH.lstrip("/")
    if not robots_file.exists():
        problems.append("robots.txt is missing")
    else:
        robots = robots_file.read_text(encoding="utf-8")
        if f"Sitemap: {absolute(SITEMAP_PATH)}" not in robots:
            problems.append("robots.txt does not declare the sitemap")
        if re.search(r"^Disallow: /\s*$", robots, re.M):
            problems.append("robots.txt disallows the whole site")
        for path in ROBOTS_DISALLOW:
            if f"Disallow: {path}" not in robots:
                problems.append(f"robots.txt does not disallow {path}")
        for page, spec in PAGES.items():
            if spec.get("indexable", True) and spec.get("sitemap"):
                canonical = spec["canonical"]
                for path in ROBOTS_DISALLOW:
                    if canonical == path or canonical.startswith(path.rstrip("/") + "/"):
                        problems.append(f"robots.txt blocks the sitemap page {canonical}")
        for asset in ("/css/", "/js/", "/assets/", "/favicon/"):
            if re.search(r"^Disallow: %s" % re.escape(asset.strip("/")), robots, re.M):
                problems.append(f"robots.txt blocks required assets: {asset}")

    return problems


# ---------------------------------------------------------------------------#
# Writer
# ---------------------------------------------------------------------------#
def apply_all(root: Path = ROOT) -> list[str]:
    changed = []
    for page in PAGES:
        path = root / page
        if not path.exists():
            continue
        raw = path.read_text(encoding="utf-8")
        updated = apply_to_html(raw, page)
        if updated != raw:
            path.write_text(updated, encoding="utf-8")
            changed.append(page)
    for name, content in (("sitemap.xml", sitemap_xml()), ("robots.txt", robots_txt())):
        path = root / name
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
            changed.append(name)
    return changed


def main(argv: list[str]) -> int:
    if "--check" in argv:
        problems = validate(ROOT)
        for problem in problems:
            print("  [SEO FAIL] " + problem)
        print("SEO CHECK: " + ("FAIL" if problems else "ALL OK"))
        return 1 if problems else 0
    changed = apply_all(ROOT)
    problems = validate(ROOT)
    print(f"SEO: rewrote {len(changed)} file(s): {', '.join(changed) if changed else 'none'}")
    for problem in problems:
        print("  [SEO FAIL] " + problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
