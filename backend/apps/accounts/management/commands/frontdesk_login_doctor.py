"""Read-only diagnosis of Front Desk email/password sign-in enrolment.

Run on the server (production or staging):

    python manage.py frontdesk_login_doctor
    python manage.py frontdesk_login_doctor --observed-ip 102.89.34.7

WHY THIS EXISTS
===============
Front-desk staff (other than management) may sign in with their email and
password only from the enrolled Receptionist Desktop. Three enrolment routes
exist, and every one of them requires an ACTIVE FRONT_DESK WORKSTATION RECORD
to exist first:

1. a desktop key issued for that workstation and installed in the desk browser;
2. the address the desk reaches the API from, registered on the workstation
   (``Workstation.ip_address``);
3. the address/network saved in ``HotelSettings.front_desk_desktop_ip`` —
   which only takes effect while an active FRONT_DESK workstation exists.

An administrator who configures only the Settings address (the field the
Settings page invites them to fill) therefore still has a desk where every
password sign-in is refused: the configuration looks complete but nothing can
match. This command prints the actual enrolment state from the database, says
which single step is missing, and checks an observed address against the saved
bindings — without changing anything. It is strictly read-only: no rows are
created, saved or deleted, and no secret (key digest, password, token) is ever
printed.
"""
from django.core.management.base import BaseCommand

from apps.accounts.desktop_policy import (
    active_front_desk_workstation,
    configured_front_desk_desktop_ip,
    configured_front_desk_desktop_networks,
    front_desk_workstation_missing,
    receptionist_desktops_for_ip,
)
from apps.accounts.models import Workstation
from apps.core.client_ip import is_infrastructure_address, is_infrastructure_network


