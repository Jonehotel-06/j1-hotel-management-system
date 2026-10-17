"""Pieces vs pack/bag: one unit contract for stock items.

The reported defect was stock being recorded as a "crate" or "bag" while the
physical count was in pieces. The ledger now counts an item's own base unit, and
an item that declares a pack size must be counted in PIECE — the pack size is a
conversion reference, never the counted unit.

Run: python manage.py test tests.test_inventory_unit_contract
"""
from apps.accounts.models import User
from apps.inventory.models import StockItem

from .base import BaseAPITestCase
from .factories import make_staff


class StockItemUnitContractTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.clerk = make_staff("unit-contract-clerk@staff.dev", role=User.Role.INVENTORY_CLERK)
        self.auth(self.clerk)

    def _create(self, **overrides):
        payload = {"sku": "UNIT-1", "name": "Sachet water", "standard_cost": "50.00"}
        payload.update(overrides)
        # An explicit None removes the per-piece cost so the pack-price
        # derivation is what the request is actually exercising.
        payload = {key: value for key, value in payload.items() if value is not None}
        return self.client.post("/api/admin/inventory/items/", payload, format="json")

    def test_a_pack_can_no_longer_be_the_counted_unit_when_its_size_is_known(self):
        response = self._create(base_unit="PACK", units_per_pack="24")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("PIECE", str(response.json()))

    def test_the_same_rule_applies_to_bags_and_crates(self):
        for unit in ("BAG", "CRATE", "CARTON"):
            with self.subTest(unit=unit):
                response = self._create(sku=f"UNIT-{unit}", base_unit=unit, units_per_pack="12")
                self.assertEqual(response.status_code, 400, response.content)

    def test_the_unit_is_normalised_to_upper_case(self):
        response = self._create(sku="UNIT-PIECE", base_unit="piece")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["data"]["base_unit"], "PIECE")

    def test_a_legacy_pack_sized_item_without_a_declared_size_still_saves(self):
        # A container unit with no pack size cannot be converted, so it is left
        # exactly as recorded rather than guessed.
        response = self._create(sku="UNIT-LEGACY", base_unit="PACK")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(StockItem.objects.get(sku="UNIT-LEGACY").base_unit, "PACK")

    def test_the_pack_price_derives_a_per_piece_standard_cost(self):
        response = self._create(
            sku="UNIT-DERIVE", base_unit="PIECE", units_per_pack="24", pack_price="2400.00",
            standard_cost=None,
        )
        self.assertEqual(response.status_code, 201, response.content)
        item = StockItem.objects.get(sku="UNIT-DERIVE")
        self.assertEqual(str(item.standard_cost), "100.00")
