"""Regenerate docs/role_permission_matrix.json from the live Django URL map.

Run from ``backend/``:

    DJANGO_SETTINGS_MODULE=config.settings.development \
        python ../docs/tools/build_role_permission_matrix.py

The JSON is the machine-readable half of docs/ROLE_PERMISSION_MATRIX.md. It is
derived, never hand-maintained: endpoint rows come from Django's resolver,
grants from ``apps.accounts.capabilities``, page visibility from
``frontend/js/dashboard.js`` PAGE_ACCESS (exported by
``docs/tools/export_page_access.js``), and landing pages from
``frontend/js/auth.js`` defaultStaffLanding().
"""
import json
import os
import re
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
import django  # noqa: E402

django.setup()

from django.urls import get_resolver  # noqa: E402

from apps.accounts.capabilities import CAPABILITY_DEFINITIONS, ROLE_CAPABILITY_CODES  # noqa: E402
from apps.accounts.models import User  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ACCESS_JSON = os.path.join(ROOT, "docs", "page_access_export.json")
OUTPUT = os.path.join(ROOT, "docs", "role_permission_matrix.json")

#: Landing page per role, copied from ``defaultStaffLanding`` in js/auth.js.
#: Subdomain contexts (APP_CONFIG.DEPARTMENT_SUBDOMAINS) can override it.
LOGIN_DESTINATIONS = {
    "CASHIER": "/dashboard/pos.html",
    "RESTAURANT_MANAGER": "/dashboard/pos.html",
    "WAITER": "/dashboard/pos.html",
    "BAR_MANAGER": "/dashboard/pos.html",
    "BARTENDER": "/dashboard/pos.html",
    "KITCHEN_MANAGER": "/dashboard/pos.html",
    "CHEF": "/dashboard/pos.html",
    "KITCHEN_ASSISTANT": "/dashboard/pos.html",
    "HOUSEKEEPING": "/dashboard/housekeeping.html",
    "HOUSEKEEPING_MANAGER": "/dashboard/housekeeping.html",
    "HOUSEKEEPER": "/dashboard/housekeeping.html",
    "PORTER": "/dashboard/service-requests.html",
    "MAINTENANCE": "/dashboard/maintenance.html",
    "MAINTENANCE_TECHNICIAN": "/dashboard/maintenance.html",
    "INVENTORY_CLERK": "/dashboard/inventory.html",
    "STOREKEEPER": "/dashboard/inventory.html",
    "PROCUREMENT_OFFICER": "/dashboard/procurement.html",
    "ACCOUNTS_MANAGER": "/dashboard/payments.html",
    "ACCOUNTANT": "/dashboard/payments.html",
    "HR_MANAGER": "/dashboard/workforce.html",
    # Guests never reach the dashboard: login.html sends a non-staff account to
    # the public site (index.html), and every /api/admin/* route answers 403.
    "GUEST": "/index.html",
}
DEFAULT_DESTINATION = "/dashboard/index.html"

SKIP_FRAGMENTS = ("media", "APIRootView", "(?P<format>")


def _clean_path(raw: str) -> str:
    path = "/" + raw.lstrip("/")
    path = path.replace("^", "").replace("$", "")
    path = path.replace("(?P<pk>[^/.]+)", "<pk>").replace("(?P<reference>[^/.]+)", "<reference>")
    path = re.sub(r"\(\?P<(\w+)>[^)]+\)", r"<\1>", path)
    return re.sub(r"/{2,}", "/", path)


def walk(patterns, prefix=""):
    for pattern in patterns:
        raw = prefix + str(pattern.pattern)
        if hasattr(pattern, "url_patterns"):
            yield from walk(pattern.url_patterns, raw)
        else:
            yield raw, pattern


rows, seen = [], set()
for raw, pattern in walk(get_resolver().url_patterns):
    if not raw.startswith("api/"):
        continue
    if any(fragment in raw for fragment in SKIP_FRAGMENTS):
        continue
    callback = pattern.callback
    view = getattr(callback, "cls", None) or getattr(getattr(callback, "__self__", None), "__class__", None)
    if view is None:
        continue
    path = _clean_path(raw)
    if path in seen:
        continue
    seen.add(path)
    required = getattr(view, "required_capabilities", None) or getattr(view, "required_capability", None)
    if isinstance(required, str):
        required = [required]
    rows.append(
        {
            "path": path,
            "view": view.__name__,
            "permission_classes": [getattr(p, "__name__", str(p)) for p in getattr(view, "permission_classes", [])],
            "required_capabilities": list(required or []),
            "require_any": bool(getattr(view, "require_any_capability", False)),
            "login_required": "AllowAny" not in [getattr(p, "__name__", "") for p in getattr(view, "permission_classes", [])],
        }
    )
rows.sort(key=lambda row: (row["path"], row["view"]))

with open(ACCESS_JSON) as handle:
    access = json.load(handle)
