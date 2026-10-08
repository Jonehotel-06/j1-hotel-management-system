"""Safe production diagnostic for Redis, the Celery broker and workers.

Run this from a managed-host shell using the same environment as the web/worker:
    python manage.py email_pipeline_check --publish

Historical note: this command predates synchronous email delivery. Email no
longer travels through Celery/Redis at all; this is now the health check for
the Celery infrastructure that still drives the scheduled booking tasks
(expiry, auto-checkout, checkout warnings), kept under its original name so
existing runbooks keep working.

No URL, password, SMTP secret, or token is printed.
"""
import socket
import ssl
from urllib.parse import urlparse

import redis
from celery import current_app
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Check Redis, the Celery broker, task registration, and an optional worker round trip."

    def add_arguments(self, parser):
        parser.add_argument(
            "--publish", action="store_true",
            help="Publish a harmless probe and require a worker/result round trip.",
        )
        parser.add_argument("--timeout", type=float, default=5.0)
        parser.add_argument(
            "--booking", default="",
            help="Also regenerate and validate the named booking's receipt PDF in memory.",
        )

    def handle(self, *args, **options):
        timeout = max(1.0, min(options["timeout"], 15.0))
        url = (getattr(settings, "CELERY_BROKER_URL", "") or "").strip()
        parsed = urlparse(url)
        port = parsed.port or (6380 if parsed.scheme == "rediss" else 6379)

        self.stdout.write(self.style.MIGRATE_HEADING("Django / broker configuration"))
        self.stdout.write(f"  settings={settings.SETTINGS_MODULE}")
        self.stdout.write(
            "  REDIS_URL: configured=%s scheme=%s host=%s port=%s"
            % ("yes" if url else "no", parsed.scheme or "-", parsed.hostname or "-", port)
        )
        self.stdout.write(
            "  eager=%s connect_timeout=%ss"
            % (settings.CELERY_TASK_ALWAYS_EAGER,
               getattr(settings, "CELERY_BROKER_CONNECTION_TIMEOUT", "-"))
        )
        if not url or parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise CommandError("CONFIGURATION: CELERY_BROKER_URL/REDIS_URL is missing or invalid.")
        if settings.CELERY_TASK_ALWAYS_EAGER:
            raise CommandError("CONFIGURATION: eager mode is enabled; this is not the production queue path.")

        if options["booking"]:
            self._pdf(options["booking"])
        self._dns(parsed.hostname, port)
        self._redis_ping(url, timeout)
        self._celery_connection(timeout)
        registered = self._workers(timeout)
        if options["publish"]:
            self._publish(timeout, registered)

        self.stdout.write(self.style.SUCCESS("CELERY INFRASTRUCTURE CHECK: PASS"))

    def _pdf(self, lookup):
        from django.db.models import Q
        from apps.bookings.models import Booking
        from apps.bookings.serializers import ReceiptSerializer
        from apps.bookings.services.receipt_pdf import render_receipt_pdf

        query = Q(booking_reference=lookup)
        if str(lookup).isdigit():
            query |= Q(pk=int(lookup))
        booking = (
            Booking.objects.select_related("guest", "room_type", "offer")
            .filter(query)
            .first()
        )
        if booking is None:
            raise CommandError("PDF: booking was not found.")
        try:
            payload = ReceiptSerializer().to_representation(booking)
            pdf = render_receipt_pdf(payload)
        except Exception as exc:
            raise CommandError(f"PDF: generation failed ({exc.__class__.__name__}).") from None
        if not pdf or not bytes(pdf).startswith(b"%PDF-"):
            raise CommandError("PDF: generated data is not a valid PDF stream.")
        self.stdout.write(self.style.SUCCESS(f"  Receipt PDF: ok ({len(pdf)} bytes, in memory)"))

    def _dns(self, host, port):
        try:
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise CommandError(f"DNS: resolution failed ({exc.__class__.__name__}).") from None
        self.stdout.write(self.style.SUCCESS(f"  DNS: ok ({len(addresses)} address result(s))"))

    def _redis_ping(self, url, timeout):
        try:
            kwargs = {
                "socket_connect_timeout": timeout,
                "socket_timeout": timeout,
                "retry_on_timeout": False,
            }
            if urlparse(url).scheme == "rediss":
                kwargs["ssl_cert_reqs"] = "required"
            client = redis.Redis.from_url(url, **kwargs)
            if client.ping() is not True:
                raise CommandError("REDIS: PING returned a non-success response.")
        except redis.AuthenticationError:
            raise CommandError("AUTHENTICATION: Redis rejected the configured credentials.") from None
        except redis.TimeoutError:
            raise CommandError("CONNECTION TIMEOUT: Redis did not respond before the timeout.") from None
        except ssl.SSLError:
            raise CommandError("TLS: Redis certificate/handshake validation failed.") from None
        except redis.ConnectionError as exc:
            cause = exc.__cause__
            if isinstance(cause, socket.gaierror):
                stage = "DNS"
            elif isinstance(cause, ssl.SSLError):
                stage = "TLS"
            else:
                stage = "CONNECTION"
            raise CommandError(f"{stage}: Redis connection failed ({exc.__class__.__name__}).") from None
        self.stdout.write(self.style.SUCCESS("  Redis PING: ok (TLS/authentication/connectivity verified)"))

    def _celery_connection(self, timeout):
        try:
            connection = current_app.connection_for_write()
            connection.ensure_connection(max_retries=0, timeout=timeout)
            connection.release()
        except Exception as exc:  # Kombu wraps transport-specific exceptions
            raise CommandError(
                f"BROKER CONNECTION: Celery could not connect ({exc.__class__.__name__})."
            ) from None
        self.stdout.write(self.style.SUCCESS("  Celery broker connection: ok"))

    def _workers(self, timeout):
        try:
            registered = current_app.control.inspect(timeout=timeout).registered()
        except Exception as exc:
            raise CommandError(f"WORKER: inspection failed ({exc.__class__.__name__}).") from None
        if not registered:
            raise CommandError(
                "WORKER AVAILABILITY: broker is reachable but no Celery worker replied. "
                "Check the worker service boot/restart logs."
            )
        # The scheduled booking tasks (expiry / auto-checkout) are the work the
        # worker must be able to execute. Email no longer uses the broker at
        # all (it is delivered synchronously by the web process), so no email
        # task is expected here.
        expected = {
            "apps.bookings.tasks.expire_pending_bookings",
            "apps.bookings.tasks.auto_checkout_due_bookings",
            "apps.bookings.tasks.checkout_due_soon_warnings",
        }
        missing = [
            name for name, tasks in registered.items()
            if not expected.issubset(set(tasks or []))
        ]
        if missing:
            raise CommandError(
                "TASK REGISTRATION: worker(s) replied but the scheduled booking tasks "
                "are not all registered: " + ", ".join(sorted(missing))
            )
        self.stdout.write(self.style.SUCCESS(
            f"  Worker availability/task registration: ok ({len(registered)} worker(s))"
        ))
        return registered

    def _publish(self, timeout, registered):
        # Harmless worker round trip that needs no custom task: broadcast ping.
        try:
            replies = current_app.control.inspect(timeout=timeout).ping()
        except Exception as exc:
            raise CommandError(f"WORKER PING: failed ({exc.__class__.__name__}).") from None
        if not replies:
            raise CommandError("WORKER AVAILABILITY: ping received no replies before timeout.")
        self.stdout.write(self.style.SUCCESS(
            f"  Worker ping round trip: ok ({len(replies)} worker(s))")
        )
