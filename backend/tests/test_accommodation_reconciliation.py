# tests/test_accommodation_reconciliation.py
"""Missing nightly room revenue/tax is detectable and safely repairable (D4/N1).

The audit's October picture — ₦50,500 of F&B revenue, ₦0 tax, two checked-out
stays carrying ₦21,750 of tax — is what an unposted night looks like: the guest
owes the money, the booking says the money is due, and the ledger never learned
about it. These tests use synthetic figures and prove the repair is exact,
idempotent and attributable.
"""
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import FinancialTransaction
from apps.finance.services.accommodation_service import post_accommodation_charge_for_night
from apps.finance.services.accommodation_reconciliation import accommodation_night_gaps
from apps.finance.services.reporting_service import financial_summary
from apps.stays.models import Stay

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room_type, make_staff


class AccommodationReconciliationTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("recon.manager@staff.dev", role=User.Role.MANAGER)
        self.room_type = make_room_type("Recon suite", price="10000.00")
        # Three elapsed nights: 2026-xx-xx … yesterday, tax ₦750/night (7.5%).
        self.check_out = hotel_today() - timedelta(days=1)
        self.check_in = self.check_out - timedelta(days=3)
        self.booking = make_booking(
            make_guest("recon-guest@example.com"), self.room_type, [],
            check_in=self.check_in, check_out=self.check_out, total="32250.00",
        )
        self.booking.tax_amount = Decimal("2250.00")
        self.booking.save(update_fields=["tax_amount", "updated_at"])
        self.stay = Stay.objects.create(
            reference=f"STY-{self.booking.booking_reference}", booking=self.booking,
            guest=self.booking.guest, status=Stay.Status.CHECKED_OUT,
            expected_arrival=self.check_in, expected_departure=self.check_out,
        )

    # -- detection ---------------------------------------------------------
    def test_a_checked_out_stay_without_postings_is_reported(self):
        gaps = accommodation_night_gaps(through_date=hotel_today() - timedelta(days=1))
        self.assertEqual(len(gaps), 1, "an unposted stay must be reported")
        gap = gaps[0]
        self.assertEqual(gap.stay_reference, self.stay.reference)
        self.assertEqual(gap.nights_missing, 3)
        self.assertEqual(gap.tax_amount, Decimal("2250.00"))
        self.assertEqual(gap.total_amount, Decimal("32250.00"))

    def test_a_fully_posted_stay_is_not_reported(self):
        for offset in range(3):
            post_accommodation_charge_for_night(
                stay=self.stay, service_date=self.check_in + timedelta(days=offset), actor=self.manager
            )
        self.assertEqual(accommodation_night_gaps(), [], "posted nights must not be flagged")

    def test_only_the_missing_nights_are_reported(self):
        post_accommodation_charge_for_night(stay=self.stay, service_date=self.check_in, actor=self.manager)
        gap = accommodation_night_gaps()[0]
        self.assertEqual(gap.nights_missing, 2)
        self.assertEqual(gap.tax_amount, Decimal("1500.00"))

    def test_tonights_still_open_night_is_never_flagged(self):
        self.stay.status = Stay.Status.IN_HOUSE
        self.stay.save(update_fields=["status", "updated_at"])
        tomorrow = self.check_out + timedelta(days=1)
        self.booking.check_out = tomorrow
        self.booking.save(update_fields=["check_out", "updated_at"])

        gap = accommodation_night_gaps(through_date=hotel_today() - timedelta(days=1))[0]
        # Four nights have elapsed by yesterday's close; tonight has not, so it
        # must never be recognized early.
        self.assertEqual(gap.nights_missing, 4)
        self.assertNotIn(hotel_today(), gap.missing_nights)
        self.assertEqual(gap.missing_nights[-1], hotel_today() - timedelta(days=1))

    # -- repair ------------------------------------------------------------
    def test_check_mode_reports_without_touching_the_ledger(self):
        out = StringIO()
        call_command("reconcile_accommodation", stdout=out)
        self.assertIn("No ledger changes were made", out.getvalue())
        self.assertEqual(FinancialTransaction.objects.count(), 0)

    def test_post_repairs_the_ledger_and_a_second_run_is_a_no_op(self):
        out = StringIO()
        call_command("reconcile_accommodation", "--post", "--actor", self.manager.email, stdout=out)
        self.assertIn("Posted 3 missing night(s)", out.getvalue())

        transactions = FinancialTransaction.objects.filter(
            type=FinancialTransaction.Type.ACCOMMODATION_CHARGE
        )
        self.assertEqual(transactions.count(), 3)
        self.assertEqual(accommodation_night_gaps(), [])

        again = StringIO()
        call_command("reconcile_accommodation", "--post", "--actor", self.manager.email, stdout=again)
        self.assertIn("No missing nights", again.getvalue())
        self.assertEqual(transactions.count(), 3, "a repeat run must not double-post")

    def test_repair_is_attributable_and_requires_an_actor(self):
        with self.assertRaises(CommandError):
            call_command("reconcile_accommodation", "--post")

        call_command("reconcile_accommodation", "--post", "--actor", self.manager.email, stdout=StringIO())
        for transaction in FinancialTransaction.objects.filter(
            type=FinancialTransaction.Type.ACCOMMODATION_CHARGE
        ):
            self.assertEqual(transaction.initiated_by_id, self.manager.pk)

    def test_unknown_actor_is_refused(self):
        with self.assertRaises(CommandError):
            call_command("reconcile_accommodation", "--post", "--actor", "nosuch@staff.dev")

    def test_a_guest_account_cannot_be_used_as_actor(self):
        from .factories import make_user

        guest = make_user("recon.actor.guest@example.com", role=User.Role.GUEST)
        with self.assertRaises(CommandError):
            call_command("reconcile_accommodation", "--post", "--actor", guest.email)

    # -- what the finance report then says ---------------------------------
    def test_repair_restores_room_revenue_and_tax_to_the_summary(self):
        start, end = self.check_in, self.check_out
        before = financial_summary(start_date=start, end_date=end)
        self.assertEqual(before["recognized_revenue"]["amount"], "0.00")
        self.assertEqual(before["tax_accrued"]["amount"], "0.00")

        call_command("reconcile_accommodation", "--post", "--actor", self.manager.email, stdout=StringIO())

        after = financial_summary(start_date=start, end_date=end)
        self.assertEqual(after["recognized_revenue"]["amount"], "30000.00")
        self.assertEqual(after["tax_accrued"]["amount"], "2250.00")
        rooms = [row for row in after["by_department"] if row["label"] == "Rooms and hotel bookings"]
        self.assertEqual(len(rooms), 1)
        self.assertEqual(rooms[0]["amount"], "30000.00")

    def test_json_output_is_machine_readable_and_carries_no_guest_details(self):
        out = StringIO()
        call_command("reconcile_accommodation", "--json", stdout=out)
        payload = out.getvalue()
        self.assertIn(self.stay.reference, payload)
        self.assertNotIn("Recon guest", payload)
        self.assertNotIn("recon-guest@example.com", payload)
