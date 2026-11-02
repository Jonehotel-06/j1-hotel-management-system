"""Workstation-key override is narrow: only an impossible saved IP pin."""

from unittest.mock import patch

from apps.accounts.desktop_policy import (
    DESKTOP_BY_IP,
    DESKTOP_BY_KEY,
    DESKTOP_BY_NONE,
    DESKTOP_BY_SETTINGS_IP,
    desktop_key_override_for_unusable_ip_pin,
)

from .base import BaseAPITestCase
from .factories import hotel_settings


class WorkstationKeyIpOverrideTests(BaseAPITestCase):
    def _override(self, client_ip):
        with patch(
            "apps.accounts.desktop_policy.request_client_ip",
            return_value=client_ip,
        ):
            return desktop_key_override_for_unusable_ip_pin(None, DESKTOP_BY_KEY)

    def test_public_visitor_with_infrastructure_only_pin_can_use_admin_issued_key(self):
        hotel_settings(front_desk_desktop_ip="100.64.0.18")
        self.assertTrue(self._override("8.8.8.8"))

    def test_normal_public_pin_is_never_overridden(self):
        hotel_settings(front_desk_desktop_ip="8.8.8.0/24")
        self.assertFalse(self._override("8.8.8.8"))

    def test_infrastructure_visitor_keeps_the_saved_pin(self):
        hotel_settings(front_desk_desktop_ip="100.64.0.18")
        self.assertFalse(self._override("100.64.0.19"))

    def test_no_saved_network_is_not_an_override(self):
        hotel_settings(front_desk_desktop_ip="")
        self.assertFalse(self._override("8.8.8.8"))

    def test_only_key_identified_requests_are_eligible(self):
        hotel_settings(front_desk_desktop_ip="100.64.0.18")
        for method in (DESKTOP_BY_IP, DESKTOP_BY_SETTINGS_IP, DESKTOP_BY_NONE, "credentials"):
            with patch(
                "apps.accounts.desktop_policy.request_client_ip",
                return_value="8.8.8.8",
            ):
                self.assertFalse(desktop_key_override_for_unusable_ip_pin(None, method))
