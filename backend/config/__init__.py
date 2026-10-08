# config/__init__.py
# Expose the Celery app when Django starts so shared_task decorators bind
# to it. Guarded so management commands still work if celery is unavailable
# (it is only required for background processing).
try:
    from .celery import app as celery_app

    __all__ = ("celery_app",)
except Exception:  # pragma: no cover - defensive
    celery_app = None
