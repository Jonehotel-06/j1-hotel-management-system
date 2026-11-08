"""The workstation identity must survive an IP address change.

Reported by the hotel:

    "Changing IP address breaks workstation access" — after an unexpected
    server/router shutdown the front desk was refused, and every signed-in staff
    session died at its next token refresh.

What these tests pin, in the order the failure happens:

* an operational account is still refused from a browser that has proved
  nothing (the rule is NOT weakened);
* once that browser is APPROVED as a front desk device it signs in from a
  brand-new address — the thing that used to break;
* the same device keeps a live session alive when the address changes, through
  the refresh path that used to kill it;
* the address change is RECORDED (last address + history) and visible to an
  administrator, and it never rewrites the workstation's registered address;
* a device token is bound to one staff account (no replay by another user), one
  active FRONT_DESK workstation (retiring the desk retires the device), and can
  be revoked, taking effect on the next request;
* approving a device cannot bypass a network pin that is genuinely working; it
  overrides only the one case where the saved pin can never match (a
  platform-proxy address) — exactly as the desktop key already does;
* approving and revoking are capability-gated, so a receptionist cannot approve
  their own phone.
"""
from apps.accounts.desktop_policy import DEVICE_HEADER, record_device_seen
from apps.accounts.models import FrontDeskDevice, User, Workstation
from apps.audit.models import AuditLog

from .base import BaseAPITestCase
from .factories import make_user

PASSWORD = "Str0ng!Pass"
DESK_IP = "102.89.34.7"          # the desk's original address
NEW_IP = "102.89.62.211"         # what the router hands out after a reboot
OTHER_IP = "102.89.34.99"        # some other device on the internet
PROXY_PEER = "10.0.0.14"         # the deployment's own proxy

LOGIN_URL = "/api/auth/login/"
REFRESH_URL = "/api/auth/token/refresh/"
DEVICES_URL = "/api/admin/users/terminals/devices/"
REQUEST_URL = "/api/admin/users/terminals/devices/request/"


class FrontDeskDeviceTestCase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.desk = Workstation.objects.create(
            name="Front desk desktop", department=Workstation.Department.FRONT_DESK, location="Lobby",
        )
        self.admin = make_user("dev-admin@jone.test", password=PASSWORD, role=User.Role.ADMIN)
        self.receptionist = make_user("dev-one@jone.test", password=PASSWORD, role=User.Role.RECEPTIONIST)
        self.colleague = make_user("dev-two@jone.test", password=PASSWORD, role=User.Role.RECEPTIONIST)
        self.device = FrontDeskDevice.objects.create(
            workstation=self.desk, staff=self.receptionist,
            device_label="Desk PC (Lobby)", first_enrolled_ip=DESK_IP, last_ip_address=DESK_IP,
            ip_addresses=[DESK_IP],
        )

    # -- helpers ---------------------------------------------------------------
    def approve(self, device=None):
        """An administrator approves the device; the token is returned once."""
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            f"{DEVICES_URL}{str((device or self.device).pk)}/approve/",
            {"staff": (device or self.device).staff_id}, format="json", REMOTE_ADDR=DESK_IP,
        )
        self.assertEqual(response.status_code, 200, response.content)
        token = response.json()["data"]["device_token"]
        self.client.force_authenticate(user=None)
        return token

    def login(self, user, *, ip=DESK_IP, token=None, password=PASSWORD):
        headers = {DEVICE_HEADER: token} if token else {}
        return self.client.post(
            LOGIN_URL, {"email": user.email, "password": password}, REMOTE_ADDR=ip, **headers,
        )

    def refresh(self, refresh_token, *, ip=DESK_IP, token=None):
        headers = {DEVICE_HEADER: token} if token else {}
        return self.client.post(REFRESH_URL, {"refresh": refresh_token}, REMOTE_ADDR=ip, **headers)


