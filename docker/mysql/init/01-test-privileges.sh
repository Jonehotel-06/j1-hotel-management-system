#!/bin/sh
# Executed automatically by official mysql:8.0 ONLY when initializing a new,
# empty data volume for the first time. Never alters existing tables or data.
# Grants the application user permission on isolated `test_%` schemas so
# `docker compose exec api python manage.py test` works without root access.
set -eu

if [ -n "${MYSQL_USER:-}" ]; then
    mysql -u root -p"${MYSQL_ROOT_PASSWORD}" <<-EOSQL
        GRANT ALL PRIVILEGES ON \`test_%\`.* TO '${MYSQL_USER}'@'%';
        FLUSH PRIVILEGES;
EOSQL
fi
