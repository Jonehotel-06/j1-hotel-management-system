# tests/test_booking_balances.py
"""The booking balance the desk acts on (audit D4).

The audit found a folio holding ₦25,500 of posted restaurant charges while the
same booking reported ``amount_due`` ₦124,125 — two projections of one debt, and
staff had no single figure. ``amount_due`` stays the stay-only value for
backward compatibility; ``outstanding_balance`` is stay + posted folio charges
and must be identical row-for-row across the guest and staff surfaces, batched
so a page of bookings never turns into a query per row.
"""
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.utils import timezone
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import FinancialLine, FinancialTransaction
from apps.finance.services import accounting
from apps.finance.services.folio_service import get_or_create_main_folio_for_stay
from apps.finance.services.ledger_service import create_posted_transaction
from apps.stays.models import Stay

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room_type, make_staff, make_user


class BookingBalanceTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("balances-manager@staff.dev", role=User.Role.MANAGER)
        self.guest_user = make_user("balances-guest@example.com", role=User.Role.GUEST)
        self.guest = make_guest("balances-guest@example.com", user=self.guest_user)
        self.room_type = make_room_type("Balance suite", price="15000.00")
        self.booking = make_booking(
            self.guest, self.room_type,
            check_in=hotel_today(), check_out=hotel_today() + timedelta(days=2),
            total="15000.00", amount_paid="0.00",
        )
        self.stay = Stay.objects.create(
            reference=f"STY-{self.booking.booking_reference}", booking=self.booking,
            guest=self.booking.guest, status=Stay.Status.IN_HOUSE,
            expected_arrival=self.booking.check_in, expected_departure=self.booking.check_out,
        )
        self.folio, _ = get_or_create_main_folio_for_stay(stay=self.stay, actor=self.manager)

    def _post_charge(self, amount, transaction_type=FinancialTransaction.Type.OTHER_CHARGE,
                     source_key="balances:charge:1"):
        return create_posted_transaction(
            transaction_type=transaction_type,
            source_key=source_key,
            actor=self.manager,
            source_reference=self.booking.booking_reference,
            lines=[
                {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": FinancialLine.Direction.DEBIT,
                 "amount": amount, "folio": self.folio},
                {"account_code": accounting.OTHER_OPERATING_REVENUE,
                 "direction": FinancialLine.Direction.CREDIT, "amount": amount},
            ],
            postings=[{"line_index": 0, "folio": self.folio, "kind": "CHARGE",
                       "effect": "DEBIT", "amount": amount}],
        )

    def _admin_row(self, booking):
        self.auth(self.manager)
        response = self.client.get("/api/admin/bookings/")
        self.assertEqual(response.status_code, 200, response.content)
        for row in response.json()["data"]:
            if row["id"] == booking.pk:
                return row
        self.fail("booking missing from the admin list")

    def test_admin_list_row_carries_the_folio_inclusive_balance(self):
        # ₦15,000 stay + ₦2,500 of posted restaurant charges.
        self._post_charge("2500.00")
        row = self._admin_row(self.booking)

        self.assertEqual(row["amount_due"], "15000.00", "stay-only balance changed")
        self.assertEqual(row["folio_charges"], "2500.00")
        self.assertEqual(row["outstanding_balance"], "17500.00")

    def test_admin_detail_matches_the_list_row(self):
        self._post_charge("2500.00")
        row = self._admin_row(self.booking)

        self.auth(self.manager)
        detail = self.client.get(f"/api/admin/bookings/{self.booking.booking_reference}/")
        self.assertEqual(detail.status_code, 200, detail.content)
        payload = detail.json()["data"]
        self.assertEqual(payload["folio_charges"], row["folio_charges"])
        self.assertEqual(payload["outstanding_balance"], row["outstanding_balance"])
        self.assertEqual(payload["amount_due"], "15000.00")

    def test_guest_detail_reports_the_same_balance(self):
        self._post_charge("2500.00")

        self.auth(self.guest_user)
        detail = self.client.get(f"/api/bookings/{self.booking.booking_reference}/")
        self.assertEqual(detail.status_code, 200, detail.content)
        payload = detail.json()["data"]
        self.assertEqual(payload["folio_charges"], "2500.00")
        self.assertEqual(payload["outstanding_balance"], "17500.00")

    def test_nightly_accommodation_postings_are_not_double_counted(self):
        # The room price is already inside total_amount; re-adding the nightly
        # ACCOMMODATION_CHARGE split would bill the guest twice for the room.
        self._post_charge("15000.00", transaction_type=FinancialTransaction.Type.ACCOMMODATION_CHARGE,
                          source_key="accommodation:STY:night1")

        self.auth(self.manager)
        detail = self.client.get(f"/api/admin/bookings/{self.booking.booking_reference}/")
        payload = detail.json()["data"]
        self.assertEqual(payload["folio_charges"], "0.00")
        self.assertEqual(payload["outstanding_balance"], "15000.00")

    def test_a_settled_stay_still_owes_its_posted_charges(self):
        # An overpaid room is not a debt, so amount_due floors at zero — but the
        # ₦2,500 of restaurant charges really is still owed.
        self._post_charge("2500.00")
        booking = self.booking
        booking.amount_paid = Decimal("20000.00")
        booking.save(update_fields=["amount_paid", "updated_at"])

        row = self._admin_row(booking)
        self.assertEqual(row["amount_due"], "0.00")
        self.assertEqual(row["folio_charges"], "2500.00")
        self.assertEqual(row["outstanding_balance"], "2500.00")

    def test_admin_list_balance_lookup_does_not_scale_with_page_size(self):
        self._post_charge("2500.00")
        # One warm-up request so first-call caches (hotel settings, content) do
        # not masquerade as a per-row query.
        self.auth(self.manager)
        self.client.get("/api/admin/bookings/")
        one = self._count_admin_list_queries()

        for index in range(6):
            booking = make_booking(
                make_guest(f"balances-extra{index}@example.com"), self.room_type,
                check_in=hotel_today(), check_out=hotel_today() + timedelta(days=2),
                total="15000.00",
            )
            stay = Stay.objects.create(
                reference=f"STY-{booking.booking_reference}", booking=booking, guest=booking.guest,
                status=Stay.Status.IN_HOUSE, expected_arrival=booking.check_in,
                expected_departure=booking.check_out,
            )
            folio, _ = get_or_create_main_folio_for_stay(stay=stay, actor=self.manager)
            create_posted_transaction(
                transaction_type=FinancialTransaction.Type.OTHER_CHARGE,
                source_key=f"balances:charge:extra{index}",
                actor=self.manager,
                source_reference=booking.booking_reference,
                lines=[
                    {"account_code": accounting.ACCOUNTS_RECEIVABLE,
                     "direction": FinancialLine.Direction.DEBIT, "amount": "500.00", "folio": folio},
                    {"account_code": accounting.OTHER_OPERATING_REVENUE,
                     "direction": FinancialLine.Direction.CREDIT, "amount": "500.00"},
                ],
                postings=[{"line_index": 0, "folio": folio, "kind": "CHARGE",
                           "effect": "DEBIT", "amount": "500.00"}],
            )

        many = self._count_admin_list_queries()
        self.assertEqual(one, many, "folio balances are resolved per row instead of per page")


    # -- the receipt and the checkout moment (D4: "receipt and checkout totals") --
    def test_receipt_shows_the_same_folio_inclusive_balance(self):
        self._post_charge("2500.00")

        self.auth(self.manager)
        response = self.client.get(f"/api/admin/bookings/{self.booking.booking_reference}/receipt/")
        self.assertEqual(response.status_code, 200, response.content)
        receipt = response.json()["data"]
        self.assertEqual(receipt["amount_due"], "15000.00")
        self.assertEqual(receipt["folio_charges"], "2500.00")
        self.assertEqual(receipt["outstanding_balance"], "17500.00")

    def test_checkout_totals_include_posted_folio_charges(self):
        self._post_charge("2500.00")
        # The guest settles the room; the restaurant charges remain outstanding.
        self.booking.amount_paid = Decimal("15000.00")
        self.booking.payment_status = "PAID"
        self.booking.status = "CHECKED_IN"
        self.booking.checked_in_at = self.booking.checked_in_at or timezone.now()
        self.booking.save(update_fields=[
            "amount_paid", "payment_status", "status", "checked_in_at", "updated_at",
        ])

        self.auth(self.manager)
        response = self.client.post(
            f"/api/admin/bookings/{self.booking.booking_reference}/check-out/",
            {"allow_balance_due": True}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()["data"]
        self.assertEqual(payload["status"], "CHECKED_OUT")
        self.assertEqual(payload["amount_due"], "0.00")
        self.assertEqual(payload["folio_charges"], "2500.00")
        self.assertEqual(payload["outstanding_balance"], "2500.00")
        self.assertEqual(payload["currency"], self.booking.currency)

    def _count_admin_list_queries(self):
        self.auth(self.manager)
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get("/api/admin/bookings/")
        self.assertEqual(response.status_code, 200, response.content)
        return len(captured)