class DeviceAdmissionTests(FrontDeskDeviceTestCase):
    def test_the_rule_is_not_weakened_a_browser_that_proved_nothing_is_still_refused(self):
        refused = self.login(self.receptionist, ip=NEW_IP)
        self.assertEqual(refused.status_code, 403, refused.content)
        self.assertEqual(refused.json()["code"], "STAFF_SIGN_IN_RESTRICTED")
        self.assertNotIn("access", str(refused.content))

    def test_an_unapproved_token_grants_nothing(self):
        """A token that was never issued (or a revoked one) is simply not a desk."""
        self.client.force_authenticate(user=self.admin)
        self.client.post(f"{DEVICES_URL}{self.device.pk}/approve/", {"staff": self.receptionist.pk},
                         format="json", REMOTE_ADDR=DESK_IP)
        self.client.post(f"{DEVICES_URL}{self.device.pk}/revoke/", {"reason": "test"},
                         format="json", REMOTE_ADDR=DESK_IP)
        self.client.force_authenticate(user=None)
        refused = self.login(self.receptionist, ip=NEW_IP, token="not-a-real-token")
        self.assertEqual(refused.status_code, 403, refused.content)

    def test_an_approved_device_signs_in_from_a_brand_new_address(self):
        """THE REPORTED FAILURE: a new address used to mean a refused shift."""
        token = self.approve()
        response = self.login(self.receptionist, ip=NEW_IP, token=token)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()["data"]
        self.assertTrue(body["tokens"]["access"])
        self.assertEqual(body["user"]["email"], self.receptionist.email)
        entry = AuditLog.objects.filter(action="STAFF_SIGN_IN", actor=self.receptionist).latest("id")
        self.assertEqual(entry.metadata["desktop_method"], "approved_device")
        self.assertEqual(entry.metadata["client_ip"], NEW_IP)

    def test_the_address_change_is_recorded_and_does_not_rewrite_the_registration(self):
        self.desk.ip_address = DESK_IP
        self.desk.save(update_fields=["ip_address"])
        token = self.approve()
        self.assertEqual(self.login(self.receptionist, ip=NEW_IP, token=token).status_code, 200)

        self.device.refresh_from_db()
        self.assertEqual(self.device.last_ip_address, NEW_IP)
        self.assertIn(DESK_IP, self.device.ip_addresses)
        self.assertIn(NEW_IP, self.device.ip_addresses)
        self.assertIsNotNone(self.device.last_seen_at)
        # Detecting a new address never rewrites the administrator's registration.
        self.desk.refresh_from_db()
        self.assertEqual(self.desk.ip_address, DESK_IP)

    def test_a_device_token_cannot_be_replayed_by_another_staff_account(self):
        token = self.approve()
        stolen = self.login(self.colleague, ip=NEW_IP, token=token)
        self.assertEqual(stolen.status_code, 403, stolen.content)
        self.assertEqual(stolen.json()["code"], "STAFF_SIGN_IN_RESTRICTED")

    def test_retiring_the_desk_retires_its_devices(self):
        token = self.approve()
        self.assertEqual(self.login(self.receptionist, ip=NEW_IP, token=token).status_code, 200)
        Workstation.objects.filter(pk=self.desk.pk).update(is_active=False)
        refused = self.login(self.receptionist, ip=NEW_IP, token=token)
        self.assertEqual(refused.status_code, 403, refused.content)

    def test_deactivating_the_staff_account_retires_its_devices(self):
        token = self.approve()
        User.objects.filter(pk=self.receptionist.pk).update(is_active=False)
        refused = self.login(self.receptionist, ip=NEW_IP, token=token)
        self.assertIn(refused.status_code, (401, 403), refused.content)

    def test_revocation_takes_effect_on_the_next_request(self):
        token = self.approve()
        self.assertEqual(self.login(self.receptionist, ip=NEW_IP, token=token).status_code, 200)
        self.client.force_authenticate(user=self.admin)
        revoked = self.client.post(
            f"{DEVICES_URL}{self.device.pk}/revoke/", {"reason": "laptop replaced"},
            format="json", REMOTE_ADDR=DESK_IP,
        )
        self.assertEqual(revoked.status_code, 200, revoked.content)
        self.client.force_authenticate(user=None)
        self.device.refresh_from_db()
        self.assertFalse(self.device.is_active)
        self.assertIsNotNone(self.device.revoked_at)
        self.assertEqual(self.device.revoked_reason, "laptop replaced")
        refused = self.login(self.receptionist, ip=NEW_IP, token=token)
        self.assertEqual(refused.status_code, 403, refused.content)

    def test_a_working_network_pin_still_applies_to_an_approved_device(self):
        """Approval is not a bypass: a pin that CAN match is still enforced."""
        from .factories import hotel_settings

        hotel_settings(front_desk_desktop_ip="102.89.34.0/24")
        token = self.approve()
        # Inside the pin: accepted.
        self.assertEqual(self.login(self.receptionist, ip="102.89.34.55", token=token).status_code, 200)
        # Outside the pin: the device is genuine but no longer at the pinned desk.
        refused = self.login(self.receptionist, ip=NEW_IP, token=token)
        self.assertEqual(refused.status_code, 403, refused.content)
        self.assertEqual(refused.json()["errors"].get("reason"), "front_desk_ip_mismatch")

    def test_an_unusable_saved_pin_does_not_lock_out_an_approved_device(self):
        """The platform-proxy pin no browser can match is the documented exception."""
        from .factories import hotel_settings

        hotel_settings(front_desk_desktop_ip="100.64.0.18")  # the hosting proxy, never the desk
        token = self.approve()
        response = self.login(self.receptionist, ip=NEW_IP, token=token)
        self.assertEqual(response.status_code, 200, response.content)


