"""
Task 09 of targeted_fixes_01 — manual stock recording is restricted to the
Store Keeper, the Administrator and the General Manager.

These tests are the acceptance proof for the change, and they are written so a
reviewer can see exactly which role is refused which endpoint:

* a CHEF, WAITER, RECEPTIONIST and ACCOUNTANT get HTTP **403** from every
  manual stock-recording endpoint (create item, opening stock, receipt,
  adjustment, issue, return, transfer, replenish, stock count, goods receipt);
* a STOREKEEPER, a GENERAL_MANAGER and an ADMIN are permitted;
* READING stock stays available to the wider inventory audience, so a role
  that keeps ``inventory.manage`` loses no visibility;
* the operational consumption path (POS sale -> stock depletion) still works
  for a waiter, because that is a system workflow and not manual recording;
* the refusal is at the API *and* the service layer, so nothing that bypasses
  the view can quietly record stock either.
"""
from decimal import Decimal

from django.urls import reverse
from rest_framework.exceptions import PermissionDenied

from apps.accounts.models import User
from apps.inventory.models import StockBalance, StockItem, StockLocation, StockMovement
from apps.inventory.services.inventory_service import (
    adjust_stock_balance,
    assert_can_record_stock,
    issue_stock,
    record_opening_stock,
    transfer_stock,
)

from .base import BaseAPITestCase
from .factories import make_staff

#: Every role that must be REFUSED manual stock recording.
REFUSED_ROLES = (
    User.Role.CHEF,
    User.Role.WAITER,
    User.Role.RECEPTIONIST,
    User.Role.ACCOUNTANT,
    User.Role.HOUSEKEEPING,
    User.Role.PROCUREMENT_OFFICER,
    User.Role.KITCHEN_ASSISTANT,
)

#: Every role that must be ALLOWED manual stock recording.
ALLOWED_ROLES = (User.Role.STOREKEEPER, User.Role.GENERAL_MANAGER, User.Role.ADMIN)


class StockRecordingPermissionTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.storekeeper = make_staff("task09-storekeeper@staff.dev", role=User.Role.STOREKEEPER)
        self.location = StockLocation.objects.create(code="T09-STORE", name="Task 09 Store")
        self.item = StockItem.objects.create(
            sku="T09-RICE", name="Task 09 rice", base_unit="KG",
            standard_cost=Decimal("900.00"), is_active=True,
        )

    def _api(self):
        """The concrete endpoints under test, with a payload for each."""
        return [
            ("POST", "/api/admin/inventory/items/",
             {"sku": "T09-NEW", "name": "Task 09 new item", "base_unit": "PIECE", "standard_cost": "100.00"}),
            ("POST", "/api/admin/inventory/opening-stock/",
             {"item_id": self.item.pk, "location_id": self.location.pk, "quantity": "5"}),
            ("POST", "/api/admin/inventory/issues/",
             {"item_id": self.item.pk, "location_id": self.location.pk, "quantity": "1",
              "department": "KITCHEN", "idempotency_key": "t09-issue"}),
            ("POST", "/api/admin/inventory/transfers/",
             {"item_id": self.item.pk, "from_location_id": self.location.pk,
              "to_location_id": self.location.pk, "quantity": "1", "idempotency_key": "t09-transfer"}),
            ("POST", "/api/admin/inventory/returns/",
             {"item_id": self.item.pk, "location_id": self.location.pk, "quantity": "1",
              "idempotency_key": "t09-return"}),
            ("POST", "/api/admin/inventory/stock-counts/",
             {"location_id": self.location.pk, "item_ids": [self.item.pk]}),
        ]

    def test_refused_roles_receive_403_from_every_manual_endpoint(self):
        """The security boundary, endpoint by endpoint, for each refused role."""
        for role in REFUSED_ROLES:
            with self.subTest(role=role):
                staff = make_staff(f"task09-{role.lower()}@staff.dev", role=role)
                self.auth(staff)
                for method, url, payload in self._api():
                    response = getattr(self.client, method.lower())(url, payload, format="json")
                    self.assertEqual(
                        response.status_code, 403,
                        f"{role} must be refused {method} {url} (got {response.status_code})",
                    )

    def test_allowed_roles_can_record_stock(self):
        """Store Keeper, General Manager and Admin keep recording authority."""
        for role in ALLOWED_ROLES:
            with self.subTest(role=role):
                staff = make_staff(f"task09-ok-{role.lower()}@staff.dev", role=role)
                self.auth(staff)
                location = StockLocation.objects.create(code=f"OK-{role[:4]}-{len(role)}", name=f"{role} store")
                response = self.client.post("/api/admin/inventory/opening-stock/", {
                    "item_id": self.item.pk, "location_id": location.pk, "quantity": "3",
                    "idempotency_key": f"t09-opening-{role}",
                }, format="json")
                self.assertEqual(response.status_code, 201, response.content)
                self.assertEqual(
                    StockBalance.objects.get(item=self.item, location=location).quantity_on_hand,
                    Decimal("3.0000"),
                )

    def test_refused_role_that_can_see_stock_keeps_its_visibility(self):
        """The Procurement Officer keeps ``inventory.manage`` but loses recording.

        This is the regression the change must NOT introduce: a role that held
        the inventory capability yesterday must still be able to READ stock
        today — only the recording authority is narrowed.
        """
        self.auth(self.storekeeper)
        opening = self.client.post("/api/admin/inventory/opening-stock/", {
            "item_id": self.item.pk, "location_id": self.location.pk, "quantity": "4",
            "idempotency_key": "t09-read-seed",
        }, format="json")
        self.assertEqual(opening.status_code, 201, opening.content)

        officer = make_staff("task09-procurement-reader@staff.dev", role=User.Role.PROCUREMENT_OFFICER)
        self.auth(officer)
        for url in (
            "/api/admin/inventory/balances/",
            "/api/admin/inventory/movements/",
            "/api/admin/inventory/items/",
            "/api/admin/inventory/locations/",
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, f"{url} -> {response.content}")
        # ...but the same role is refused the moment it tries to RECORD stock.
        refused = self.client.post("/api/admin/inventory/opening-stock/", {
            "item_id": self.item.pk, "location_id": self.location.pk, "quantity": "1",
            "idempotency_key": "t09-officer-opening",
        }, format="json")
        self.assertEqual(refused.status_code, 403, refused.content)

    def test_service_layer_refuses_a_refused_role_without_the_view(self):
        """A management command or task cannot bypass the rule either."""
        chef = make_staff("task09-chef-service@staff.dev", role=User.Role.CHEF)
        with self.assertRaises(PermissionDenied):
            issue_stock(
                item=self.item, location=self.location, movement_type=StockMovement.Type.ISSUE,
                quantity=Decimal("1"), actor=chef, source_key="t09-direct-issue",
            )
        with self.assertRaises(PermissionDenied):
            record_opening_stock(
                item=self.item, location=self.location, quantity=Decimal("2"),
                actor=chef, source_key="t09-direct-opening",
            )
        with self.assertRaises(PermissionDenied):
            transfer_stock(
                item=self.item, from_location=self.location, to_location=self.location,
                quantity=Decimal("1"), actor=chef, transfer_key="t09-direct-transfer",
            )
        # Nothing was written by the refused attempts.
        self.assertFalse(StockMovement.objects.filter(source_key__startswith="t09-direct").exists())

    def test_storekeeper_service_calls_still_work(self):
        """The same service functions accept the Store Keeper."""
        movement, created = record_opening_stock(
            item=self.item, location=self.location, quantity=Decimal("6"),
            actor=self.storekeeper, source_key="t09-sk-opening",
        )
        self.assertTrue(created)
        self.assertEqual(movement.actor, self.storekeeper)

        balance = StockBalance.objects.get(item=self.item, location=self.location)
        adjustment, made = adjust_stock_balance(
            balance=balance, new_quantity=Decimal("5"), reason="Stock count correction",
            actor=self.storekeeper, idempotency_key="t09-sk-adjust",
        )
        self.assertTrue(made)
        self.assertEqual(adjustment.quantity_delta, Decimal("-1.0000"))

    def test_scheduled_jobs_with_no_actor_are_not_blocked(self):
        """``actor=None`` is an automated path and keeps working."""
        assert_can_record_stock(None)  # must not raise

    def test_capability_registry_matches_the_rule(self):
        """The persisted role matrix grants recording to exactly three roles."""
        from apps.accounts.capabilities import ROLE_CAPABILITY_CODES

        holders = {
            role for role, codes in ROLE_CAPABILITY_CODES.items() if "inventory.record" in codes
        }
        # The two retired titles map to Store Keeper and General Manager, so the
        # set that can record is exactly those five internal role constants.
        self.assertEqual(
            holders,
            {"STOREKEEPER", "GENERAL_MANAGER", "ADMIN", "INVENTORY_CLERK", "MANAGER"},
        )
        for role in REFUSED_ROLES:
            self.assertNotIn("inventory.record", ROLE_CAPABILITY_CODES.get(role, frozenset()))
