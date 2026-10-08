#!/usr/bin/env bash
# Provider-neutral build procedure: dependencies → static files → migrations.
set -o errexit

pip install -r requirements.txt

python manage.py collectstatic --noinput
python manage.py migrate --noinput