class DeviceRefreshTests(FrontDeskDeviceTestCase):
    """The half of the outage that hurt most: live sessions dying at refresh."""

    def setUp(self):
        super().setUp()
        self.token = self.approve()
        signed_in = self.login(self.receptionist, ip=DESK_IP, token=self.token)
        self.assertEqual(signed_in.status_code, 200, signed_in.content)
        self.refresh_token = signed_in.json()["data"]["tokens"]["refresh"]

    def test_a_session_survives_the_address_changing_at_refresh(self):
        response = self.refresh(self.refresh_token, ip=NEW_IP, token=self.token)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["data"]["tokens"]["access"])

    def test_without_the_device_credential_the_same_refresh_is_refused(self):
        """The refusal the hotel reported — still in force for an unproven browser."""
        refused = self.refresh(self.refresh_token, ip=NEW_IP)
        self.assertEqual(refused.status_code, 403, refused.content)
        self.assertEqual(refused.json()["code"], "STAFF_SIGN_IN_RESTRICTED")

    def test_a_refresh_with_someone_elses_device_token_is_refused(self):
        other_device = FrontDeskDevice.objects.create(
            workstation=self.desk, staff=self.colleague, device_label="Other desk PC",
        )
        other_token = self.approve(other_device)
        refused = self.refresh(self.refresh_token, ip=NEW_IP, token=other_token)
        self.assertEqual(refused.status_code, 403, refused.content)

    def test_a_rotated_refresh_token_keeps_the_session_alive_across_addresses(self):
        """Refresh rotation must not end the device session.

        ``ROTATE_REFRESH_TOKENS`` is on, so every refresh hands back a new
        refresh token. If the device credential only covered the first refresh,
        the desk would die one rotation later — the same outage with a longer
        fuse. This walks three rotations, each from a NEW address.
        """
        token = self.token
        current = self.refresh_token
        for index, ip in enumerate(("102.89.62.10", "102.89.62.11", "102.89.62.12")):
            response = self.refresh(current, ip=ip, token=token)
            self.assertEqual(response.status_code, 200, f"rotation {index}: {response.content}")
            current = response.json()["data"]["tokens"].get("refresh") or current

    def test_an_approved_device_at_the_registered_address_still_works(self):
        """Fast path unchanged: the device credential is an addition, not a replacement."""
        response = self.refresh(self.refresh_token, ip=DESK_IP, token=self.token)
        self.assertEqual(response.status_code, 200, response.content)


