"""Night audit, AR aging, manual journals, cash drops and expense departments."""
from datetime import timedelta
from decimal import Decimal

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.core.utils import hotel_today
from apps.finance.models import Expense, FinancialLine, FinancialTransaction
from apps.finance.services import accounting
from apps.finance.services.cash_session_service import open_cash_session
from apps.finance.services.ledger_service import create_posted_transaction

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type, make_staff


class NightAuditTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("night-audit-manager@staff.dev", role=User.Role.MANAGER)
        self.cashier = make_staff("night-audit-cashier@staff.dev", role=User.Role.CASHIER)
        self.today = hotel_today()
        self.room_type = make_room_type("Night audit suite", price="20000.00")
        self.room = make_room(self.room_type, "701")

    def _post_accommodation(self, amount="20000.00", key="night-audit:acc:1"):
        return create_posted_transaction(
            transaction_type=FinancialTransaction.Type.ACCOMMODATION_CHARGE,
            source_key=key,
            actor=self.manager,
            business_date=self.today,
            lines=[
                {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": FinancialLine.Direction.DEBIT, "amount": amount},
                {"account_code": accounting.ACCOMMODATION_REVENUE, "direction": FinancialLine.Direction.CREDIT, "amount": amount},
            ],
        )

    def test_daily_flash_computes_adr_and_revpar_from_ledger(self):
        self._post_accommodation()
        make_booking(
            make_guest("night-audit-guest@example.com"), self.room_type,
            rooms=[self.room], check_in=self.today, check_out=self.today + timedelta(days=1),
            total="20000.00",
        )
        self.auth(self.manager)
        response = self.client.get(f"/api/admin/finance/reports/night-audit/?date={self.today.isoformat()}")
        self.assertEqual(response.status_code, 200, response.content)
        flash = response.json()["data"]["daily_flash"]
        self.assertEqual(flash["accommodation_revenue"], "20000.00")
        self.assertEqual(flash["occupied_rooms"], 1)
        self.assertEqual(flash["available_rooms"], 1)
        self.assertEqual(flash["adr"], "20000.00")
        self.assertEqual(flash["revpar"], "20000.00")
        self.assertEqual(flash["occupancy_percent"], 100.0)

    def test_night_audit_lists_drawer_variance_and_blocks_cashiers(self):
        session = open_cash_session(cashier=self.cashier, actor=self.cashier, opening_float="500.00")
        session.counted_cash = Decimal("450.00")
        session.variance = Decimal("-50.00")
        session.save(update_fields=["counted_cash", "variance", "updated_at"])
        self.auth(self.manager)
        response = self.client.get("/api/admin/finance/reports/night-audit/")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["cash_drawers"][0]["reference"], session.reference)
        self.assertTrue(any(d["type"] == "CASH_VARIANCE" for d in data["discrepancies"]))
        self.auth(self.cashier)
        self.assertEqual(self.client.get("/api/admin/finance/reports/night-audit/").status_code, 403)

    def test_ar_aging_buckets_outstanding_balances_and_excludes_paid(self):
        old = make_booking(
            make_guest("aging-old@example.com"), self.room_type,
            check_in=self.today - timedelta(days=100), check_out=self.today - timedelta(days=97),
            total="9000.00", amount_paid="1000.00",
        )
        old.payment_status = Booking.PaymentStatus.PARTIALLY_PAID
        old.save(update_fields=["payment_status", "updated_at"])
        paid = make_booking(
            make_guest("aging-paid@example.com"), self.room_type,
            check_in=self.today - timedelta(days=100), check_out=self.today - timedelta(days=97),
            total="9000.00", amount_paid="9000.00",
        )
        paid.payment_status = Booking.PaymentStatus.PAID
        paid.save(update_fields=["payment_status", "updated_at"])
        self.auth(self.manager)
        response = self.client.get("/api/admin/finance/reports/ar-aging/")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        # Batch 01 task 7. Neither booking has a posted accommodation charge,
        # so NOTHING has been charged to a guest ledger yet and the guest
        # receivable is genuinely nil — the old report published the booking's
        # contract projection as if it were a posted receivable, which could
        # never agree with the balance sheet.
        self.assertEqual(data["total_outstanding"], "0.00")
        self.assertEqual(data["net_receivable"], "0.00")
        self.assertTrue(data["reconciliation"]["reconciles"])
        # The money is not lost: it is reported as contracted stay value the
        # ledger has not charged yet, and the partly paid booking's ₦8,000 is
        # separated from the fully paid one.
        self.assertEqual(data["stay_value_not_yet_charged"], "8000.00")
        self.assertEqual(data["bookings_not_yet_charged"], 1)
        self.assertEqual(data["total_stay_exposure"], "8000.00")
        self.assertEqual(paid.amount_due, Decimal("0.00"))


class ManualJournalTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("journal-manager@staff.dev", role=User.Role.MANAGER)
        self.cashier = make_staff("journal-cashier@staff.dev", role=User.Role.CASHIER)
        self.payload = {
            "narrative": "Accrue October cleaning contract.",
            "lines": [
                {"account_code": "EXPENSE.OPERATING", "direction": "DEBIT", "amount": "1500.00"},
                {"account_code": "LIABILITY.ACCOUNTS_PAYABLE", "direction": "CREDIT", "amount": "1500.00"},
            ],
            "idempotency_key": "journal-test-1",
        }

    def test_balanced_journal_posts_and_retry_is_idempotent(self):
        self.auth(self.manager)
        first = self.client.post("/api/admin/finance/journals/", self.payload, format="json")
        self.assertEqual(first.status_code, 201, first.content)
        reference = first.json()["data"]["reference"]
        self.assertEqual(first.json()["data"]["type"], "ADJUSTMENT")
        retry = self.client.post("/api/admin/finance/journals/", self.payload, format="json")
        self.assertEqual(retry.status_code, 200, retry.content)
        self.assertEqual(retry.json()["data"]["reference"], reference)
        self.assertEqual(
            FinancialTransaction.objects.filter(type=FinancialTransaction.Type.ADJUSTMENT).count(), 1
        )

    def test_unbalanced_or_unknown_account_journal_is_rejected(self):
        self.auth(self.manager)
        bad = dict(self.payload, idempotency_key="journal-test-2")
        bad["lines"] = [
            {"account_code": "EXPENSE.OPERATING", "direction": "DEBIT", "amount": "1500.00"},
            {"account_code": "LIABILITY.ACCOUNTS_PAYABLE", "direction": "CREDIT", "amount": "1400.00"},
        ]
        self.assertEqual(self.client.post("/api/admin/finance/journals/", bad, format="json").status_code, 400)
        unknown = dict(self.payload, idempotency_key="journal-test-3")
        unknown["lines"] = [
            {"account_code": "REVENUE.MADE_UP", "direction": "DEBIT", "amount": "10.00"},
            {"account_code": "ASSET.CASH_ON_HAND", "direction": "CREDIT", "amount": "10.00"},
        ]
        self.assertEqual(self.client.post("/api/admin/finance/journals/", unknown, format="json").status_code, 400)

    def test_cashier_cannot_post_journals_or_read_chart(self):
        self.auth(self.cashier)
        self.assertEqual(self.client.post("/api/admin/finance/journals/", self.payload, format="json").status_code, 403)
        self.assertEqual(self.client.get("/api/admin/finance/chart-of-accounts/").status_code, 403)
        self.auth(self.manager)
        chart = self.client.get("/api/admin/finance/chart-of-accounts/")
        self.assertEqual(chart.status_code, 200, chart.content)
        self.assertTrue(any(a["code"] == "REVENUE.ACCOMMODATION" for a in chart.json()["data"]["accounts"]))


class CashDropAndExpenseDepartmentTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("drop-manager@staff.dev", role=User.Role.MANAGER)
        self.cashier = make_staff("drop-cashier@staff.dev", role=User.Role.CASHIER)
        self.other_cashier = make_staff("drop-other@staff.dev", role=User.Role.CASHIER)

    def test_cashier_records_drop_and_expected_cash_falls(self):
        session = open_cash_session(cashier=self.cashier, actor=self.cashier, opening_float="1000.00")
        self.auth(self.cashier)
        response = self.client.post(
            f"/api/admin/finance/cash-sessions/{session.reference}/drop/",
            {"amount": "400.00", "notes": "Mid-shift drop."}, format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["data"]["expected_cash"], "600.00")
        too_much = self.client.post(
            f"/api/admin/finance/cash-sessions/{session.reference}/drop/",
            {"amount": "601.00"}, format="json",
        )
        self.assertEqual(too_much.status_code, 400)

    def test_other_cashier_cannot_drop_from_someone_elses_drawer(self):
        session = open_cash_session(cashier=self.cashier, actor=self.cashier, opening_float="1000.00")
        self.auth(self.other_cashier)
        response = self.client.post(
            f"/api/admin/finance/cash-sessions/{session.reference}/drop/",
            {"amount": "10.00"}, format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_expense_department_is_stored_and_returned(self):
        self.auth(self.manager)
        response = self.client.post("/api/admin/finance/expenses/", {
            "category": "Kitchen supplies",
            "amount": "2500.00",
            "payment_method": "BANK_TRANSFER",
            "department": "Kitchen",
            "payee": "Market vendor",
            "idempotency_key": "expense-dept-1",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["data"]["department"], "Kitchen")
        self.assertEqual(Expense.objects.get(reference=response.json()["data"]["reference"]).department, "Kitchen")


class NightAuditInHouseTests(BaseAPITestCase):
    """Regression: the audit pack must run against real in-house stays.

    The first deployment crashed here because the room-assignment prefetch
    used a related name that only exists on Room, not on Stay — a path the
    empty-database tests never exercised.
    """

    def setUp(self):
        super().setUp()
        from apps.finance.services.folio_service import get_or_create_main_folio_for_stay
        from apps.stays.models import Stay, StayRoom

        self.manager = make_staff("night-audit-stay-manager@staff.dev", role=User.Role.MANAGER)
        self.today = hotel_today()
        self.room_type = make_room_type("Night audit in-house suite", price="20000.00")
        self.room = make_room(self.room_type, "702")
        booking = make_booking(
            make_guest("night-audit-stay@example.com"), self.room_type,
            rooms=[self.room], check_in=self.today - timedelta(days=1),
            check_out=self.today + timedelta(days=1), total="20000.00",
        )
        stay = Stay.objects.create(
            reference=f"STY-{booking.booking_reference}", booking=booking, guest=booking.guest,
            status=Stay.Status.IN_HOUSE, expected_arrival=booking.check_in,
            expected_departure=booking.check_out,
        )
        StayRoom.objects.create(stay=stay, room=self.room, assigned_by=self.manager)
        self.stay = stay
        self.folio, _ = get_or_create_main_folio_for_stay(stay=stay, actor=self.manager)
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.OTHER_CHARGE,
            source_key="night-audit-stay:charge:1",
            actor=self.manager,
            source_reference=booking.booking_reference,
            lines=[
                {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": FinancialLine.Direction.DEBIT, "amount": "5000.00", "folio": self.folio},
                {"account_code": accounting.OTHER_OPERATING_REVENUE, "direction": FinancialLine.Direction.CREDIT, "amount": "5000.00"},
            ],
            postings=[{"line_index": 0, "folio": self.folio, "kind": "CHARGE", "effect": "DEBIT", "amount": "5000.00"}],
        )

    def test_in_house_stay_appears_with_unbilled_folio_balance(self):
        self.auth(self.manager)
        response = self.client.get("/api/admin/finance/reports/night-audit/")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["in_house_count"], 1)
        self.assertEqual(len(data["unbilled_in_house_folios"]), 1)
        row = data["unbilled_in_house_folios"][0]
        self.assertEqual(row["stay_reference"], self.stay.reference)
        self.assertEqual(row["room"], "702")
        self.assertEqual(row["folio_balance"], "5000.00")
        self.assertFalse(
            any(d["type"] == "OCCUPIED_NOT_IN_HOUSE" for d in data["discrepancies"]),
            data["discrepancies"],
        )
        self.assertFalse(
            any(d["type"] == "IN_HOUSE_NO_FOLIO" for d in data["discrepancies"]),
            data["discrepancies"],
        )

    def test_multi_room_booking_receivable_includes_each_folio_once(self):
        from apps.finance.models import Folio, FolioPosting
        from apps.finance.services.night_audit_service import accounts_receivable_aging

        first = make_room(self.room_type, "703")
        second = make_room(self.room_type, "704")
        guest = make_guest("batch02-multi-room@example.com")
        booking = make_booking(guest, self.room_type, rooms=[first, second],
            number_of_rooms=2, check_in=self.today - timedelta(days=1),
            check_out=self.today + timedelta(days=1), total="10000.00", amount_paid="5000.00")
        for i, amount in enumerate(("1000.00", "2000.00")):
            folio = Folio.objects.create(reference=f"FOL-BATCH02-{i}", booking=booking, guest=guest)
            create_posted_transaction(
                transaction_type=FinancialTransaction.Type.OTHER_CHARGE,
                actor=self.manager, source_key=f"batch02:room:{i}", business_date=self.today,
                lines=[
                    {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": "DEBIT", "amount": amount, "folio": folio},
                    {"account_code": accounting.OTHER_OPERATING_REVENUE, "direction": "CREDIT", "amount": amount},
                ],
                postings=[{"line_index": 0, "folio": folio, "kind": FolioPosting.Kind.CHARGE,
                           "effect": FolioPosting.Effect.DEBIT, "amount": amount}],
            )
        report = accounts_receivable_aging(as_of=self.today)
        # Batch 01 task 7: one row per folio, rolled up once per booking. The
        # two room folios are each counted exactly once in both views.
        folio_rows = [item for item in report["items"]
                      if item["booking_reference"] == booking.booking_reference]
        self.assertEqual(len(folio_rows), 2)
        self.assertEqual(sorted(row["balance_due"] for row in folio_rows), ["1000.00", "2000.00"])
        roll_up = next(item for item in report["by_booking"]
                       if item["booking_reference"] == booking.booking_reference)
        self.assertEqual(roll_up["folios"], 2)
        self.assertEqual(roll_up["outstanding"], "3000.00")
        # ₦3,000 of this booking is posted to the guest ledger; the ₦5,000 of
        # room nights the night audit has not charged yet is reported in the
        # separate exposure figure, so the operational total still reaches
        # ₦8,000 for this booking without double counting either half.
        from apps.finance.services.guest_receivables_service import unposted_stay_balance

        self.assertEqual(
            Decimal(report["total_stay_exposure"]) - Decimal(report["total_outstanding"]),
            Decimal(report["stay_value_not_yet_charged"]),
        )
        self.assertGreaterEqual(Decimal(unposted_stay_balance(
            as_of=self.today, currency="NGN")["amount"]), Decimal("5000.00"))
        # Every figure still ties back to the guest-receivable control account.
        self.assertTrue(report["reconciliation"]["reconciles"])
        self.assertEqual(
            report["reconciliation"]["control_account_balance"], report["net_receivable"])
