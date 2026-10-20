# apps/core/apps.py
from django.apps import AppConfig

_MYSQL_LARGE_COLUMN_MARKERS = ("text", "blob", "json", "geometry")


def _install_mysql8_text_db_default_compat() -> None:
    """Work around Oracle MySQL 8.0 bug rejecting `ALTER COLUMN <TEXT> SET DEFAULT (...)`.

    Django 5.2's `BaseDatabaseSchemaEditor._alter_column_database_default_sql`
    emits `ALTER COLUMN col SET DEFAULT ('')`, which MariaDB accepts on
    `LONGTEXT` columns (`notifications.0009`), whereas Oracle MySQL 8.0 raises
    `OperationalError: (1101, "BLOB, TEXT, GEOMETRY or JSON column '...' can't
    have a default value")` unless emitted via `MODIFY COLUMN ... DEFAULT (...)`.
    """
    from django.db.backends.mysql.schema import DatabaseSchemaEditor

    if getattr(DatabaseSchemaEditor, "_jone_mysql8_db_default_patched", False):
        return

    original = DatabaseSchemaEditor._alter_column_database_default_sql

    def _compat_alter_column_database_default_sql(
        self, model, old_field, new_field, drop=False
    ):
        new_db_params = new_field.db_parameters(connection=self.connection)
        col_type = (new_db_params.get("type") or "").lower()
        if not self.connection.mysql_is_mariadb and any(
            marker in col_type for marker in _MYSQL_LARGE_COLUMN_MARKERS
        ):
            null_sql = "NULL" if new_field.null else "NOT NULL"
            column_sql = self.quote_name(new_field.column)
            if drop:
                return f"MODIFY COLUMN {column_sql} {new_db_params['type']} {null_sql}", []
            default_sql, params = self.db_default_sql(new_field)
            return (
                f"MODIFY COLUMN {column_sql} {new_db_params['type']} {null_sql} DEFAULT {default_sql}",
                params,
            )
        return original(self, model, old_field, new_field, drop=drop)

    DatabaseSchemaEditor._alter_column_database_default_sql = (
        _compat_alter_column_database_default_sql
    )
    DatabaseSchemaEditor._jone_mysql8_db_default_patched = True


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    verbose_name = "Core / Shared Infrastructure"

    def ready(self) -> None:
        _install_mysql8_text_db_default_compat()
