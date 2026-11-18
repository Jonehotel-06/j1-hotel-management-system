"""Mini Mart room-charge (guest folio) checkout tests.

The room charge is the only new settlement path added by this repair: it must
post the sale onto the guest's existing main folio exactly once, never create a
tender or cash movement, and never claim the sale was paid.
"""
from decimal import Decimal

from apps.accounts.models import User
from apps.bookings.models import Guest
from apps.finance.models import CashMovement, FinancialTransaction, FolioPosting
from apps.finance.services.cash_session_service import open_cash_session
from apps.finance.services.folio_service import folio_balance_queryset
from apps.guest_services.minimart_service import complete_sale
from apps.guest_services.models import MiniMartProduct, MiniMartSale
from apps.inventory.models import StockBalance, StockItem, StockLocation
from apps.inventory.services.inventory_service import record_stock_movement
from apps.pos.models import PosOrder, PosTender
from apps.stays.models import Stay
from tests.base import BaseAPITestCase
from tests.factories import make_booking, make_guest, make_room, make_room_type, make_staff
from tests.portal_stay_helpers import make_in_house_stay

MART = "/api/admin/service-requests/reception/minimart/"


class RoomChargeFixtures:
    def room_charge_setup(self):
        self.staff = make_staff("mart-room@staff.dev", role=User.Role.RECEPTIONIST)
        self.store = StockLocation.objects.create(code="RECEPTION", name="Reception retail shelf")
        self.item = StockItem.objects.create(
            sku="TEST-ROOM-WATER", name="Room charge bottled water", category="Test drinks",
            item_type="SELLABLE_PRODUCT", base_unit="BOTTLE", standard_cost="20", reorder_level="2",
        )
        self.product = MiniMartProduct.objects.create(item=self.item, location=self.store, selling_price="150")
        record_stock_movement(
            item=self.item, location=self.store, movement_type="OPENING", quantity_delta="5",
            source_key="test-mart-room-opening",
        )
        self.session = open_cash_session(cashier=self.staff, actor=self.staff)
        room_type = make_room_type()
        self.room = make_room(room_type, "401")
        self.guest = make_guest("inhouse@test.dev")
        self.booking = make_booking(self.guest, room_type, rooms=[self.room])
        self.stay = make_in_house_stay(self.booking)

    def payload(self, **overrides):
        data = {
            "expected_total": "300.00",
            "idempotency_key": "test-room-charge",
            "payment_method": "ROOM_CHARGE",
            "guest_id": self.guest.pk,
            "booking_id": self.booking.pk,
            "lines": [{"product_id": self.product.pk, "quantity": 2}],
        }
        data.update(overrides)
        return data

    def balance(self):
        return StockBalance.objects.get(item=self.item, location=self.store).quantity_on_hand

    def folio_balance(self):
        from apps.finance.models import Folio

        return folio_balance_queryset(Folio.objects.filter(stay=self.stay)).first().balance


