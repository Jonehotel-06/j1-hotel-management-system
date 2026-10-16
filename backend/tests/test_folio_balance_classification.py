"""The guest balance must not net accommodation money against folio charges.

Reported: booking ``J1-20261012-D3DF9254`` showed

    Accommodation total ₦420,000 · accommodation paid ₦420,000
    Laundry charge ₦3,200
    Purchases and charges on the folio −₦416,800
    Amount to settle ₦0

— i.e. the ₦420,000 the guest paid for the ROOM was netted against an unrelated
₦3,200 LAUNDRY charge, producing a negative folio balance and telling the desk
(and the gate) that a guest who still owed ₦3,200 had nothing to pay.

Root cause: the folio carries two different balances for one guest — the
accommodation balance (the booking's own total, reported as total/paid/due) and
the folio charges (restaurant, bar, room service, laundry). Two independent
projections summed the folio postings:

* ``booking_folio_charge_map`` EXCLUDED the nightly ``ACCOMMODATION_CHARGE``
  debits (they are the booking's own total split by night — including them would
  count the room twice) but INCLUDED the accommodation *payment* credit, so the
  same ₦420,000 counted once in ``amount_paid`` and again as a folio credit:
  ``3,200 − 420,000 = −416,800``; and
* ``settle_outstanding_balance`` reads that same figure as
  ``folio_due = max(folio_charge_balance, 0) = 0``, so ``bill_total`` became
  ``stay_due (0) + 0`` and the desk could not even collect the laundry money —
  it refused with "already fully paid".

The fix classifies each money posting against the balance it settles
(``FolioPosting.settles``): accommodation money is ``ACCOMMODATION`` and is
excluded from the folio-charge balance (it is already in ``amount_paid``), while
money the guest hands over FOR the folio charges — and every discount,
adjustment, reversal and refund that belongs to them — is still netted. Nothing
was re-posted, deleted or edited: the ledger is append-only and the fix is a
classification + read-model change.
"""
import importlib.util
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from io import StringIO

from django.apps import apps as global_apps
from django.core.management import call_command
from django.utils import timezone

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import (
    FinancialLine,
    FinancialTransaction,
    Folio,
    FolioPosting,
    PaymentAllocation,
)
from apps.finance.services import accounting
from apps.finance.services.folio_service import (
    booking_folio_charge_balance,
    booking_outstanding_balance,
    folio_balance_queryset,
)
from apps.finance.services.ledger_service import create_posted_transaction
from apps.finance.services.payment_collection_service import ensure_processed_refund
from apps.guest_services.models import LaundryOrder
from apps.inventory.models import StockItem, StockLocation, Supplier
from apps.inventory.services.inventory_service import (
    approve_purchase_order,
    create_purchase_order,
    create_stock_recipe,
    mark_purchase_order_ordered,
    receive_goods,
    submit_purchase_order,
)
from apps.payments.models import Payment, Refund
from apps.pos.models import MenuCategory, MenuItem, PosOrder
from apps.pos.services.order_service import create_order, submit_order, transition_order
from apps.stays.models import Stay
from apps.stays.services import _ensure_main_folio

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type, make_staff

LAUNDRY_URL = "/api/admin/service-requests/laundry/"
MIGRATION_FILE = (
    Path(__file__).resolve().parents[1]
    / "apps/finance/migrations/0014_classify_existing_folio_money.py"
)