pages = access["PAGE_ACCESS"]
pages_by_role = {}
for page, rule in pages.items():
    targets = access["ROLES"] if rule.get("everyone") else rule.get("roles", [])
    for role in targets:
        pages_by_role.setdefault(role.upper(), set()).add(page)
for role in access["ROLES"]:
    pages_by_role.setdefault(role.upper(), set())

DEPARTMENTS = {
    "MANAGEMENT": {
        "name": "Management",
        "dashboard": "/dashboard/index.html",
        "roles": ["ADMIN", "GENERAL_MANAGER", "MANAGER", "HR_MANAGER"],
    },
    "ACCOUNTS": {
        "name": "Accounts & Finance",
        "dashboard": "/dashboard/accounting.html",
        "roles": ["ACCOUNTS_MANAGER", "ACCOUNTANT", "CASHIER"],
    },
    "ADMIN_PROCUREMENT": {
        "name": "Administration & Procurement",
        "dashboard": "/dashboard/procurement.html",
        "roles": ["ADMIN", "PROCUREMENT_OFFICER", "MANAGER"],
    },
    "FRONT_DESK": {
        "name": "Front Desk / Reception",
        "dashboard": "/dashboard/bookings.html",
        "roles": ["FRONT_DESK_SUPERVISOR", "RECEPTIONIST"],
    },
    "KITCHEN": {
        "name": "Kitchen",
        "dashboard": "/dashboard/pos.html",
        "roles": ["KITCHEN_MANAGER", "CHEF", "KITCHEN_ASSISTANT"],
    },
    "RESTAURANT_BAR": {
        "name": "Restaurant & Bar Service",
        "dashboard": "/dashboard/pos.html",
        "roles": ["RESTAURANT_MANAGER", "WAITER", "BAR_MANAGER", "BARTENDER"],
    },
    "HOUSEKEEPING": {
        "name": "Housekeeping",
        "dashboard": "/dashboard/housekeeping.html",
        "roles": ["HOUSEKEEPING_MANAGER", "HOUSEKEEPER", "HOUSEKEEPING"],
    },
    "PORTERING": {
        "name": "Portering / Maintenance",
        "dashboard": "/dashboard/service-requests.html",
        "roles": ["PORTER", "SECURITY", "MAINTENANCE", "MAINTENANCE_TECHNICIAN"],
    },
    "STORES_INVENTORY": {
        "name": "Stores & Inventory",
        "dashboard": "/dashboard/inventory.html",
        "roles": ["STOREKEEPER", "INVENTORY_CLERK"],
    },
}

role_entries = []
for role in User.Role.values:
    role_entries.append(
        {
            "role": role,
            "label": User.Role(role).label,
            "departments": [code for code, dept in DEPARTMENTS.items() if role in dept["roles"]],
            "capabilities": sorted(ROLE_CAPABILITY_CODES.get(role, ())),
            "login_destination": LOGIN_DESTINATIONS.get(role, DEFAULT_DESTINATION),
            "visible_nav": access.get("VISIBLE_NAV", {}).get(role),
            "dashboard_pages": sorted(pages_by_role.get(role, set())),
            "is_guest": role == "GUEST",
        }
    )

payload = {
    "_meta": {
        "generated": "2026-10-22",
        "generator": "docs/tools/build_role_permission_matrix.py",
        "sources": [
            "backend Django URL resolver (live walk, this deployment's settings)",
            "apps.accounts.models.User.Role",
            "apps.accounts.capabilities.CAPABILITY_DEFINITIONS",
            "apps.accounts.capabilities.ROLE_CAPABILITY_CODES",
            "frontend/js/dashboard.js PAGE_ACCESS (exported to docs/page_access_export.json)",
            "frontend/js/auth.js defaultStaffLanding()",
        ],
        "notes": [
            "ADMIN resolves to the full active capability catalogue at runtime; its role row lists the "
            "catalogue in code, which can trail the database by a seed migration.",
            "dashboard_pages is UI navigation only. It never authorises an API call: every endpoint row "
            "carries its own server-side permission_classes/required_capabilities.",
            "require_any=true means any ONE listed capability suffices; otherwise all are required.",
            "Paths are normalised from Django regexes; <pk>/<reference> are path parameters.",
            "Capability grants are the code catalogue. A deployment whose RoleCapability rows were seeded "
            "earlier can differ; run the audit probe against the target database.",
        ],
    },
    "capabilities": [
        {"code": code, "name": name, "category": category, "description": description}
        for code, name, category, description in CAPABILITY_DEFINITIONS
    ],
    "departments": DEPARTMENTS,
    "roles": role_entries,
    "endpoints": rows,
}

with open(OUTPUT, "w") as handle:
    json.dump(payload, handle, indent=2, sort_keys=False)
    handle.write("\n")

print(f"roles={len(role_entries)} endpoints={len(rows)} capabilities={len(CAPABILITY_DEFINITIONS)}")
print(f"departments={len(DEPARTMENTS)} -> {OUTPUT}")