class MiniMartRoomChargeAPITests(RoomChargeFixtures, BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.room_charge_setup()
        self.auth(self.staff)

    def checkout(self, data=None):
        return self.client.post(MART + "checkout/", data or self.payload(), format="json")

    def test_room_charge_posts_to_folio_without_tender_or_cash(self):
        response = self.checkout()
        self.assertEqual(response.status_code, 200, response.data)
        sale = MiniMartSale.objects.get()
        order = sale.order
        # Charged, not paid: the receivable sits on the guest's folio.
        self.assertEqual(order.settlement_status, PosOrder.SettlementStatus.NOT_APPLICABLE)
        self.assertEqual(order.folio_id, sale.order.folio_id)
        self.assertEqual(order.stay_id, self.stay.pk)
        self.assertFalse(PosTender.objects.filter(order=order).exists())
        self.assertEqual(CashMovement.objects.count(), 0)
        self.session.refresh_from_db()
        self.assertEqual(self.session.expected_cash, 0)
        # Exactly one ledger charge and exactly one folio statement item.
        charges = FinancialTransaction.objects.filter(source_reference=order.reference,
                                                     type=FinancialTransaction.Type.POS_CHARGE)
        self.assertEqual(charges.count(), 1)
        self.assertTrue(charges.first().lines.filter(account_code="REVENUE.OTHER_OPERATING").exists())
        postings = FolioPosting.objects.filter(folio=order.folio, transaction=charges.first())
        self.assertEqual(postings.count(), 1)
        self.assertEqual(postings.first().kind, FolioPosting.Kind.CHARGE)
        self.assertEqual(postings.first().amount, Decimal("300.00"))
        self.assertEqual(self.folio_balance(), Decimal("300.00"))
        # Stock left the shelf once.
        self.assertEqual(self.balance(), 3)

    def test_room_charge_response_reports_method_and_zero_cash(self):
        response = self.checkout()
        self.assertEqual(response.status_code, 200, response.data)
        sale = response.data["data"]["sale"]
        self.assertEqual(sale["payment_method"], "ROOM_CHARGE")
        self.assertEqual(sale["amount_paid"], "0.00")
        self.assertEqual(sale["payment_status"], PosOrder.SettlementStatus.NOT_APPLICABLE)
        self.assertEqual(sale["customer_type"], "GUEST")
        self.assertEqual(sale["room_number"], "401")

    def test_repeated_room_charge_replays_without_double_posting(self):
        self.assertEqual(self.checkout().status_code, 200)
        replay = self.checkout()
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertFalse(replay.data["data"]["created"])
        self.assertEqual(MiniMartSale.objects.count(), 1)
        self.assertEqual(FolioPosting.objects.count(), 1)
        self.assertEqual(self.folio_balance(), Decimal("300.00"))
        self.assertEqual(self.balance(), 3)

    def test_room_charge_requires_a_booking(self):
        response = self.checkout(self.payload(booking_id=None))
        self.assertEqual(response.status_code, 400)
        self.assertIn("booking_id", str(response.data))
        self.assertFalse(MiniMartSale.objects.exists())

    def test_room_charge_requires_a_guest(self):
        response = self.checkout(self.payload(guest_id=None, booking_id=None))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(MiniMartSale.objects.exists())

    def test_room_charge_rejected_for_guest_who_has_not_checked_in(self):
        room_type = make_room_type(name="Room-charge annex")
        room = make_room(room_type, "402")
        guest = make_guest("not-in-house@test.dev")
        booking = make_booking(guest, room_type, rooms=[room])
        response = self.checkout(self.payload(
            guest_id=guest.pk, booking_id=booking.pk, idempotency_key="test-room-charge-nostay",
        ))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(MiniMartSale.objects.exists())
        self.assertEqual(self.balance(), 5)  # no stock left the shelf

    def test_room_charge_rejected_after_check_out(self):
        self.stay.status = Stay.Status.CHECKED_OUT
        self.stay.save(update_fields=["status", "updated_at"])
        response = self.checkout()
        self.assertEqual(response.status_code, 400)
        self.assertFalse(MiniMartSale.objects.exists())
        self.assertEqual(self.balance(), 5)

    def test_room_charge_never_accepts_tender_fields(self):
        for field, value in (
            ("cash_session_reference", self.session.reference),
            ("transaction_reference", "POS-1234"),
            ("payment_amount", "300.00"),
        ):
            response = self.checkout(self.payload(idempotency_key="test-room-" + field, **{field: value}))
            self.assertEqual(response.status_code, 400, field)
            self.assertIn(field, str(response.data))
        self.assertFalse(MiniMartSale.objects.exists())

    def test_walk_in_sale_still_untouched_by_room_charge(self):
        response = self.checkout({
            "expected_total": "300.00", "idempotency_key": "test-walkin-after-room",
            "cash_session_reference": self.session.reference,
            "lines": [{"product_id": self.product.pk, "quantity": 2}],
        })
        self.assertEqual(response.status_code, 200, response.data)
        order = MiniMartSale.objects.get().order
        self.assertEqual(order.settlement_status, PosOrder.SettlementStatus.PAID)
        self.assertIsNone(order.folio_id)
        self.assertEqual(PosTender.objects.count(), 1)
        self.assertFalse(FolioPosting.objects.exists())

    def test_history_reports_room_charge_sales(self):
        self.assertEqual(self.checkout().status_code, 200)
        response = self.client.get(MART + "sales/")
        self.assertEqual(response.status_code, 200)
        row = response.data["data"]["results"][0]
        self.assertEqual(row["payment_method"], "ROOM_CHARGE")
        self.assertEqual(row["amount_paid"], "0.00")
        self.assertEqual(row["customer_type"], "GUEST")

    def test_catalogue_advertises_room_charge(self):
        response = self.client.get(MART + "products/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("ROOM_CHARGE", response.data["data"]["payment_methods"])


class MiniMartRoomChargeServiceTests(RoomChargeFixtures, BaseAPITestCase):
    """Service-level guarantees that must hold without going through the API."""

    def setUp(self):
        super().setUp()
        self.room_charge_setup()

    def test_service_refuses_room_charge_for_walk_in(self):
        from rest_framework.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            complete_sale(actor=self.staff, data={
                "expected_total": "150.00", "idempotency_key": "svc-room-walkin",
                "payment_method": "ROOM_CHARGE",
                "lines": [{"product_id": self.product.pk, "quantity": 1}],
            })
        self.assertFalse(MiniMartSale.objects.exists())
        self.assertEqual(self.balance(), 5)

    def test_service_room_charge_is_idempotent_on_retry(self):
        data = self.payload(idempotency_key="svc-room-retry")
        first, created = complete_sale(actor=self.staff, data=data)
        self.assertTrue(created)
        second, created_again = complete_sale(actor=self.staff, data=data)
        self.assertFalse(created_again)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(FolioPosting.objects.count(), 1)
        self.assertEqual(self.folio_balance(), Decimal("300.00"))

    def test_guest_record_is_preserved_and_linked(self):
        sale, _ = complete_sale(actor=self.staff, data=self.payload(idempotency_key="svc-room-link"))
        sale.refresh_from_db()
        self.assertEqual(sale.guest_id, self.guest.pk)
        self.assertEqual(sale.booking_id, self.booking.pk)
        self.assertEqual(sale.room_number, "401")
        self.assertTrue(Guest.objects.filter(pk=self.guest.pk).exists())
        self.assertEqual(sale.order.guest_name, self.guest.full_name)
