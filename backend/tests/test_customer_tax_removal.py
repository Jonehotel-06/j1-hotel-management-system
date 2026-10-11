# tests/test_customer_tax_removal.py
"""MANDATORY customer-billing tax removal — acceptance tests.

Owner policy (2026-10-11): new customer-facing transactions at J-ONE HOTEL &
LODGE are charged from approved selling prices plus permitted charges less
permitted discounts — with NO appended customer-billed tax.  The approved
price of ₦10,000 is charged as ₦10,000.

These tests pin the whole money path: quote → booking record → Paystack
initialization → verified payment → receipt → folio → checkout balance →
ledger.  They also pin the two preservation rules:

* Historical transactions (which DID carry tax) keep their original
  breakdowns, receipts and ledger postings.
* The hotel's own statutory exposure is kept in a SEPARATE ledger pair that
  never touches a guest balance, and reports distinguish historical tax
  collected from current statutory accruals.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.bookings.serializers import ReceiptSerializer
from apps.bookings.services.pricing import calculate_quote
from apps.core.utils import hotel_today
from apps.finance.models import FinancialLine, FinancialTransaction
from apps.finance.services import accounting
from apps.finance.services.accommodation_service import post_accommodation_charge_for_night
from apps.finance.services.reporting_service import financial_summary
from apps.finance.services.tax_policy import customer_tax_amount, statutory_policy
from apps.payments.models import Payment
from apps.pos.models import PosOrder, PosTender
from apps.pos.services.order_service import (
    capture_direct_tender, create_order, submit_order, transition_order,
)
from apps.stays.models import Stay

from .base import BaseAPITestCase
from .factories import (
    hotel_settings, make_booking, make_guest, make_room, make_room_type,
    make_staff, make_user,
)


def _line_total(account_code, direction):
    return sum(
        (line.amount for line in FinancialLine.objects.filter(
            account_code=account_code, direction=direction,
            transaction__status=FinancialTransaction.Status.POSTED,
        )),
        Decimal("0.00"),
    )


class QuoteTaxRemovalTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.room_type = make_room_type("Deluxe", price="75000.00", max_guests=2)
        self.ci = hotel_today() + timedelta(days=5)
        self.co = self.ci + timedelta(days=1)

    def test_legacy_rate_cannot_add_tax_to_a_new_quote(self):
        hotel_settings(tax_rate_percent=Decimal("7.50"), service_fee=Decimal("0.00"))
        quote = calculate_quote(
            room_type=self.room_type, check_in=self.ci, check_out=self.co,
            rooms=1, adults=2, children=0,
        )
        self.assertEqual(customer_tax_amount(), Decimal("0.00"))
        self.assertEqual(quote.tax, Decimal("0.00"))
        self.assertEqual(quote.total, Decimal("75000.00"))  # exactly the approved price

    def test_discount_and_service_fee_still_apply_without_tax(self):
        hotel_settings(tax_rate_percent=Decimal("7.50"), service_fee=Decimal("2000.00"))
        quote = calculate_quote(
            room_type=self.room_type, check_in=self.ci, check_out=self.co,
            rooms=1, adults=2, children=0,
        )
        # 75,000 + 2,000 fee; no +7.5% on top.
        self.assertEqual(quote.total, Decimal("77000.00"))
        self.assertEqual(quote.to_api_dict()["tax"], "0.00")


class BookingMoneyPathTests(BaseAPITestCase):
    """Quote → booking → Paystack → receipt all agree on a tax-free total."""

    def setUp(self):
        super().setUp()
        hotel_settings(tax_rate_percent=Decimal("7.50"), service_fee=Decimal("0.00"))
        self.room_type = make_room_type("Deluxe", price="25000.00")
        make_room(self.room_type, "301")
        self.user = make_user("taxpayer@guest.dev", password="Str0ng!Pass")
        self.auth(self.user)
        self.today = hotel_today()
        response = self.client.post("/api/bookings/", {
            "room_type": self.room_type.slug,
            "check_in": (self.today + timedelta(days=5)).isoformat(),
            "check_out": (self.today + timedelta(days=7)).isoformat(),
            "rooms": 1, "adults": 2, "children": 0,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.json())
        self.booking_ref = response.json()["data"]["booking_reference"]
        self.booking = Booking.objects.get(booking_reference=self.booking_ref)

    def test_booking_snapshot_is_tax_free(self):
        self.assertEqual(self.booking.tax_amount, Decimal("0.00"))
        self.assertEqual(self.booking.total_amount, Decimal("50000.00"))
        self.assertEqual(self.booking.required_payment, Decimal("50000.00"))
        self.assertEqual(
            self.booking.total_amount,
            self.booking.subtotal - self.booking.discount_amount
            + self.booking.extra_guest_fee_amount + self.booking.fee_amount,
        )

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.initialize_transaction")
    def test_paystack_initialization_amount_matches_the_tax_free_total(self, mock_init):
        mock_init.side_effect = lambda **kw: {
            "authorization_url": "https://checkout.paystack.com/abc",
            "access_code": "abc", "reference": kw["reference"],
        }
        response = self.client.post("/api/payments/initialize/",
                                    {"booking_reference": self.booking_ref})
        self.assertEqual(response.status_code, 201, response.json())
        data = response.json()["data"]
        self.assertEqual(data["amount"], "50000.00")
        payment = Payment.objects.get()
        self.assertEqual(payment.amount, Decimal("50000.00"))
        _, kwargs = mock_init.call_args
        self.assertEqual(kwargs["amount_kobo"], 5_000_000)  # ₦50,000 — never +tax

    def test_receipt_reconciles_with_the_booking(self):
        receipt = ReceiptSerializer().to_representation(self.booking)
        self.assertEqual(receipt["tax"], "0.00")
        self.assertEqual(receipt["total"], "50000.00")
        self.assertEqual(receipt["amount_due"], "50000.00")

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_mock")
    @patch("apps.payments.services.paystack.verify_transaction")
    def test_verified_payment_frees_no_discrepancy(self, mock_verify):
        payment = Payment.objects.create(
            booking=self.booking, user=self.user, reference="J1P-TAXFREE-1",
            provider=Payment.Provider.PAYSTACK, amount=Decimal("50000.00"),
            currency="NGN", status=Payment.Status.PENDING,
        )
        mock_verify.return_value = {
            "status": True, "data": {
                "status": "success", "reference": payment.reference,
                "amount": 5_000_000, "currency": "NGN",
                "paid_at": "2026-10-11T10:00:00.000Z",
                "customer": {"email": "taxpayer@guest.dev"},
            },
        }
        response = self.client.get(f"/api/payments/verify/{payment.reference}/")
        self.assertEqual(response.status_code, 200, response.json())
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.amount_paid, Decimal("50000.00"))
        self.assertEqual(self.booking.payment_status, Booking.PaymentStatus.PAID)


class AccommodationLedgerTaxTests(BaseAPITestCase):
    """Checkout accounting: tax-free nights post revenue + statutory accrual."""

    def setUp(self):
        super().setUp()
        self.actor = make_staff("tax.recon@staff.dev", role=User.Role.MANAGER)
        self.room_type = make_room_type("Ledger suite", price="20000.00")
        self.check_out = hotel_today() - timedelta(days=1)
        self.check_in = self.check_out - timedelta(days=1)  # one elapsed night

    def _post_night(self, *, booking):
        stay = Stay.objects.create(
            reference=f"STY-{booking.booking_reference}", booking=booking,
            guest=booking.guest, status=Stay.Status.CHECKED_OUT,
            expected_arrival=booking.check_in, expected_departure=booking.check_out,
        )
        return post_accommodation_charge_for_night(
            stay=stay, service_date=self.check_in, actor=self.actor,
        )

    def test_new_night_posts_revenue_without_tax_plus_statutory_accrual(self):
        hotel_settings(statutory_tax_enabled=True,
                       statutory_tax_rate_percent=Decimal("7.50"),
                       statutory_tax_basis="EXCLUSIVE")
        booking = make_booking(
            make_guest("new-night@example.com"), self.room_type, [],
            check_in=self.check_in, check_out=self.check_out, total="20000.00",
        )
        self._post_night(booking=booking)
        # Guest side: one balanced charge of exactly the approved price.
        self.assertEqual(
            _line_total(accounting.ACCOUNTS_RECEIVABLE, FinancialLine.Direction.DEBIT),
            Decimal("20000.00"),
        )
        self.assertEqual(
            _line_total(accounting.ACCOMMODATION_REVENUE, FinancialLine.Direction.CREDIT),
            Decimal("20000.00"),
        )
        # Historical billed-tax account untouched for new transactions.
        self.assertEqual(
            _line_total(accounting.TAX_PAYABLE, FinancialLine.Direction.CREDIT),
            Decimal("0.00"),
        )
        # Statutory exposure accrued separately: 7.5% of 20,000 = 1,500 —
        # visible to the accountant, invisible to the guest.
        self.assertEqual(
            _line_total(accounting.STATUTORY_TAX_PAYABLE, FinancialLine.Direction.CREDIT),
            Decimal("1500.00"),
        )
        self.assertEqual(
            _line_total(accounting.STATUTORY_TAX_ABSORBED, FinancialLine.Direction.DEBIT),
            Decimal("1500.00"),
        )

    def test_statutory_accrual_can_be_disabled_without_touching_history(self):
        hotel_settings(statutory_tax_enabled=False)
        booking = make_booking(
            make_guest("no-stat@example.com"), self.room_type, [],
            check_in=self.check_in, check_out=self.check_out, total="20000.00",
        )
        self._post_night(booking=booking)
        self.assertEqual(
            _line_total(accounting.STATUTORY_TAX_PAYABLE, FinancialLine.Direction.CREDIT),
            Decimal("0.00"),
        )
        self.assertEqual(
            _line_total(accounting.ACCOMMODATION_REVENUE, FinancialLine.Direction.CREDIT),
            Decimal("20000.00"),
        )

    def test_historical_taxed_night_keeps_its_original_breakdown(self):
        hotel_settings(statutory_tax_enabled=True,
                       statutory_tax_rate_percent=Decimal("7.50"))
        booking = make_booking(
            make_guest("historic@example.com"), self.room_type, [],
            check_in=self.check_in, check_out=self.check_out, total="21500.00",
        )
        # A pre-removal booking snapshot: ₦20,000 net + ₦1,500 billed tax.
        booking.tax_amount = Decimal("1500.00")
        booking.save(update_fields=["tax_amount", "updated_at"])
        self._post_night(booking=booking)
        self.assertEqual(
            _line_total(accounting.TAX_PAYABLE, FinancialLine.Direction.CREDIT),
            Decimal("1500.00"), "historical billed tax must still be recognised",
        )
        self.assertEqual(
            _line_total(accounting.ACCOMMODATION_REVENUE, FinancialLine.Direction.CREDIT),
            Decimal("20000.00"),
        )
        # No second, duplicative statutory accrual on top of historical tax.
        self.assertEqual(
            _line_total(accounting.STATUTORY_TAX_PAYABLE, FinancialLine.Direction.CREDIT),
            Decimal("0.00"),
        )

    def test_reports_distinguish_historical_tax_and_statutory_tax(self):
        hotel_settings(statutory_tax_enabled=True,
                       statutory_tax_rate_percent=Decimal("7.50"))
        old_booking = make_booking(
            make_guest("old-books@example.com"), self.room_type, [],
            check_in=self.check_in, check_out=self.check_out, total="21500.00",
        )
        old_booking.tax_amount = Decimal("1500.00")
        old_booking.save(update_fields=["tax_amount", "updated_at"])
        self._post_night(booking=old_booking)
        new_booking = make_booking(
            make_guest("new-books@example.com"), self.room_type, [],
            check_in=self.check_in, check_out=self.check_out, total="20000.00",
        )
        self._post_night(booking=new_booking)
        summary = financial_summary(
            start_date=self.check_in - timedelta(days=1),
            end_date=self.check_out + timedelta(days=1),
        )
        self.assertEqual(summary["tax_accrued"]["amount"], "1500.00")           # history
        self.assertEqual(summary["statutory_tax_accrued"]["amount"], "1500.00")  # new, separate
        self.assertEqual(summary["guest_charges"]["amount"], "41500.00")
        self.assertEqual(summary["recognized_revenue"]["amount"], "40000.00")


class PosTaxRemovalTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        from apps.pos.models import MenuCategory, MenuItem, MenuModifier

        self.actor = make_staff("tax.cashier@staff.dev", role=User.Role.CASHIER)
        self.category = MenuCategory.objects.create(
            name="Grills", slug="grills",
            service_area=MenuCategory.ServiceArea.RESTAURANT,
        )
        # Legacy per-item tax configured — must not reach the guest's bill.
        self.item = MenuItem.objects.create(
            category=self.category, name="Suya platter", sku="SUYA-01",
            base_price=Decimal("2000.00"), tax_rate_percent=Decimal("7.50"),
        )
        self.modifier = MenuModifier.objects.create(
            menu_item=self.item, name="Extra pepper", price_delta=Decimal("200.00"),
        )

    def _deliver(self, order):
        submit_order(order=order, actor=self.actor)
        transition_order(order=order, target_status=PosOrder.Status.PREPARING, actor=self.actor)
        transition_order(order=order, target_status=PosOrder.Status.READY, actor=self.actor)
        return transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.actor)

    def _create(self, **kwargs):
        payload = dict(
            mode=PosOrder.Mode.RESTAURANT,
            actor=self.actor,
            lines=[{
                "menu_item_id": self.item.pk,
                "quantity": 2,
                "modifier_ids": [self.modifier.pk],
            }],
        )
        payload.update(kwargs)
        return create_order(**payload)

    def test_pos_order_never_appends_tax(self):
        order, created = self._create()
        self.assertTrue(created)
        self.assertEqual(order.subtotal, Decimal("4400.00"))   # (2000+200)×2
        self.assertEqual(order.tax_amount, Decimal("0.00"))
        self.assertEqual(order.total_amount, Decimal("4400.00"))
        line = order.lines.get()
        self.assertEqual(line.line_total, Decimal("4400.00"))

    def test_pos_charge_ledger_is_tax_free_with_separate_statutory_accrual(self):
        hotel_settings(statutory_tax_enabled=True,
                       statutory_tax_rate_percent=Decimal("7.50"),
                       statutory_tax_basis="EXCLUSIVE")
        order, _ = self._create(mode=PosOrder.Mode.TAKEAWAY, guest_name="Walk-in")
        delivered = self._deliver(order)
        delivered.refresh_from_db()
        txn = delivered.charge_transaction
        lines = list(txn.lines.values_list("account_code", "direction", "amount"))
        self.assertIn(
            (accounting.FOOD_BEVERAGE_REVENUE, "CREDIT", Decimal("4400.00")), lines,
        )
        self.assertNotIn(
            accounting.TAX_PAYABLE,
            [code for code, _, _ in lines],
        )
        statutory = [amount for code, _, amount in lines
                     if code == accounting.STATUTORY_TAX_PAYABLE]
        self.assertEqual(statutory, [Decimal("330.00")])  # 7.5% of 4,400
        # The guest's charge is exactly the approved prices.
        tender = capture_direct_tender(
            order=delivered, method=PosTender.Method.CARD,
            amount=delivered.total_amount, actor=self.actor,
            external_reference="terminal-slip-tax",
        )
        self.assertEqual(delivered.total_amount, Decimal("4400.00"))
        self.assertIsNotNone(tender.pk)

    def test_historical_pos_order_with_tax_still_posts_collected_tax(self):
        hotel_settings(statutory_tax_enabled=True)
        order, _ = self._create(mode=PosOrder.Mode.TAKEAWAY, guest_name="History")
        # Simulate a pre-removal order snapshot.
        order.tax_amount = Decimal("330.00")
        order.total_amount = Decimal("4730.00")
        order.save(update_fields=["tax_amount", "total_amount", "updated_at"])
        delivered = self._deliver(order)
        txn = delivered.charge_transaction
        codes = list(txn.lines.values_list("account_code", flat=True))
        self.assertIn(accounting.TAX_PAYABLE, codes)
        self.assertNotIn(accounting.STATUTORY_TAX_PAYABLE, codes)
        tax_line = txn.lines.get(account_code=accounting.TAX_PAYABLE)
        self.assertEqual(tax_line.amount, Decimal("330.00"))


class SettingsContractTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_staff("tax.admin@staff.dev", role=User.Role.ADMIN)
        self.auth(self.admin)

    def test_legacy_customer_tax_rate_is_read_only(self):
        hotel_settings(tax_rate_percent=Decimal("0.00"))
        response = self.client.patch("/api/admin/settings/",
                                     {"tax_rate_percent": "7.50"}, format="json")
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["data"]["tax_rate_percent"], "0.00")

    def test_statutory_policy_fields_are_configurable(self):
        response = self.client.patch("/api/admin/settings/", {
            "statutory_tax_enabled": True,
            "statutory_tax_rate_percent": "7.50",
            "statutory_tax_basis": "EXCLUSIVE",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.json())
        data = response.json()["data"]
        self.assertEqual(data["statutory_tax_rate_percent"], "7.50")
        self.assertEqual(data["statutory_tax_basis"], "EXCLUSIVE")
        policy = statutory_policy()
        self.assertTrue(policy.enabled)
        self.assertEqual(policy.amount_for(net_amount=Decimal("10000.00")),
                         Decimal("750.00"))

    def test_inclusive_basis_extracts_rather_than_appends(self):
        hotel_settings(statutory_tax_enabled=True,
                       statutory_tax_rate_percent=Decimal("7.50"),
                       statutory_tax_basis="INCLUSIVE")
        policy = statutory_policy()
        # ₦10,750 gross with 7.5% contained → 750 extracted, guest still pays 10,750.
        self.assertEqual(policy.amount_for(net_amount=Decimal("10750.00"),
                                           gross_amount=Decimal("10750.00")),
                         Decimal("750.00"))
