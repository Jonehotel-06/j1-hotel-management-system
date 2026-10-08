#!/usr/bin/env python3
# dashboard/build_dash.py
"""Disabled legacy dashboard generator.

The dashboard pages are now checked-in, API-driven staff console pages. The old
regenerator used embedded demonstration records and must not be run because it
could overwrite production dashboard pages with non-authoritative content.

If dashboard generation is needed again, rebuild this tool around the current
API contracts and loading/empty/error-state pattern in the shipped pages.
"""

raise SystemExit(
    "dashboard/build_dash.py is disabled. Edit the checked-in dashboard HTML "
    "pages directly, or replace this script with an API-driven generator before running it."
)
