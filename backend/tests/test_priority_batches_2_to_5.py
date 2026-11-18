"""Batches 2-5 integration tests: returning guests, amenities source stock,
manual stock-out shortage reporting, and Mini Mart payment methods.
"""
from decimal import Decimal
from unittest.mock import patch

from rest_framework.exceptions import ValidationError

from apps.accounts.models import User
from apps.bookings.models import Booking, Guest
from apps.core.utils import hotel_today
from apps.finance.models import CashSession, FinancialLine, FinancialTransaction
from apps.finance.services import accounting
from apps.finance.services.cash_session_service import open_cash_session
from apps.guest_services.models import MiniMartProduct, MiniMartSale
from apps.guest_services.services_amenities import reception_location
from apps.inventory.models import StockBalance, StockItem, StockLocation, StockMovement
from apps.inventory.services.inventory_service import record_stock_movement
from apps.pos.models import PosTender
from tests.base import BaseAPITestCase
from tests.factories import make_guest, make_room, make_room_type, make_staff

BOOKINGS = "/api/admin/bookings/"
GUESTS = "/api/admin/guests/"
AMENITIES = "/api/admin/service-requests/reception/amenities/"
MART = "/api/admin/service-requests/reception/minimart/"


# ---------------------------------------------------------------------------
# Batch 2 — returning guest selection during booking creation
# ---------------------------------------------------------------------------
class ReturningGuestBookingTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.staff = make_staff("desk@staff.dev", role=User.Role.RECEPTIONIST)
        self.auth(self.staff)
        self.room_type = make_room_type()
        self.room = make_room(self.room_type, "101")
        self.returning = make_guest("returning@example.com", first_name="Ada", last_name="Obi")
        self.returning_booking = Booking.objects.create(
            booking_reference="J1-OLD-0001", guest=self.returning, room_type=self.room_type,
            check_in=hotel_today() - __import__("datetime").timedelta(days=40),
            check_out=hotel_today() - __import__("datetime").timedelta(days=37),
            number_of_rooms=1, adults=1, children=0, currency="NGN",
            price_per_night=Decimal("15000.00"), subtotal=Decimal("30000.00"),
            discount_amount=Decimal("0.00"), extra_guest_fee_amount=Decimal("0.00"),
            tax_amount=Decimal("0.00"), total_amount=Decimal("30000.00"),
            required_payment=Decimal("30000.00"), amount_paid=Decimal("30000.00"),
            status=Booking.Status.CHECKED_OUT, payment_status=Booking.PaymentStatus.PAID,
        )

    def payload(self, **overrides):
        data = {
            "room_type": self.room_type.pk,
            "room_id": self.room.pk,
            "check_in": str(hotel_today() + __import__("datetime").timedelta(days=5)),
            "check_out": str(hotel_today() + __import__("datetime").timedelta(days=7)),
            "rooms": 1, "adults": 1, "children": 0,
            "source": "WALK_IN", "status": "CONFIRMED",
            "guest": {"first_name": "Ada", "last_name": "Obi",
                      "email": "returning@example.com", "phone": self.returning.phone},
        }
        data.update(overrides)
        return data

    def test_guest_search_finds_records_by_name_phone_and_email(self):
        for term in ("Ada", "Obi", self.returning.phone, "returning@example.com"):
            response = self.client.get(GUESTS, {"search": term})
            self.assertEqual(response.status_code, 200, term)
            ids = [row["id"] for row in response.data["data"]]
            self.assertIn(self.returning.pk, ids, f"search by {term!r} found the guest")

    def test_selecting_a_returning_guest_reuses_the_same_record(self):
        before = Guest.objects.count()
        response = self.client.post(
            BOOKINGS, self.payload(guest_id=self.returning.pk), format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Guest.objects.count(), before, "no duplicate guest was created")
        booking = Booking.objects.get(booking_reference=response.data["data"]["booking_reference"])
        self.assertEqual(booking.guest_id, self.returning.pk)
        # History preserved: the earlier booking still belongs to the guest.
        self.assertEqual(self.returning.bookings.count(), 2)

    def test_guest_id_wins_over_a_differently_named_guest_dict(self):
        """A phone booking must never be merged into another person's profile."""
        other = make_guest("other@example.com", first_name="Bola", last_name="Ade")
        response = self.client.post(
            BOOKINGS,
            self.payload(guest_id=other.pk, guest={"first_name": "Ada", "last_name": "Obi",
                                                   "email": "returning@example.com",
                                                   "phone": self.returning.phone}),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        booking = Booking.objects.get(booking_reference=response.data["data"]["booking_reference"])
        self.assertEqual(booking.guest_id, other.pk)

    def test_the_guest_only_needs_an_id(self):
        response = self.client.post(BOOKINGS, self.payload(guest_id=self.returning.pk, guest={}), format="json")
        self.assertEqual(response.status_code, 201, response.data)

    def test_unknown_guest_id_is_refused(self):
        response = self.client.post(BOOKINGS, self.payload(guest_id=999999), format="json")
        self.assertEqual(response.status_code, 400)

    def test_new_guest_is_still_created_when_no_id_is_supplied(self):
        before = Guest.objects.count()
        payload = self.payload()
        payload["guest"] = {"first_name": "Fresh", "last_name": "Guest",
                            "email": "fresh@example.com", "phone": "08039990000"}
        response = self.client.post(BOOKINGS, payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Guest.objects.count(), before + 1)


# ---------------------------------------------------------------------------
# Batch 3 — amenities: the allocate pickers read the capability-scoped source
# ---------------------------------------------------------------------------
class AmenitySourceStockTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.storekeeper = make_staff("store@staff.dev", role=User.Role.STOREKEEPER)
        self.receptionist = make_staff("front@staff.dev", role=User.Role.RECEPTIONIST)
        self.store = StockLocation.objects.create(code="MAIN", name="Main Store")
        self.reception = reception_location()
        if self.reception is None:
            self.reception = StockLocation.objects.create(code="RECEPTION", name="Reception", is_reception=True)
        self.soap = StockItem.objects.create(
            sku="AMN-SOAP", name="Guest Soap", category="Amenities",
            item_type="CONSUMABLE", base_unit="BAR", standard_cost="50", reorder_level="5",
        )
        self.towel = StockItem.objects.create(
            sku="AMN-TOWEL", name="Hand Towel", category="Amenities",
            item_type="CONSUMABLE", base_unit="PIECE", standard_cost="200", reorder_level="2",
        )
        record_stock_movement(item=self.soap, location=self.store, movement_type="OPENING",
                              quantity_delta="40", source_key="amenity-opening-soap")
        record_stock_movement(item=self.towel, location=self.store, movement_type="OPENING",
                              quantity_delta="10", source_key="amenity-opening-towel")

    def test_allocator_sees_real_stores_items_and_quantities(self):
        self.auth(self.storekeeper)
        response = self.client.get(AMENITIES + "source-stock/")
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        codes = {row["code"] for row in data["locations"]}
        self.assertIn("MAIN", codes)
        self.assertNotIn(self.reception.code, codes, "the Reception shelf is not a source")
        skus = {row["sku"] for row in data["items"]}
        self.assertTrue({"AMN-SOAP", "AMN-TOWEL"} <= skus)
        stock = {row["sku"]: row for row in data["stock"]}
        self.assertEqual(Decimal(stock["AMN-SOAP"]["quantity"]), Decimal("40"))
        self.assertEqual(stock["AMN-SOAP"]["location_code"], "MAIN")
        self.assertEqual(stock["AMN-SOAP"]["base_unit"], "BAR")

    def test_search_narrows_items_and_stores_server_side(self):
        self.auth(self.storekeeper)
        by_item = self.client.get(AMENITIES + "source-stock/", {"search": "soap"})
        self.assertEqual([r["sku"] for r in by_item.data["data"]["stock"]], ["AMN-SOAP"])
        by_store = self.client.get(AMENITIES + "source-stock/", {"search": "main"})
        self.assertTrue(by_store.data["data"]["stock"], "store search returns its stock")

    def test_receptionist_cannot_allocate_but_can_read_the_shelf(self):
        self.auth(self.receptionist)
        self.assertEqual(self.client.get(AMENITIES + "source-stock/").status_code, 403)
        self.assertEqual(self.client.get(AMENITIES + "stock/").status_code, 200)

    def test_allocation_deducts_the_issuing_store_and_records_history(self):
        self.auth(self.storekeeper)
        response = self.client.post(
            AMENITIES + "allocate/",
            {"item_id": self.soap.pk, "quantity": "12", "from_location_id": self.store.pk,
             "idempotency_key": "alloc-1"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            StockBalance.objects.get(item=self.soap, location=self.store).quantity_on_hand,
            Decimal("28"),
        )
        self.assertEqual(
            StockBalance.objects.get(item=self.soap, location=self.reception).quantity_on_hand,
            Decimal("12"),
        )
        moves = StockMovement.objects.filter(item=self.soap, source_key__startswith="transfer:")
        self.assertEqual(moves.count(), 2, "one transfer-out and one transfer-in")
        self.assertEqual(
            {m.type for m in moves},
            {StockMovement.Type.TRANSFER_OUT, StockMovement.Type.TRANSFER_IN},
        )

    def test_allocation_beyond_available_stock_is_refused_with_the_numbers(self):
        self.auth(self.storekeeper)
        response = self.client.post(
            AMENITIES + "allocate/",
            {"item_id": self.towel.pk, "quantity": "50", "from_location_id": self.store.pk,
             "idempotency_key": "alloc-too-much"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        text = str(response.data)
        self.assertIn("10", text, "the available quantity is stated")
        self.assertIn("50", text, "the requested quantity is stated")
        self.assertEqual(
            StockBalance.objects.get(item=self.towel, location=self.store).quantity_on_hand,
            Decimal("10"),
            "stock was not moved",
        )


# ---------------------------------------------------------------------------
# Batch 4 — manual stock-out: valid issues work, genuine shortages explain
# ---------------------------------------------------------------------------
class ManualStockOutTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.storekeeper = make_staff("mso@staff.dev", role=User.Role.STOREKEEPER)
        self.auth(self.storekeeper)
        self.store = StockLocation.objects.create(code="DRY", name="Dry Store")
        self.kitchen = StockLocation.objects.create(code="KIT", name="Kitchen")
        self.destination = StockLocation.objects.create(code="BAR", name="Bar Store")
        self.rice = StockItem.objects.create(
            sku="RICE-50", name="Rice 50kg bag", category="Food",
            item_type="RECIPE_INGREDIENT", base_unit="KG", standard_cost="900",
            reorder_level="10", units_per_pack="50",
        )
        record_stock_movement(item=self.rice, location=self.store, movement_type="OPENING",
                              quantity_delta="100", source_key="mso-opening-rice")
        self.endpoint = "/api/admin/inventory/manual-stock-out/"

    def test_valid_stock_out_in_the_base_unit(self):
        response = self.client.post(self.endpoint, {
            "item_id": self.rice.pk, "source_location_id": self.store.pk, "quantity": "25",
            "reason": "GENERAL", "remarks": "Housekeeping service",
            "idempotency_key": "mso-ok-1",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            StockBalance.objects.get(item=self.rice, location=self.store).quantity_on_hand,
            Decimal("75"),
        )
        self.assertEqual(
            StockMovement.objects.filter(item=self.rice, type=StockMovement.Type.ISSUE).count(), 1
        )

    def test_stock_out_using_a_pack_unit_converts_exactly(self):
        response = self.client.post(self.endpoint, {
            "item_id": self.rice.pk, "source_location_id": self.store.pk, "quantity": "1",
            "unit": "BAG", "conversion_factor": "50",
            "reason": "GENERAL", "idempotency_key": "mso-bag-1",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            StockBalance.objects.get(item=self.rice, location=self.store).quantity_on_hand,
            Decimal("50"),
        )

    def test_genuine_shortage_states_requested_and_available_and_changes_nothing(self):
        before = StockBalance.objects.get(item=self.rice, location=self.store).quantity_on_hand
        response = self.client.post(self.endpoint, {
            "item_id": self.rice.pk, "source_location_id": self.store.pk, "quantity": "150",
            "reason": "GENERAL", "idempotency_key": "mso-short-1",
        }, format="json")
        self.assertEqual(response.status_code, 400)
        message = str(response.data)
        self.assertIn("150", message)
        self.assertIn("100", message)
        self.assertIn("KG", message)
        self.assertIn("DRY", message)
        self.assertEqual(
            StockBalance.objects.get(item=self.rice, location=self.store).quantity_on_hand, before
        )
        self.assertEqual(StockMovement.objects.filter(item=self.rice, type="ISSUE").count(), 0)

    def test_the_selected_store_must_hold_the_stock(self):
        response = self.client.post(self.endpoint, {
            "item_id": self.rice.pk, "source_location_id": self.kitchen.pk, "quantity": "5",
            "reason": "GENERAL", "idempotency_key": "mso-wrong-store",
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("KIT", str(response.data))

    def test_store_to_store_transfer_uses_the_same_guarded_path(self):
        response = self.client.post(self.endpoint, {
            "item_id": self.rice.pk, "source_location_id": self.store.pk,
            "destination_location_id": self.destination.pk, "quantity": "20",
            "reason": "GENERAL", "recipient": "Staff Musa", "idempotency_key": "mso-transfer-1",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            StockBalance.objects.get(item=self.rice, location=self.destination).quantity_on_hand,
            Decimal("20"),
        )
        self.assertEqual(
            StockBalance.objects.get(item=self.rice, location=self.store).quantity_on_hand,
            Decimal("80"),
        )

    def test_duplicate_submission_creates_one_movement(self):
        payload = {
            "item_id": self.rice.pk, "source_location_id": self.store.pk, "quantity": "5",
            "reason": "GENERAL", "idempotency_key": "mso-dupe-1",
        }
        self.client.post(self.endpoint, payload, format="json")
        self.client.post(self.endpoint, payload, format="json")
        self.assertEqual(StockMovement.objects.filter(item=self.rice, type="ISSUE").count(), 1)

    def test_negative_stock_is_never_permitted(self):
        """The protection itself is intact — it just explains itself now."""
        from django.core.exceptions import ValidationError as DjangoValidationError
        with self.assertRaises(DjangoValidationError) as caught:
            record_stock_movement(
                item=self.rice, location=self.store, movement_type="ISSUE",
                quantity_delta="-500", source_key="direct-negative-1", actor=self.storekeeper,
            )
        self.assertIn("Insufficient stock at DRY", str(caught.exception))
        self.assertIn("requested 500", str(caught.exception).lower())
        self.assertIn("available 100", str(caught.exception).lower())
        self.assertEqual(
            StockBalance.objects.get(item=self.rice, location=self.store).quantity_on_hand,
            Decimal("100"),
        )


# ---------------------------------------------------------------------------
# Batch 5 — Mini Mart payment methods
# ---------------------------------------------------------------------------
class MiniMartPaymentMethodTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.staff = make_staff("mart5@staff.dev", role=User.Role.RECEPTIONIST)
        self.auth(self.staff)
        self.shelf = StockLocation.objects.create(code="MART", name="Mini Mart shelf")
        self.item = StockItem.objects.create(
            sku="MM-WATER", name="Bottled water", category="Drinks",
            item_type="SELLABLE_PRODUCT", base_unit="BOTTLE", standard_cost="80",
            reorder_level="3",
        )
        self.product = MiniMartProduct.objects.create(
            item=self.item, location=self.shelf, selling_price="150"
        )
        record_stock_movement(item=self.item, location=self.shelf, movement_type="OPENING",
                              quantity_delta="20", source_key="mart5-opening")
        self.session = open_cash_session(cashier=self.staff, actor=self.staff)
        self.base_payload = {
            "expected_total": "300.00",
            "lines": [{"product_id": self.product.pk, "quantity": 2}],
        }

    def checkout(self, **extra):
        payload = dict(self.base_payload)
        payload.update(extra)
        return self.client.post(MART + "checkout/", payload, format="json")

    def test_catalogue_advertises_every_supported_method(self):
        response = self.client.get(MART + "products/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.data["data"]["payment_methods"]),
            {"CASH", "CARD", "BANK_TRANSFER", "PAYSTACK"},
        )

    def test_cash_sale_still_requires_an_open_session(self):
        response = self.checkout(idempotency_key="mm-cash-nosession", payment_method="CASH")
        self.assertEqual(response.status_code, 400)

    def test_cash_sale_records_a_cash_collection(self):
        response = self.checkout(idempotency_key="mm-cash-1", payment_method="CASH",
                                 cash_session_reference=self.session.reference)
        self.assertEqual(response.status_code, 200, response.data)
        sale = MiniMartSale.objects.get()
        tender = sale.order.tenders.get()
        self.assertEqual(tender.method, PosTender.Method.CASH)
        self.assertEqual(Decimal(tender.amount), Decimal("300.00"))
        self.assertEqual(sale.order.settlement_status, "PAID")
        collection = tender.collection_transaction
        debits = {l.account_code: Decimal(l.amount) for l in
                  collection.lines.filter(direction=FinancialLine.Direction.DEBIT)}
        self.assertEqual(debits[accounting.CASH_ON_HAND], Decimal("300.00"))

    def test_bank_transfer_sale_records_a_bank_collection(self):
        response = self.checkout(idempotency_key="mm-trf-1", payment_method="BANK_TRANSFER",
                                 transaction_reference="TRF-MM-0001")
        self.assertEqual(response.status_code, 200, response.data)
        tender = PosTender.objects.get()
        self.assertEqual(tender.method, PosTender.Method.BANK_TRANSFER)
        self.assertEqual(tender.external_reference, "TRF-MM-0001")
        collection = tender.collection_transaction
        debits = {l.account_code: Decimal(l.amount) for l in
                  collection.lines.filter(direction=FinancialLine.Direction.DEBIT)}
        self.assertEqual(debits[accounting.BANK_CLEARING], Decimal("300.00"))
        self.assertIsNone(tender.cash_session_id, "a transfer does not touch the cash drawer")

    def test_pos_card_sale_records_a_card_collection(self):
        response = self.checkout(idempotency_key="mm-pos-1", payment_method="CARD",
                                 transaction_reference="POS-SLIP-7731")
        self.assertEqual(response.status_code, 200, response.data)
        tender = PosTender.objects.get()
        self.assertEqual(tender.method, PosTender.Method.CARD)
        collection = tender.collection_transaction
        debits = {l.account_code: Decimal(l.amount) for l in
                  collection.lines.filter(direction=FinancialLine.Direction.DEBIT)}
        self.assertEqual(debits[accounting.CARD_CLEARING], Decimal("300.00"))

    def test_electronic_sale_requires_a_reference(self):
        response = self.checkout(idempotency_key="mm-noref-1", payment_method="CARD")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(MiniMartSale.objects.exists())

    def test_paystack_reference_is_verified_before_the_sale_counts_as_paid(self):
        with patch("apps.guest_services.minimart_service._verify_paystack_reference",
                   side_effect=ValidationError({"transaction_reference": "Paystack verification failed."})):
            response = self.checkout(idempotency_key="mm-ps-fail", payment_method="PAYSTACK",
                                     transaction_reference="PSK-BAD")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(MiniMartSale.objects.exists())
        self.assertEqual(
            StockBalance.objects.get(item=self.item, location=self.shelf).quantity_on_hand,
            Decimal("20"),
            "a failed verification must not move stock",
        )

    def test_verified_paystack_sale_settles_through_bank_clearing(self):
        with patch("apps.guest_services.minimart_service._verify_paystack_reference",
                   return_value=True):
            response = self.checkout(idempotency_key="mm-ps-ok", payment_method="PAYSTACK",
                                     transaction_reference="PSK-GOOD-1")
        self.assertEqual(response.status_code, 200, response.data)
        tender = PosTender.objects.get()
        self.assertEqual(tender.method, PosTender.Method.PAYSTACK)
        collection = tender.collection_transaction
        debits = {l.account_code: Decimal(l.amount) for l in
                  collection.lines.filter(direction=FinancialLine.Direction.DEBIT)}
        self.assertEqual(debits[accounting.BANK_CLEARING], Decimal("300.00"))
        self.assertEqual(
            StockBalance.objects.get(item=self.item, location=self.shelf).quantity_on_hand,
            Decimal("18"),
        )

    def test_partial_payment_leaves_the_balance_outstanding(self):
        response = self.checkout(idempotency_key="mm-part-1", payment_method="CARD",
                                 transaction_reference="POS-PART-1", payment_amount="100.00")
        self.assertEqual(response.status_code, 200, response.data)
        tender = PosTender.objects.get()
        self.assertEqual(Decimal(tender.amount), Decimal("100.00"))
        self.assertEqual(tender.order.settlement_status, "PARTIALLY_PAID")

    def test_duplicate_checkout_does_not_double_charge_or_double_deplete(self):
        payload = dict(idempotency_key="mm-dupe-1", payment_method="CASH",
                       cash_session_reference=self.session.reference)
        first = self.checkout(**payload)
        second = self.checkout(**payload)
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertFalse(second.data["data"]["created"])
        self.assertEqual(MiniMartSale.objects.count(), 1)
        self.assertEqual(PosTender.objects.count(), 1)
        self.assertEqual(
            StockBalance.objects.get(item=self.item, location=self.shelf).quantity_on_hand,
            Decimal("18"),
        )
        self.assertEqual(
            FinancialTransaction.objects.filter(
                type=FinancialTransaction.Type.PAYMENT_COLLECTION,
                source_reference=MiniMartSale.objects.get().order.reference,
            ).count(),
            1,
        )