class Command(BaseCommand):
    help = (
        "Report how (or whether) the Front Desk desktop is enrolled for "
        "email/password staff sign-in, and what an administrator must register. "
        "Read-only: changes nothing."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--observed-ip",
            default="",
            help=(
                "The address the server reports for the desk browser (Settings "
                "page, 'IP address seen by the server'). Checked against the "
                "saved bindings to show which enrolment route would match."
            ),
        )

    def handle(self, *args, **options):
        out = self.stdout
        observed = str(options.get("observed_ip") or "").strip()

        out.write("Front Desk login doctor (read-only)")
        out.write("=" * 46)

        # ---- 1. The workstation records: the foundation of every route ----
        desks = list(Workstation.objects.filter(department=Workstation.Department.FRONT_DESK).order_by("pk"))
        active_desks = [desk for desk in desks if desk.is_active]
        if not desks:
            out.write(
                "\n1. FRONT DESK WORKSTATIONS: NONE REGISTERED"
                "\n   >>> This is the blocking gap. No address and no desktop key can"
                "\n       authorize the desk until a Front Desk workstation exists."
                "\n   FIX: an administrator opens Dashboard -> Terminals and registers"
                "\n        the Front Desk workstation (an active FRONT_DESK record)."
            )
        else:
            out.write(f"\n1. FRONT DESK WORKSTATIONS: {len(active_desks)} active / {len(desks)} total")
            for desk in desks:
                state = "ACTIVE" if desk.is_active else "inactive (admits nobody)"
                key = "desktop key issued" if desk.sign_in_key_hash else "no desktop key"
                registered = (
                    f"registered address {desk.ip_address}"
                    if desk.ip_address else "no address registered"
                )
                routes = []
                if desk.is_active and desk.sign_in_key_hash:
                    routes.append("key route ready")
                if desk.is_active and desk.ip_address:
                    routes.append("address route ready")
                out.write(
                    f"   - {desk.name} [{desk.reference}] ({state})"
                    f"\n     {key}; {registered}"
                    + (f"; {'; '.join(routes)}" if routes else "")
                )
            if not active_desks:
                out.write(
                    "\n   >>> All Front Desk workstations are INACTIVE, so no route can"
                    "\n       authorize sign-ins. FIX: activate one in Dashboard -> Terminals."
                )

        # ---- 2. The hotel-wide Settings binding ---------------------------
        configured = configured_front_desk_desktop_ip()
        networks = configured_front_desk_desktop_networks()
        out.write("\n2. SETTINGS 'Front Desk desktop address' (hotel-wide pin/route 3):")
        if not configured:
            out.write(
                "   (empty) — no network pin; the workstation routes above decide alone."
            )
        else:
            unusable = [str(net) for net in networks if is_infrastructure_network(net)]
            out.write(f"   saved value: {configured}")
            if front_desk_workstation_missing():
                out.write(
                    "   >>> INEFFECTIVE RIGHT NOW: no active Front Desk workstation"
                    "\n       exists, and this address only authorizes sign-ins while"
                    "\n       one does. FIX: register the workstation first (step 1)."
                )
            if unusable:
                plural = "ENTRIES" if len(unusable) > 1 else "ENTRY"
                out.write(
                    f"   >>> UNUSABLE {plural}: {', '.join(unusable)}"
                    "\n       This is a hosting/private address no browser can arrive"
                    "\n       from, so it can never match. FIX: replace it with the"
                    "\n       address the Settings page reports when opened ON the desk."
                )
            if observed:
                from apps.core.client_ip import address_in_network, parse_address

                address = parse_address(observed)
                if address is None:
                    out.write(f"   observed address {observed!r} is not a valid IP — skipped.")
                elif any(address_in_network(observed, str(net)) for net in networks):
                    out.write(
                        f"   observed address {observed} MATCHES this binding"
                        + (
                            " (but see the workstation gap above)"
                            if front_desk_workstation_missing()
                            else " — route 3 can authorize the desk."
                        )
                    )
                else:
                    out.write(
                        f"   observed address {observed} does NOT match this binding."
                    )

        # ---- 3. What the observed address would authorize ------------------
        if observed:
            matches = receptionist_desktops_for_ip(observed)
            out.write(f"\n3. OBSERVED ADDRESS {observed} (route 2, workstation records):")
            if matches:
                for desk in matches:
                    out.write(
                        f"   MATCHES workstation {desk.name} [{desk.reference}]"
                        " — sign-in from this address needs only correct credentials."
                    )
                if len(matches) > 1:
                    out.write(
                        "   >>> AMBIGUOUS: the same address is registered on more than"
                        "\n       one active Front Desk workstation, and an ambiguous"
                        "\n       registration authorizes NOBODY. FIX: keep it on exactly"
                        "\n       one workstation record."
                    )
            else:
                out.write(
                    "   matches no active Front Desk workstation record."
                )
            if is_infrastructure_address(observed):
                out.write(
                    "   >>> This looks like a hosting/private address, not a desk on the"
                    "\n       internet: registering it cannot work. Read the address from"
                    "\n       the Settings page opened ON the desk itself."
                )

        # ---- 4. The summary an administrator can act on --------------------
        out.write("\n4. WHAT TO DO")
        if front_desk_workstation_missing():
            out.write(
                "   STEP 1 (blocking): register an active Front Desk workstation in"
                "\n   Dashboard -> Terminals. Until this exists, email/password sign-in"
                "\n   for front-desk staff is refused from every device by design."
            )
        else:
            desk = active_front_desk_workstation()
            if desk.sign_in_key_hash or desk.ip_address:
                out.write(
                    "   The desk is enrolled (key and/or registered address). A"
                    "\n   front-desk employee should be able to sign in with email and"
                    "\n   password from that desk. If sign-in is still refused, read the"
                    "\n   address the login refusal reports and compare it with the"
                    "\n   registered values above (a moved desk needs re-registration)."
                )
            else:
                out.write(
                    "   An active workstation exists but NEITHER enrolment route is"
                    "\n   configured: issue a desktop key for it (Terminals page) and"
                    "\n   install that key in the desk browser, or register the desk's"
                    "\n   address on the workstation (or in Settings)."
                )
        out.write(
            "\n   Management (ADMIN / MANAGER / GENERAL_MANAGER) can always sign in"
            "\n   from any device; this policy binds operational staff only."
        )
        out.write("\nDiagnosis complete. Nothing was changed.")
