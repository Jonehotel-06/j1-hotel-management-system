# apps/core/management/commands/client_ip_check.py
"""Show what client address this deployment resolves, and which rule decided it.

WHY THIS EXISTS
===============
Two product rules depend on knowing where a request actually came from: an
operational staff account may only sign in from the registered Front Desk
desktop, and attendance is clocked at that same desktop. When the answer is
wrong the operator sees "this device is not the front desk" and there is
nothing on the server that explains why - the address is simply different every
time, or it is the platform's own proxy address.

The usual cause is a proxy the deployment never declared: Cloudflare in front
of Northflank reaches the container from a Cloudflare edge address, which the
private-range defaults do not trust, so the forwarding headers are ignored and
``REMOTE_ADDR`` (the ingress) is returned. This command makes that visible
without a debugger:

    python manage.py client_ip_check                  # resolved configuration
    python manage.py client_ip_check --simulate       # ...and a worked example
    python manage.py client_ip_check --simulate \
        --peer 162.158.0.10 --cf-connecting-ip 102.89.33.7

``--simulate`` builds a fake request and prints the address the product would
use, the rule that produced it, and whether the address is usable for a desk
registration at all. ``--peer`` may be repeated to try several hops at once,
which is how a Northflank deployment confirms the Cloudflare option did what it
claims before anyone re-enrols a workstation.

Nothing secret is printed: these are network addresses, not credentials.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.core.client_ip import (
    CLOUDFLARE_NETWORKS,
    client_ip_source,
    is_infrastructure_address,
    is_trusted_proxy,
    normalize_ip,
    resolve_client_ip,
    trust_cloudflare_networks,
    trusted_client_ip_headers,
    trusted_proxy_networks,
)

#: A public address used when the operator gives no example of their own. It is
#: deliberately in no trusted-proxy range, so the demo shows the "would this be
#: usable for a desk" answer rather than a special case.
DEMO_CLIENT = "102.89.33.7"
#: A Cloudflare edge address: what a Cloudflare -> Northflank deployment sees.
DEMO_EDGE = "162.158.0.10"


class _FakeRequest:
    """Just enough of an HttpRequest for ``resolve_client_ip``."""

    def __init__(self, meta):
        self.META = meta


class Command(BaseCommand):
    help = (
        "Print the trusted-proxy configuration, then show which client address "
        "would be resolved for a sample request and which rule decided it."
    )
    requires_system_checks: list[str] = []

    def add_arguments(self, parser):
        parser.add_argument(
            "--simulate", action="store_true",
            help="Build a sample request and report the address the product would use.",
        )
        parser.add_argument(
            "--peer", action="append", default=[], metavar="REMOTE_ADDR",
            help=(
                "Address the connection arrives from (repeatable). Defaults to a "
                "Cloudflare edge address, which is what Cloudflare -> Northflank sees."
            ),
        )
        parser.add_argument("--forwarded-for", default="", metavar="XFF",
                            help="Value for X-Forwarded-For in the simulated request.")
        parser.add_argument("--cf-connecting-ip", default="", metavar="IP",
                            help="Value Cloudflare would put in CF-Connecting-IP.")
        parser.add_argument("--true-client-ip", default="", metavar="IP",
                            help="Value for True-Client-IP (what some platforms rewrite).")
        parser.add_argument("--x-real-ip", default="", metavar="IP",
                            help="Value for X-Real-IP (nginx / some edges).")

    def handle(self, *args, **options):
        self._report_configuration()
        if options["simulate"] or any(
            options[key]
            for key in ("peer", "forwarded_for", "cf_connecting_ip", "true_client_ip", "x_real_ip")
        ):
            self._simulate(options)
        else:
            self.stdout.write("")
            self.stdout.write(
                "Add --simulate (optionally with --peer / --cf-connecting-ip) to see "
                "the address this deployment would resolve."
            )

    def _report_configuration(self):
        cloudflare = trust_cloudflare_networks()
        networks = trusted_proxy_networks()
        headers = trusted_client_ip_headers()

        self.stdout.write(self.style.MIGRATE_HEADING("Trusted proxies"))
        self.stdout.write(
            f"  TRUST_PROXY_HEADERS_FROM_CLOUDFLARE = {'on' if cloudflare else 'off'}"
            + ("" if cloudflare else "  (private / loopback ranges only)")
        )
        if cloudflare:
            self.stdout.write(
                f"  Cloudflare ranges merged: {len(CLOUDFLARE_NETWORKS)} "
                "(cloudflare.com/ips-v4 + /ips-v6)"
            )
        self.stdout.write(f"  Effective trusted networks: {len(networks)}")
        # A handful, not the whole list: the useful question is "is the edge in
        # there", and the answer either is or is not.
        for network in networks[:8]:
            marker = "   <- Cloudflare" if cloudflare and str(network) in CLOUDFLARE_NETWORKS else ""
            self.stdout.write(f"    {network}{marker}")
        if len(networks) > 8:
            self.stdout.write(f"    ... {len(networks) - 8} more")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Edge headers consulted (in order)"))
        if headers:
            for name in headers:
                self.stdout.write(f"  {name[len('HTTP_'):].lower().replace('_', '-')}")
        else:
            self.stdout.write("  (none - only X-Forwarded-For is walked)")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Verdict"))
        if cloudflare:
            self.stdout.write(
                "  Cloudflare is trusted: a request arriving from a Cloudflare edge "
                "address uses CF-Connecting-IP (or True-Client-IP / X-Real-IP when the "
                "edge rewrote one) as the client address."
            )
        else:
            self.stdout.write(
                "  Only private / loopback peers are trusted. If this deployment sits "
                "behind Cloudflare -> Northflank, the connection arrives from a PUBLIC "
                "Cloudflare address, so forwarding headers are IGNORED and the address "
                "you see is the ingress. Set TRUST_PROXY_HEADERS_FROM_CLOUDFLARE=1 and "
                "re-run --simulate."
            )

    def _simulate(self, options):
        peers = [normalize_ip(item) for item in options["peer"] if normalize_ip(item)]
        if not peers:
            peers = [DEMO_EDGE]
        client = (
            normalize_ip(options["cf_connecting_ip"])
            or normalize_ip(options["true_client_ip"])
            or normalize_ip(options["x_real_ip"])
            or DEMO_CLIENT
        )
        forwarded_for = options["forwarded_for"]
        if not forwarded_for:
            # What Cloudflare + the platform actually produce: the client's own
            # (forgeable) prefix, then the real client.
            forwarded_for = f"203.0.113.99, {client}"

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Simulated requests"))
        for peer in peers:
            request = _FakeRequest({
                "REMOTE_ADDR": peer,
                "HTTP_X_FORWARDED_FOR": forwarded_for,
                "HTTP_CF_CONNECTING_IP": options["cf_connecting_ip"] or client,
                "HTTP_TRUE_CLIENT_IP": options["true_client_ip"],
                "HTTP_X_REAL_IP": options["x_real_ip"],
            })
            resolved = resolve_client_ip(request)
            source = client_ip_source(request)
            trusted = is_trusted_proxy(peer)
            infrastructure = is_infrastructure_address(resolved) if resolved else False

            self.stdout.write("")
            self.stdout.write(f"  REMOTE_ADDR            {peer}")
            self.stdout.write(
                f"  trusted proxy?         {'yes' if trusted else 'no'}"
                + ("" if trusted else "  <- forwarding headers ignored")
            )
            self.stdout.write(f"  X-Forwarded-For        {forwarded_for}")
            self.stdout.write(f"  client address         {resolved or '(none)'}")
            self.stdout.write(f"  decided by             {source}")
            if not resolved:
                self.stdout.write(self.style.WARNING(
                    "  -> nothing usable; a desk cannot be enrolled from this request."
                ))
            elif infrastructure:
                self.stdout.write(self.style.WARNING(
                    "  -> infrastructure address: this is the platform's own proxy, not a "
                    "device. Registering it will never authorise a desk."
                ))
            else:
                self.stdout.write(self.style.SUCCESS(
                    f"  -> usable: register {resolved} as the workstation address for "
                    "this desk (settings -> workstation registrations)."
                ))

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Reading the result"))
        self.stdout.write(
            "  'decided by' names the rule that produced the answer:\n"
            "    peer              the connection itself (no trusted proxy involved)\n"
            "    cf-connecting-ip  Cloudflare's own header - what you want behind CF\n"
            "    true-client-ip    the platform rewrote it\n"
            "    x-real-ip         your own front end set it\n"
            "    forwarded         walked out of X-Forwarded-For, right to left"
        )
        if not trust_cloudflare_networks():
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "  TRUST_PROXY_HEADERS_FROM_CLOUDFLARE is off: a Cloudflare peer is NOT "
                "trusted above. Re-run with the setting on to compare."
            ))
        self.stdout.write("")
        self.stdout.write("  Reference: docs/NORTHFLANK_CLOUDFLARE_NAMECHEAP_DEPLOYMENT.md")
