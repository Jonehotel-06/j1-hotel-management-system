"""Read-only dependency health checks shared by the probes and management commands.

Two very different failures used to look identical in production:

1. the database is *unreachable* (transient: connection refused, pool exhausted,
   server restarting) — retrying genuinely helps;
2. the database is *reachable but its schema is behind the deployed code*
   (permanent until someone runs ``migrate``) — retrying never helps, and
   reporting it as transient hides the real cause for days.

:func:`database_ping` answers only (1). :func:`schema_drift_report` answers (2)
by introspecting the live database against the Django model registry. Both are
read-only and never mutate, migrate or write.
"""
from __future__ import annotations

import logging

from django.db import connection, connections

logger = logging.getLogger("apps")

#: Models whose tables are created outside the ORM migration flow, or which are
#: intentionally unmanaged. They are skipped rather than reported as drift.
SKIP_APPS = frozenset({"contenttypes", "auth", "sessions", "admin"})


def database_ping(alias: str = "default") -> bool:
    """True when the database answers a trivial query. Never raises."""
    try:
        with connections[alias].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 - a probe must never propagate
        logger.warning("readiness probe: database ping failed", exc_info=True)
        return False
    return True


def schema_drift_report(alias: str = "default", apps_registry=None) -> dict:
    """Compare the model registry with the physical schema. Read-only.

    Returns::

        {
            "ok": bool,
            "checked_models": int,
            "checked_tables": int,
            "missing_tables": [str, ...],
            "missing_columns": [(table, column), ...],
            "error": str,          # only when introspection itself failed
        }
    """
    from django.apps import apps as global_apps

    registry = apps_registry or global_apps
    missing_tables: list[str] = []
    missing_columns: list[tuple[str, str]] = []
    checked_models = 0
    tables: set[str] = set()

    try:
        conn = connections[alias]
        existing = set(conn.introspection.table_names())
    except Exception as exc:  # noqa: BLE001
        logger.error("schema drift: introspection failed", exc_info=True)
        return {
            "ok": False,
            "checked_models": 0,
            "checked_tables": 0,
            "missing_tables": [],
            "missing_columns": [],
            "error": f"{type(exc).__name__}: {exc}",
        }

    for model in registry.get_models():
        if not model._meta.managed:
            continue
        if model._meta.app_label in SKIP_APPS:
            continue
        table = model._meta.db_table
        checked_models += 1
        tables.add(table)
        if table not in existing:
            missing_tables.append(table)
            continue
        try:
            with conn.cursor() as cursor:
                described = conn.introspection.get_table_description(cursor, table)
            live = {column.name for column in described}
        except Exception:  # noqa: BLE001 - a table we cannot describe is reported below
            logger.warning("schema drift: cannot describe table %s", table, exc_info=True)
            continue
        for field in model._meta.local_concrete_fields:
            if field.column and field.column not in live:
                missing_columns.append((table, field.column))

    return {
        "ok": not missing_tables and not missing_columns,
        "checked_models": checked_models,
        "checked_tables": len(tables),
        "missing_tables": sorted(missing_tables),
        "missing_columns": sorted(missing_columns),
        "error": "",
    }


def pending_migration_count(alias: str = "default") -> int | None:
    """Number of migration files not recorded as applied. Read-only.

    ``None`` means the check itself could not run (for example the
    ``django_migrations`` table does not exist yet on a brand-new database).
    """
    try:
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connections[alias])
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        return len(plan)
    except Exception:  # noqa: BLE001
        logger.warning("readiness probe: migration check failed", exc_info=True)
        return None