class DeviceManagementTests(FrontDeskDeviceTestCase):
    def test_a_receptionist_cannot_list_approve_or_revoke_devices(self):
        for url, payload in (
            (DEVICES_URL, None),
            (f"{DEVICES_URL}{self.device.pk}/approve/", {"staff": self.receptionist.pk}),
            (f"{DEVICES_URL}{self.device.pk}/revoke/", {}),
        ):
            with self.subTest(url=url):
                self.client.force_authenticate(user=self.receptionist)
                response = (self.client.get(url) if payload is None
                            else self.client.post(url, payload, format="json"))
                self.assertEqual(response.status_code, 403, response.content)

    def test_an_anonymous_caller_cannot_manage_devices(self):
        self.client.force_authenticate(user=None)
        self.assertIn(self.client.get(DEVICES_URL).status_code, (401, 403))

    def test_the_token_is_never_serialized(self):
        self.approve()
        self.client.force_authenticate(user=self.admin)
        listing = self.client.get(DEVICES_URL, REMOTE_ADDR=DESK_IP)
        self.assertEqual(listing.status_code, 200, listing.content)
        body = str(listing.content)
        self.assertNotIn("device_token", body)
        self.device.refresh_from_db()
        self.assertTrue(self.device.token_hash, "approval stored a digest")
        self.assertNotIn(self.device.token_hash, body, "the digest must never leave the server")
        row = listing.json()["data"][0]
        self.assertEqual(row["state"], "APPROVED")
        self.assertEqual(row["workstation_reference"], self.desk.reference)
        self.assertEqual(row["last_ip_address"], DESK_IP)

    def test_an_admin_can_see_a_moved_device(self):
        token = self.approve()
        self.login(self.receptionist, ip=NEW_IP, token=token)
        self.client.force_authenticate(user=self.admin)
        listing = self.client.get(DEVICES_URL, {"state": "approved"}, REMOTE_ADDR=NEW_IP)
        row = listing.json()["data"][0]
        self.assertEqual(row["last_ip_address"], NEW_IP)
        self.assertIn(DESK_IP, row["ip_addresses"])
        self.assertIn(NEW_IP, row["ip_addresses"])

    def test_a_staff_account_can_request_approval_but_that_grants_nothing(self):
        self.client.force_authenticate(user=self.colleague)
        requested = self.client.post(
            REQUEST_URL, {"device_label": "New lobby PC"}, format="json", REMOTE_ADDR=NEW_IP,
        )
        self.assertEqual(requested.status_code, 201, requested.content)
        pending = requested.json()["data"]
        self.assertEqual(pending["state"], "PENDING")
        self.assertNotIn("device_token", str(requested.content))
        self.client.force_authenticate(user=None)
        # A pending row can never be used to sign in.
        refused = self.login(self.colleague, ip=NEW_IP, token="pending")
        self.assertEqual(refused.status_code, 403, refused.content)

    def test_approval_requires_a_named_staff_account(self):
        pending = FrontDeskDevice.objects.create(workstation=self.desk, device_label="Pending PC")
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            f"{DEVICES_URL}{pending.pk}/approve/", {}, format="json", REMOTE_ADDR=DESK_IP,
        )
        self.assertEqual(response.status_code, 400, response.content)
        pending.refresh_from_db()
        self.assertIsNone(pending.staff_id)
        self.assertTrue(pending.token_hash.startswith("pending:"),
                        "an unapproved device must hold no usable digest")

    def test_the_device_history_is_bounded(self):
        for index in range(40):
            record_device_seen(self.device, f"102.89.{index}.1")
        self.device.refresh_from_db()
        self.assertLessEqual(len(self.device.ip_addresses), 20)
        self.assertEqual(self.device.last_ip_address, "102.89.39.1")
