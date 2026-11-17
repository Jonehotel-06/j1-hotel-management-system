#!/usr/bin/env bash
# Provider-neutral build procedure: dependencies → static files → migrations.
#
# The step order matters. A previous deployment shipped the application image
# (with new endpoints such as /api/admin/inventory/units/ and new model fields
# such as StockLocation.is_reception) BEFORE the matching migrations reached the
# database. Every affected endpoint then raised ProgrammingError ("no such
# column ..."), which the API edge reported as "the database is temporarily
# unavailable" — a transient-sounding 503 for a permanent state.
#
# `check_schema` closes that window: after `migrate` it introspects the live
# database against the model registry and exits non-zero if any table or column
# the code expects is still missing. The build then fails loudly instead of
# publishing an API that answers 500 on half its screens.
set -o errexit

pip install -r requirements.txt

python manage.py collectstatic --noinput
python manage.py migrate --noinput

# Verify the schema the running code expects actually exists. Read-only.
python manage.py check_schema
