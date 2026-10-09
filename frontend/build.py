#!/usr/bin/env python3
# build.py
"""J-ONE frontend builder.

Responsibilities
1. Shared chrome: the public header / footer / mobile nav live ONCE in
   /components. The build inlines them into every page author-time, so the
   deployed site is fully self-contained with zero runtime component-request
   overhead while keeping one source of truth. Every public page contains
   these markers, replaced at build time:
       <!--HEADER-->   → components/header.html
       <!--FOOTER-->   → components/footer.html
       <!--MOBILE-->   → components/mobile-nav.html
2. Versioning (single source of truth: version.json):
       * `--bump major|minor|patch` or `--version X.Y.Z` releases a new
         version: version.json, js/version.js and the service-worker
         CACHE_VERSION are updated together.
       * EVERY local .js/.css reference in every HTML page (public, dashboard,
         portal, and bearer-link QR guest page) is stamped `?v=<version>` so a release can never serve
         stale cached assets next to fresh HTML.
       * js/version.js + js/update-checker.js are injected into every page so
         visitors on an older cached deployment are told a new version is
         available (accessible modal, never shown mid-booking/payment).
       * Portal pages deliberately keep their own minimal shell and narrow
         verified-email session bootstrap. The builder includes and stamps them
         at their `../` asset depth, but never injects PWA/update-controller
         scripts that could reload and discard an in-memory portal session.

Usage:
    python3 build.py                 # rebuild chrome + (re)stamp current version
    python3 build.py --bump patch    # release 1.2.3 → 1.2.4 and rebuild
    python3 build.py --version 2.0.0 # release an explicit version and rebuild
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
VERSION_FILE = ROOT / "version.json"
VERSION_JS = ROOT / "js" / "version.js"
SW_FILE = ROOT / "sw.js"

HEADER = (ROOT / "components/header.html").read_text(encoding="utf-8")
FOOTER = (ROOT / "components/footer.html").read_text(encoding="utf-8")
MOBILE = (ROOT / "components/mobile-nav.html").read_text(encoding="utf-8")

THEME_BOOT = (
    '<script>(function(){var t;try{t=JSON.parse(localStorage.getItem("jone.theme"))}'
    'catch(e){}if(t!=="dark"&&t!=="light"){t=(window.matchMedia&&'
    'window.matchMedia("(prefers-color-scheme: dark)").matches)?"dark":"light"}'
    'document.documentElement.setAttribute("data-theme",t);})();</script>'
)

# Matches any prior injected theme-boot snippet (new setAttribute-only form or the
# old classList.remove form) so re-runs are idempotent, and strips the old
# preload-theme class add so it never hides the body.
_THEME_STRIP = re.compile(
    r'<script>\(function\(\)\{var t;try\{t=JSON\.parse\(localStorage\.getItem\("jone\.theme"\)\)\}'
    r'catch\(e\)\{\}if\(t!=="dark"&&t!=="light"\)\{t=\(window\.matchMedia&&'
    r'window\.matchMedia\("\(prefers-color-scheme: dark\)"\)\.matches\)\?"dark":"light"\}'
    r'document\.documentElement\.setAttribute\("data-theme",t\);.*?\}\)\(\);</script>',
    re.S,
)
_PRELOAD_STRIP = re.compile(
    r'<script>document\.documentElement\.classList\.add\("preload-theme"\);</script>',
)

JS_BLOCK = """<script src="js/runtime-config.js"></script>
<script src="js/config.js"></script>
<script src="js/utils.js"></script>
<script src="js/icons.js"></script>
<script src="js/api.js"></script>
<script src="js/theme.js"></script>
<script src="js/ui.js"></script>
<script src="js/hotel-data.js"></script>
<script src="js/navigation.js"></script>
<script src="js/contact.js"></script>
<script>JONE.ui.initChrome(); JONE.nav.init();</script>
<script src="js/pwa.js"></script>"""

# name -> absolute root-relative path expected to exist (mirrors spec structure)
PUBLIC_PAGES = [
    "index.html", "about.html", "rooms.html", "room-details.html",
    "facilities.html", "gallery.html", "offers.html",
    "booking.html", "booking-review.html", "booking-confirmation.html",
    "contact.html", "policies.html", "privacy.html", "terms.html",
    "cancellation-policy.html", "refund-policy.html", "review.html",
    "login.html", "register.html", "my-bookings.html", "payment-verify.html",
    "cancellation-result.html", "offline.html", "404.html", "403.html", "500.html",
]

# The portal and bearer-link guest pages are intentionally isolated from
# public chrome, persistent browser storage, and reload-capable PWA/update
# scripts. They still receive release asset stamps.
PORTAL_PAGES = ["portal/login.html", "portal/index.html"]
QR_SERVICE_PAGES = ["qr-service.html"]

PWA_HEAD = (
    '<link rel="manifest" href="/manifest.webmanifest">\n'
    '<meta name="apple-mobile-web-app-capable" content="yes">\n'
    '<meta name="mobile-web-app-capable" content="yes">\n'
    '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">\n'
    '<meta name="apple-mobile-web-app-title" content="J-ONE Hotel">'
)


# ---------------------------------------------------------------------------#
# Version management — version.json is the single source of truth.
# ---------------------------------------------------------------------------#
def read_version():
    data = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
    version = str(data.get("version") or "").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version or ""):
        raise SystemExit(f"version.json has an invalid version: {version!r} (expected X.Y.Z)")
    return version


def write_version(version):
    VERSION_FILE.write_text(
        json.dumps({"version": version}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    VERSION_JS.write_text(
        "/* ==========================================================================\n"
        "   J-ONE HOTEL & LODGE — frontend version marker (GENERATED by build.py).\n"
        "\n"
        "   This file is regenerated from version.json on every build; do not edit\n"
        "   it by hand. js/update-checker.js compares window.JONE_VERSION against\n"
        "   the live version.json to detect that a newer deployment is available.\n"
        "   ========================================================================== */\n"
        f'window.JONE_VERSION = "{version}";\n',
        encoding="utf-8",
    )
    # Keep the service-worker cache namespace in lockstep with the release so
    # every deploy invalidates old precaches.
    sw = SW_FILE.read_text(encoding="utf-8")
    sw, n = re.subn(r'CACHE_VERSION = "[^"]+"', f'CACHE_VERSION = "jone-v{version}"', sw, count=1)
    if n == 0:
        print("WARNING: CACHE_VERSION not found in sw.js", file=sys.stderr)
    else:
        SW_FILE.write_text(sw, encoding="utf-8")


def bump_version(current, mode):
    major, minor, patch = (int(x) for x in current.split("."))
    if mode == "major":
        return f"{major + 1}.0.0"
    if mode == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


# ---------------------------------------------------------------------------#
# Version stamping + runtime wiring on every HTML page.
# ---------------------------------------------------------------------------#
_ASSET_RE = re.compile(
    r'((?:src|href)=")([^"?#]+?\.(?:js|css))(?:\?[^"#]*)?(")',
    re.IGNORECASE,
)


def _stamp_assets(raw, version):
    """Stamp (or refresh) ?v=<version> on every LOCAL js/css reference."""

    def repl(m):
        prefix, path, suffix = m.group(1), m.group(2), m.group(3)
        if path.startswith(("http://", "https://", "//", "data:")):
            return m.group(0)
        base, _, _q = path.partition("?")
        return f"{prefix}{base}?v={version}{suffix}"

    return _ASSET_RE.sub(repl, raw)


def _ensure_version_scripts(raw, depth):
    """Idempotently load js/version.js + js/update-checker.js on every page."""
    if "js/update-checker.js" not in raw:
        tags = (
            f'<script src="{depth}js/version.js"></script>\n'
            f'<script src="{depth}js/update-checker.js"></script>\n'
        )
        raw = raw.replace("</body>", tags + "</body>", 1)
    return raw


def _ensure_pwa(raw, depth=""):
    """Idempotently guarantee the PWA head metadata and js/pwa.js are present.

    The manifest is referenced with a ROOT-ABSOLUTE path so the very same tag is
    correct at every directory depth (root pages and /dashboard/ alike).
    """
    if 'rel="manifest"' not in raw:
        m = re.search(r'<link rel="apple-touch-icon"[^>]*>', raw)
        anchor = m.group(0) if m else None
        if anchor is None:
            m = re.search(r'<link rel="stylesheet" href="[^"]*main\.css">', raw)
            anchor = m.group(0) if m else None
        if anchor:
            raw = raw.replace(anchor, anchor + "\n" + PWA_HEAD, 1)
    if 'name="theme-color"' not in raw:
        raw = raw.replace('<link rel="icon"',
                          '<meta name="theme-color" content="#373435">\n<link rel="icon"', 1)
    tag = '<script src="%sjs/pwa.js"></script>' % depth
    if "js/pwa.js" not in raw:
        raw = raw.replace("</body>", tag + "\n</body>", 1)
    return raw


def build(path, version):
    f = ROOT / path
    if not f.exists():
        return False
    raw = f.read_text(encoding="utf-8")
    is_portal = path.startswith("portal/")
    is_memory_only_page = is_portal or path in QR_SERVICE_PAGES
    depth = "../" if path.startswith(("dashboard/", "portal/")) else ""

    # --- Re-inject shared chrome idempotently -------------------------------
    # After the first build the page has chrome baked in and no <!--HEADER-->
    # markers remain, so edits to components/ wouldn't propagate. We instead
    # anchor on stable boundaries and swap the chrome region each run. Portal
    # and bearer-link guest pages are intentionally excluded: their smaller,
    # purpose-built shells are part of the credential-handling boundary.
    if not is_memory_only_page and 'class="site-footer"' in raw:
        # Header region: everything between <body> and <main id="main">.
        def repl_head(m):
            return m.group(1) + "\n" + HEADER + "\n" + m.group(3)
        raw = re.sub(r'(<body>)(.*?)(<main id="main">)', repl_head, raw, count=1, flags=re.S)
        # Footer + mobile region: anchor on the runtime bootstrap when present,
        # or legacy config.js otherwise. Query-stamped paths are accepted so a
        # plain rebuild stays idempotent after a release. The runtime bootstrap
        # must precede config.js on every fresh and already-built page.
        def repl_foot(m):
            anchor = m.group(3)
            if "runtime-config.js" not in anchor:
                anchor = '<script src="js/runtime-config.js"></script>\n' + anchor
            return m.group(1) + "\n" + FOOTER + "\n" + MOBILE + "\n" + anchor
        raw = re.sub(
            r'(</main>)(.*?)(<script src="js/(?:runtime-config|config)\.js(?:\?[^"\s]*)?"></script>)',
            repl_foot,
            raw,
            count=1,
            flags=re.S,
        )

    if not is_memory_only_page:
        # Also handle freshly-authored public pages that still use markers.
        raw = raw.replace("<!--HEADER-->", HEADER)
        raw = raw.replace("<!--FOOTER-->", FOOTER)
        raw = raw.replace("<!--MOBILE-->", MOBILE)

        # Idempotent theme boot: drop any prior injected snippets with a literal
        # string replace, then inject ONE snippet inside <head> before first paint.
        # (Strip the newline added alongside it too, so repeated builds never
        # accumulate blank lines.)
        for _ in range(8):
            if THEME_BOOT not in raw:
                break
            raw = raw.replace("\n" + THEME_BOOT, "").replace(THEME_BOOT, "")
        old_boot = (
            '<script>(function(){var t;try{t=JSON.parse(localStorage.getItem("jone.theme"))}'
            'catch(e){}if(t!=="dark"&&t!=="light"){t=(window.matchMedia&&'
            'window.matchMedia("(prefers-color-scheme: dark)").matches)?"dark":"light"}'
            'document.documentElement.setAttribute("data-theme",t);'
            'document.documentElement.classList.remove("preload-theme");})();</script>'
        )
        raw = raw.replace(old_boot, "")
        raw = _PRELOAD_STRIP.sub("", raw)
        if '<script>document.documentElement.classList.add("preload-theme");</script>' not in raw:
            raw = re.sub(r'(<link rel="stylesheet" href="css/main\.css(?:\?[^"]*)?">)', r'\1' + "\n" + THEME_BOOT, raw, count=1)
        raw = re.sub(r"<!--JS-->", JS_BLOCK, raw)

    # Portal/QR pages retain deliberate minimal shells. Do not inject pwa.js,
    # localStorage theme boot, or update-checker there: a reload could discard
    # an opaque portal session or an issued QR bearer before it is printed.
    if not is_memory_only_page:
        raw = _ensure_pwa(raw, depth=depth)
        raw = _ensure_version_scripts(raw, depth)

    # --- Version wiring ------------------------------------------------------
    raw = _stamp_assets(raw, version)

    f.write_text(raw, encoding="utf-8")
    return True


def main():
    parser = argparse.ArgumentParser(description="Build the J-ONE frontend.")
    parser.add_argument("--bump", choices=("major", "minor", "patch"),
                        help="Release a new version before building.")
    parser.add_argument("--version", metavar="X.Y.Z",
                        help="Set an explicit version before building.")
    args = parser.parse_args()

    if args.bump and args.version:
        raise SystemExit("Use either --bump or --version, not both.")

    version = read_version()
    if args.version:
        if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
            raise SystemExit(f"Invalid version: {args.version!r} (expected X.Y.Z)")
        version = args.version
    elif args.bump:
        version = bump_version(version, args.bump)

    # Always (re)sync the generated artifacts with the released version so a
    # drifted js/version.js or service-worker CACHE_VERSION can never survive
    # a build. On --bump/--version this IS the release step; on a plain build
    # it is a no-op consistency check.
    write_version(version)
    if args.bump or args.version:
        print(f"Released version {version} (version.json, js/version.js, sw.js cache).")

    # Public pages get the full chrome rebuild; dashboard and portal pages keep
    # their own shells. Every intentional page gets asset stamps; public and
    # dashboard pages also get version/update-checker wiring.
    built = []
    pages = list(PUBLIC_PAGES) + list(PORTAL_PAGES) + list(QR_SERVICE_PAGES) + sorted(
        str(p.relative_to(ROOT)) for p in (ROOT / "dashboard").glob("*.html")
    )
    for p in pages:
        if build(p, version):
            built.append(p)
    print(f"Built {len(built)} page(s) at version {version}: {', '.join(built)}")


if __name__ == "__main__":
    main()
