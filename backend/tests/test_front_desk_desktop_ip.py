"""Front Desk email/password sign-in from the configured desktop.

The behaviour under test is the one the front desk actually uses: an employee at
the registered Front Desk desktop signs in with their OWN email and password and
nothing else — no desktop key typed, no attendance/access code, no fingerprint,
no prior clock-in.

Two administrator registrations can name that desktop, and this suite pins their
relationship (see ``apps.accounts.desktop_policy`` for the documented
precedence):

* ``Workstation.ip_address`` — the address registered on a single desk; and
* ``HotelSettings.front_desk_desktop_ip`` — the hotel-wide value the Settings
  page reports ``request_ip`` for and invites an administrator to save.

Saving the address in Settings used to be restriction-only, so the very desk it
described was refused and staff fell back to the numeric access code. These
tests pin that a Settings-registered desktop AUTHORIZES sign-in, while every
existing refusal (wrong password, wrong address, inactive account, inactive or
wrong-department workstation, forged forwarding headers) still holds.

Nothing here weakens the other departments: an operational role still needs the
desk, and management still keeps its any-device exception.
"""
from django.core.cache import cache
from django.test import override_settings

from apps.accounts.desktop_policy import (
    DESKTOP_BY_SETTINGS_IP,
    DESKTOP_HEADER,
    issue_desktop_key,
)
from apps.accounts.models import User, Workstation
from apps.audit.models import AuditLog
from apps.hotel.models import HotelSettings
from apps.staff_operations.models import AttendanceRecord

from .base import BaseAPITestCase
from .factories import hotel_settings, make_user

PASSWORD = "Str0ng!Pass"
DESK_IP = "102.89.34.7"          # the configured Front Desk desktop address
OTHER_IP = "102.89.34.99"        # some other device on the internet
DESK_NETWORK = "102.89.34.0/24"  # the same desk, registered as a network
PROXY_PEER = "10.0.0.14"         # the deployment's own proxy (private range)

LOGIN_URL = "/api/auth/login/"
REFRESH_URL = "/api/auth/token/refresh/"
LOGOUT_URL = "/api/auth/logout/"
SETTINGS_URL = "/api/admin/settings/"
CLOCK_URL = "/api/admin/staff-operations/attendance/clock/"
WORKSTATIONS_URL = "/api/admin/users/terminals/"


class FrontDeskLoginTestCase(BaseAPITestCase):
    """Shared fixture: a configured desktop plus its front desk employees."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.admin = make_user("fd-admin@jone.test", password=PASSWORD, role=User.Role.ADMIN)
        self.receptionist = make_user("fd-one@jone.test", password=PASSWORD, role=User.Role.RECEPTIONIST)
        self.colleague = make_user("fd-two@jone.test", password=PASSWORD, role=User.Role.RECEPTIONIST)
        # The desktop as an administrator configures it in Settings, and the
        # desk record that makes the hotel's front desk a real, deactivatable
        # registration. Its own ip_address is deliberately left blank: this is
        # the deployment in which the address was only ever saved in Settings.