def _load_migration():
    """Load the deployment pass by path (its module name starts with a digit)."""
    spec = importlib.util.spec_from_file_location("folio_money_classification_0014", MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FolioBalanceClassificationTests(BaseAPITestCase):
    """The reported booking: a fully paid room and an unpaid laundry bill."""

    def setUp(self):
        super().setUp()
        self.manager = make_staff("folio-balance-manager@staff.dev", role=User.Role.MANAGER)
        self.receptionist = make_staff("folio-balance-desk@staff.dev", role=User.Role.RECEPTIONIST)
        self.storekeeper = make_staff("folio-balance-store@staff.dev", role=User.Role.STOREKEEPER)
        self.cashier = make_staff("folio-balance-cashier@staff.dev", role=User.Role.CASHIER)
        self.today = hotel_today()
        room_type = make_room_type("Balance suite", price="140000.00")
        room = make_room(room_type, "901")
        # ₦420,000 for the stay, paid in full at booking level — the reported
        # guest's position before the laundry bill.
        self.booking = make_booking(
            make_guest("folio-balance@example.com"), room_type, rooms=[room],
            check_in=self.today, check_out=self.today + timedelta(days=3),
            total="420000.00", amount_paid="420000.00", status="CHECKED_IN",
        )
        self.stay = Stay.objects.create(
            reference=f"STY-{self.booking.booking_reference}", booking=self.booking,
            guest=self.booking.guest, status=Stay.Status.IN_HOUSE,
            expected_arrival=self.booking.check_in, expected_departure=self.booking.check_out,
        )
        # The real collection that made the booking look paid, then the real
        # check-in bridge that puts it on the folio (stays.services._ensure_main_folio
        # is the production path; this is not a hand-built posting).
        self.room_payment = Payment.objects.create(
            booking=self.booking, user=self.manager, reference="J1P-FOLIO-BALANCE-ROOM",
            provider=Payment.Provider.BANK_TRANSFER, amount=Decimal("420000.00"),
            currency="NGN", status=Payment.Status.SUCCESS, paid_at=timezone.now(),
        )
        self.folio, _ = _ensure_main_folio(stay=self.stay, actor=self.manager)

    # ------------------------------- helpers --------------------------------

    def _receive_laundry(self, *, unit_price="1600.00", quantity=2, key="folio-balance-laundry"):
        self.auth(self.manager)
        response = self.client.post(LAUNDRY_URL, {
            "stay_reference": self.stay.reference,
            "items": [{
                "description": "Shirt", "quantity": quantity,
                "service_type": "WASH_IRON", "unit_price": unit_price,
            }],
            "idempotency_key": key,
        }, format="json")
        assert response.status_code == 201, response.content
        return response.json()["data"]["reference"]

    def _deliver_laundry(self, reference):
        for status in ("IN_PROGRESS", "READY", "DELIVERED"):
            self.auth(self.manager)
            response = self.client.post(
                f"{LAUNDRY_URL}{reference}/status/", {"status": status}, format="json"
            )
            assert response.status_code == 200, response.content
        return LaundryOrder.objects.get(reference=reference)

    def _room_service_order(self, *, quantity=4, key="folio-balance-rs"):
        """A real room-service sale: configured stock, depleted, charged to the folio."""
        bottle = StockItem.objects.create(
            sku="BAL-WATER", name="Bottled water", base_unit="BOTTLE",
            standard_cost=Decimal("150.00"),
        )
        bar = StockLocation.objects.create(code="BAL-BAR", name="Balance bar")
        category = MenuCategory.objects.create(
            name="Balance room service", slug="balance-room-service",
            service_area=MenuCategory.ServiceArea.RESTAURANT,
        )
        menu_item = MenuItem.objects.create(
            category=category, name="Bottled water 75cl", sku="BAL-MENU-WATER",
            base_price=Decimal("500.00"),
        )
        create_stock_recipe(
            menu_item=menu_item, location=bar, tracking_mode="COMPONENTS",
            components=[{"item_id": bottle.pk, "quantity_per_menu_unit": "1"}],
            actor=self.storekeeper, idempotency_key="balance-water-recipe",
        )
        supplier = Supplier.objects.create(reference="SUP-BAL-1", name="Balance Supplies")
        purchase_order, _ = create_purchase_order(
            supplier=supplier, delivery_location=bar, requested_by=self.storekeeper,
            lines=[{"item_id": bottle.pk, "quantity_ordered": "24", "unit_cost": "150.00"}],
            idempotency_key="balance-po-1",
        )
        submit_purchase_order(purchase_order=purchase_order, actor=self.storekeeper)
        approve_purchase_order(purchase_order=purchase_order, actor=self.manager)
        ordered = mark_purchase_order_ordered(purchase_order=purchase_order, actor=self.storekeeper)
        receive_goods(
            purchase_order=ordered, actor=self.storekeeper, source_key="balance-receipt-1",
            receipt_lines=[{
                "purchase_order_line_id": ordered.lines.get().pk,
                "quantity_received": "24", "unit_cost": "150.00",
            }],
        )
        order, _ = create_order(
            mode=PosOrder.Mode.ROOM_SERVICE, actor=self.cashier, stay=self.stay,
            delivery_location="Suite 901",
            lines=[{"menu_item_id": menu_item.pk, "quantity": quantity}],
            idempotency_key=key,
        )
        submit_order(order=order, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.PREPARING, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.READY, actor=self.cashier)
        delivered = transition_order(
            order=order, target_status=PosOrder.Status.DELIVERED, actor=self.cashier
        )
        delivered.refresh_from_db()
        assert delivered.charge_transaction_id, "a delivered room-service order must charge the folio"
        return delivered

    def _clearance(self):
        self.auth(self.receptionist)
        response = self.client.get(
            f"/api/admin/bookings/{self.booking.booking_reference}/folio-clearance/"
        )
        assert response.status_code == 200, response.content
        return response.json()["data"]

    def _settle(self, amount, *, provider="BANK_TRANSFER", **extra):
        self.auth(self.manager)
        return self.client.post(
            f"/api/admin/bookings/{self.booking.booking_reference}/settle-balance/",
            {"amount": amount, "provider": provider, **extra}, format="json",
        )

    # -------------------- 1. the reported defect, exactly -------------------

    def test_a_paid_room_with_an_unpaid_laundry_bill_still_owes_the_laundry(self):
        reference = self._receive_laundry()
        self._deliver_laundry(reference)  # delivery bills it to the folio

        data = self._clearance()

        # The accommodation is settled and stays settled.
        self.assertEqual(data["accommodation"]["total_amount"], "420000.00")
        self.assertEqual(data["accommodation"]["amount_paid"], "420000.00")
        self.assertEqual(data["accommodation"]["amount_due"], "0.00")
        # The laundry is a charge of its own and is stated as such.
        self.assertEqual(data["charges_total"], "3200.00")
        self.assertEqual(len(data["charges"]), 1)
        self.assertEqual(data["charges"][0]["kind"], "CHARGE")
        self.assertEqual(data["charges"][0]["amount"], "3200.00")
        self.assertEqual(data["charges"][0]["source_reference"], reference)
        # NOT −₦416,800: the room payment is not a credit against laundry.
        self.assertEqual(data["credits_total"], "0.00")
        self.assertEqual(data["credits"], [])
        self.assertEqual(data["folio_balance"], "3200.00")
        self.assertEqual(data["outstanding_balance"], "3200.00")
        self.assertEqual(data["amount_to_settle"], "3200.00")
        self.assertFalse(data["cleared"])
        # The room money is still visible — as accommodation money.
        self.assertEqual(data["accommodation_settlements_total"], "420000.00")
        self.assertEqual(len(data["accommodation_settlements"]), 1)
        self.assertEqual(data["accommodation_settlements"][0]["kind"], "PAYMENT")
        self.assertEqual(data["accommodation_settlements"][0]["amount"], "420000.00")
        self.assertEqual(
            data["accommodation_settlements"][0]["settles"], FolioPosting.Settles.ACCOMMODATION
        )
        # Every figure agrees with every other figure.
        self.assertEqual(
            Decimal(data["charges_total"]) - Decimal(data["credits_total"]),
            Decimal(data["folio_balance"]),
        )
        self.assertEqual(
            Decimal(data["folio_balance"]), booking_folio_charge_balance(booking=self.booking)
        )
        self.assertEqual(
            Decimal(data["outstanding_balance"]), booking_outstanding_balance(booking=self.booking)
        )

    def test_the_desk_can_collect_the_rooms_laundry_bill(self):
        """The reported blocking symptom: the payment was refused as 'already paid'."""
        reference = self._receive_laundry()
        self._deliver_laundry(reference)

        response = self._settle("3200.00")

        self.assertEqual(response.status_code, 201, response.content)
        payload = response.json()["data"]
        self.assertEqual(payload["settlement"]["bill_total_before"], "3200.00")
        self.assertEqual(payload["settlement"]["folio_portion"], "3200.00")
        self.assertEqual(payload["settlement"]["stay_portion"], "0.00")
        self.assertEqual(str(payload["amount"]), "3200.00")
        # The money that settles folio charges is classified as such.
        allocation = PaymentAllocation.objects.get(payment__reference=payload["reference"])
        posting = FolioPosting.objects.get(
            transaction=allocation.transaction, kind=FolioPosting.Kind.PAYMENT
        )
        self.assertEqual(posting.settles, FolioPosting.Settles.FOLIO_CHARGES)
        self.assertEqual(posting.effect, FolioPosting.Effect.CREDIT)
        # …and the room's own paid-to-date is untouched by folio money.
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.amount_paid, Decimal("420000.00"))
        self.assertEqual(self.booking.amount_due, Decimal("0.00"))

        data = self._clearance()
        self.assertEqual(data["folio_balance"], "0.00")
        self.assertEqual(data["credits_total"], "3200.00")
        self.assertEqual(data["outstanding_balance"], "0.00")
        self.assertEqual(data["amount_to_settle"], "0.00")
        self.assertTrue(data["cleared"])
        self.assertTrue(data["gate_pass_allowed"])

        # Nothing left to collect: a second attempt is refused, and it never
        # invents a second payment.
        payments_before = Payment.objects.filter(booking=self.booking).count()
        again = self._settle("3200.00")
        self.assertEqual(again.status_code, 409, again.content)
        self.assertEqual(again.json().get("code"), "PAYMENT_ALREADY_COMPLETED")
        self.assertEqual(Payment.objects.filter(booking=self.booking).count(), payments_before)

    def test_a_fully_paid_room_with_no_extra_charges_owes_nothing(self):
        data = self._clearance()

        self.assertEqual(data["charges"], [])
        self.assertEqual(data["credits"], [])
        self.assertEqual(data["charges_total"], "0.00")
        self.assertEqual(data["folio_balance"], "0.00")
        self.assertEqual(data["outstanding_balance"], "0.00")
        self.assertEqual(data["amount_to_settle"], "0.00")
        self.assertTrue(data["cleared"])
        self.assertTrue(data["gate_pass_allowed"])
        self.assertEqual(data["gate_pass_blockers"], [])
        # The desk has nothing to collect, and it says so instead of guessing.
        self.assertFalse(data["can_settle"])
        self.assertIn("Nothing is outstanding", data["settle_blocked_reason"])
        # The room payment is still recorded once, as accommodation money.
        self.assertEqual(data["accommodation_settlements_total"], "420000.00")

    # ------------------ 2. gate clearance / blockers ------------------------

    def test_gate_clearance_is_denied_while_a_real_balance_remains(self):
        reference = self._receive_laundry()
        self._deliver_laundry(reference)

        data = self._clearance()
        self.assertFalse(data["gate_pass_allowed"])
        self.assertEqual([row["code"] for row in data["gate_pass_blockers"]], ["OUTSTANDING_BALANCE"])
        self.assertEqual(data["gate_pass_blockers"][0]["amount"], "3200.00")

        # An unbilled in-house request is a second, separate blocker.
        self._receive_laundry(key="folio-balance-laundry-2")
        data = self._clearance()
        self.assertIn("UNPAID_REQUESTS", [row["code"] for row in data["gate_pass_blockers"]])
        self.assertEqual(data["unpaid_requests_total"], "3200.00")
        self.assertEqual(data["amount_to_settle"], "6400.00")
        self.assertFalse(data["gate_pass_allowed"])

    # -------------- 3. genuine credits still reduce the charges -------------

    def test_a_genuine_credit_reduces_only_what_it_was_given_for(self):
        """A folio discount is preserved; accommodation money never hides a charge."""
        reference = self._receive_laundry()
        self._deliver_laundry(reference)
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.DISCOUNT,
            source_key="folio-balance-test:discount", actor=self.manager,
            business_date=self.today, narrative="Goodwill discount on the laundry bill",
            lines=[
                {"account_code": accounting.DISCOUNTS_ALLOWED, "direction": "DEBIT", "amount": "1000.00"},
                {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": "CREDIT", "amount": "1000.00",
                 "folio": self.folio},
            ],
            postings=[{
                "line_index": 1, "folio": self.folio, "kind": "DISCOUNT", "effect": "CREDIT",
                "amount": "1000.00", "description": "Goodwill discount",
            }],
        )

        data = self._clearance()
        self.assertEqual(data["credits_total"], "1000.00")
        self.assertEqual(data["charges_total"], "3200.00")
        self.assertEqual(data["folio_balance"], "2200.00", "a genuine credit must still count")
        self.assertEqual(data["outstanding_balance"], "2200.00")
        self.assertEqual(data["amount_to_settle"], "2200.00")
        self.assertFalse(data["cleared"])

        # A refund of the ROOM money is accommodation money: it must not be
        # turned into a folio charge, and it must not clear the laundry either.
        refund = Refund.objects.create(
            booking=self.booking, payment=self.room_payment, amount=Decimal("5000.00"),
            currency="NGN", paystack_transaction_reference="PI-FOLIO-BALANCE-1",
            status=Refund.Status.PROCESSED, processed_at=timezone.now(),
        )
        ensure_processed_refund(refund=refund, actor=self.manager)
        refund_posting = FolioPosting.objects.filter(kind=FolioPosting.Kind.REFUND).get()
        self.assertEqual(refund_posting.settles, FolioPosting.Settles.ACCOMMODATION)
        self.assertEqual(refund_posting.amount, Decimal("5000.00"))
        self.assertEqual(refund_posting.effect, FolioPosting.Effect.DEBIT)
        data = self._clearance()
        self.assertEqual(data["folio_balance"], "2200.00")
        self.assertEqual(data["outstanding_balance"], "2200.00")
        self.assertFalse(data["cleared"])

    # ------------- 4. every sales channel still counts at check-out ---------

    def test_bar_restaurant_room_service_and_laundry_charges_all_count(self):
        order = self._room_service_order(quantity=4)   # 4 × ₦500 = ₦2,000
        reference = self._receive_laundry()
        self._deliver_laundry(reference)              # 2 × ₦1,600 = ₦3,200

        data = self._clearance()

        self.assertEqual(data["charges_total"], "5200.00")
        self.assertEqual(len(data["charges"]), 2, data["charges"])
        sources = sorted(row["source_reference"] for row in data["charges"])
        self.assertEqual(sources, sorted([order.reference, reference]))
        for row in data["charges"]:
            self.assertEqual(row["kind"], "CHARGE")
        self.assertEqual(
            FolioPosting.objects.filter(folio=self.folio, effect=FolioPosting.Effect.DEBIT).count(), 2
        )
        self.assertEqual(data["outstanding_balance"], "5200.00")
        self.assertEqual(data["amount_to_settle"], "5200.00")
        self.assertFalse(data["gate_pass_allowed"])
        # The room-service sale really depleted stock, and the folio charge is
        # the restaurant money — not a stock movement, not a second charge.
        self.assertEqual(
            sum(
                Decimal(row.quantity_on_hand)
                for row in StockItem.objects.get(sku="BAL-WATER").balances.all()
            ),
            Decimal("20.0000"),
        )

    # --------------- 5. the read surfaces never disagree --------------------

    def test_every_surface_reports_the_same_figure(self):
        reference = self._receive_laundry()
        self._deliver_laundry(reference)

        clearance = self._clearance()
        self.auth(self.manager)

        listing = self.client.get(
            "/api/admin/bookings/", {"search": self.booking.booking_reference, "page_size": 100}
        )
        self.assertEqual(listing.status_code, 200, listing.content)
        row = next(
            entry for entry in listing.json()["data"]
            if entry["booking_reference"] == self.booking.booking_reference
        )
        self.assertEqual(row["folio_charges"], clearance["folio_balance"])
        self.assertEqual(row["outstanding_balance"], clearance["outstanding_balance"])
        self.assertEqual(row["amount_due"], "0.00")

        detail = self.client.get(f"/api/admin/bookings/{self.booking.booking_reference}/")
        self.assertEqual(detail.status_code, 200, detail.content)
        detail_data = detail.json()["data"]
        self.assertEqual(detail_data["folio_charges"], clearance["folio_balance"])
        self.assertEqual(detail_data["outstanding_balance"], clearance["outstanding_balance"])

        # The raw folio ledger is untouched by the classification: it still holds
        # the room payment credit and the laundry charge, exactly as posted.
        raw = folio_balance_queryset(Folio.objects.filter(pk=self.folio.pk)).values_list(
            "balance", flat=True
        ).get()
        self.assertEqual(raw, Decimal("3200.00") - Decimal("420000.00"))

    # ------------- 6. nothing existing is edited, deleted or moved ----------

    def test_existing_financial_records_are_preserved(self):
        reference = self._receive_laundry()
        self._deliver_laundry(reference)

        def snapshot():
            return {
                "transactions": {
                    row.pk: (row.type, row.status, row.reference)
                    for row in FinancialTransaction.objects.all()
                },
                "lines": {
                    row.pk: (row.transaction_id, row.account_code, row.direction, str(row.amount))
                    for row in FinancialLine.objects.all()
                },
                "postings": {
                    row.pk: (row.folio_id, row.kind, row.effect, row.settles, str(row.amount))
                    for row in FolioPosting.objects.all()
                },
                "payments": {
                    row.pk: (row.reference, row.status, str(row.amount))
                    for row in Payment.objects.all()
                },
                "allocations": {
                    row.pk: (row.payment_id, row.folio_id, str(row.amount))
                    for row in PaymentAllocation.objects.all()
                },
                "booking": (
                    self.booking.booking_reference, str(self.booking.total_amount),
                    str(self.booking.amount_paid),
                ),
            }

        before = snapshot()
        self._clearance()            # a read must change nothing at all
        self._settle("3200.00")      # and a settlement may only ADD records
        after = snapshot()

        # Every pre-existing row is still there, unchanged, field for field.
        for key in ("transactions", "lines", "postings", "payments", "allocations"):
            for pk, value in before[key].items():
                self.assertIn(pk, after[key], f"{key} row {pk} disappeared")
                self.assertEqual(after[key][pk], value, f"{key} row {pk} was edited")
        self.booking.refresh_from_db()
        self.assertEqual(
            (self.booking.booking_reference, str(self.booking.total_amount),
             str(self.booking.amount_paid)),
            before["booking"],
        )
        # The settlement added evidence; it never rewrote history.
        self.assertGreater(len(after["postings"]), len(before["postings"]))

    # ------- 7. the deployment pass classifies what already exists ----------

    def test_legacy_postings_are_classified_by_the_deployment_pass(self):
        """Pre-release rows carry no classification; the deployment pass labels them."""
        migration = _load_migration()

        # Rewind the posting that existed before the release, exactly as a
        # pre-0013 database would hold it.
        FolioPosting.objects.update(settles="")
        self.assertEqual(booking_folio_charge_balance(booking=self.booking), Decimal("-420000.00"))

        reference = self._receive_laundry()
        self._deliver_laundry(reference)
        amounts_before = dict(FolioPosting.objects.values_list("pk", "amount"))

        migration.classify_folio_money(global_apps, None)

        posting = FolioPosting.objects.get(kind=FolioPosting.Kind.PAYMENT)
        self.assertEqual(posting.settles, FolioPosting.Settles.ACCOMMODATION)
        self.assertEqual(booking_folio_charge_balance(booking=self.booking), Decimal("3200.00"))
        self.assertEqual(booking_outstanding_balance(booking=self.booking), Decimal("3200.00"))
        # Amounts are never touched by the classification.
        self.assertEqual(dict(FolioPosting.objects.values_list("pk", "amount")), amounts_before)

        # Running it twice changes nothing, and the reverse clears only itself.
        migration.classify_folio_money(global_apps, None)
        self.assertEqual(
            FolioPosting.objects.filter(kind=FolioPosting.Kind.PAYMENT, settles="").count(), 0
        )
        migration.unclassify_folio_money(global_apps, None)
        self.assertEqual(
            FolioPosting.objects.filter(kind=FolioPosting.Kind.PAYMENT, settles="").count(), 1
        )

    def test_the_deployment_pass_labels_a_desk_settlement_and_its_refund(self):
        """Both halves of the money classification are proved by the live data."""
        migration = _load_migration()
        reference = self._receive_laundry()
        self._deliver_laundry(reference)
        settled = self._settle("3200.00")
        self.assertEqual(settled.status_code, 201, settled.content)

        desk_payment = Payment.objects.get(reference=settled.json()["data"]["reference"])
        refund = Refund.objects.create(
            booking=self.booking, payment=desk_payment, amount=Decimal("3200.00"),
            currency="NGN", paystack_transaction_reference="PI-FOLIO-BALANCE-DESK",
            status=Refund.Status.PROCESSED, processed_at=timezone.now(),
        )
        ensure_processed_refund(refund=refund, actor=self.manager)
        # Live behaviour: money handed over for the folio is FOLIO_CHARGES, and so
        # is the refund that re-opens that same bill.
        desk_posting = FolioPosting.objects.get(
            kind=FolioPosting.Kind.PAYMENT, amount=Decimal("3200.00")
        )
        self.assertEqual(desk_posting.settles, FolioPosting.Settles.FOLIO_CHARGES)
        self.assertEqual(
            FolioPosting.objects.get(kind=FolioPosting.Kind.REFUND).settles,
            FolioPosting.Settles.FOLIO_CHARGES,
        )
        balance_with_labels = booking_folio_charge_balance(booking=self.booking)
        self.assertEqual(balance_with_labels, Decimal("3200.00"))
        self.assertFalse(self._clearance()["cleared"])

        # The same rows with no classification on them are a pre-release
        # database — the reported defect state, where the room credit nets off
        # the guest's charges and the balance goes negative.
        FolioPosting.objects.update(settles="")
        self.assertEqual(booking_folio_charge_balance(booking=self.booking), Decimal("-416800.00"))
        migration.classify_folio_money(global_apps, None)
        self.assertEqual(
            FolioPosting.objects.get(
                kind=FolioPosting.Kind.PAYMENT, amount=Decimal("3200.00")
            ).settles,
            FolioPosting.Settles.FOLIO_CHARGES,
        )
        self.assertEqual(
            FolioPosting.objects.get(kind=FolioPosting.Kind.REFUND).settles,
            FolioPosting.Settles.FOLIO_CHARGES,
        )
        # The room payment keeps ITS classification: the desk settlement must
        # never be able to re-label money that paid for the stay.
        self.assertEqual(
            FolioPosting.objects.get(
                kind=FolioPosting.Kind.PAYMENT, amount=Decimal("420000.00")
            ).settles,
            FolioPosting.Settles.ACCOMMODATION,
        )
        self.assertEqual(booking_folio_charge_balance(booking=self.booking), balance_with_labels)

    def test_the_receivable_check_command_reports_without_touching_anything(self):
        """A read-only money view of the same booking, for the record."""
        reference = self._receive_laundry()
        self._deliver_laundry(reference)
        before = (
            FinancialTransaction.objects.count(),
            FinancialLine.objects.count(),
            FolioPosting.objects.count(),
            Payment.objects.count(),
        )

        out = StringIO()
        call_command("guest_receivable_check", stdout=out)

        self.assertEqual(
            (FinancialTransaction.objects.count(), FinancialLine.objects.count(),
             FolioPosting.objects.count(), Payment.objects.count()),
            before,
        )
