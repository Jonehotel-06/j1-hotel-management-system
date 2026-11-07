"""Independent financial-integrity verification for J-ONE Hotel Management System.

This test does NOT trust the prior AI completion reports. It posts real, balanced
 FinancialTransactions through the immutable ledger service for every revenue,
 collection, expense, refund and reversal source, then asserts that
 ``financial_summary`` returns one consistent, non-duplicated, fully classified
 set of figures and that ``build_reconciliation`` reconciles every required
 arithmetic identity (task §14).

 It deliberately exercises the shared ``financial_summary`` contract that BOTH the
 Accounting page and the Reports page consume, so a failure here means both pages
 would tell the wrong story.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase

from apps.core.utils import hotel_today, money
from apps.finance.models import FinancialTransaction, FinancialLine
from apps.finance.services.ledger_service import create_posted_transaction
from apps.finance.services.reporting_service import financial_summary
from tests.factories import make_user

DR, CR = FinancialLine.Direction.DEBIT, FinancialLine.Direction.CREDIT
T = FinancialTransaction.Type

CASH = "ASSET.CASH_ON_HAND"
BANK = "ASSET.BANK_CLEARING"
CARD = "ASSET.CARD_CLEARING"
AR = "ASSET.GUEST_RECEIVABLE"
DEPOSITS = "LIABILITY.GUEST_DEPOSITS"
AP = "LIABILITY.ACCOUNTS_PAYABLE"
ACCOM = "REVENUE.ACCOMMODATION"
FNB = "REVENUE.FOOD_BEVERAGE"
OTHER = "REVENUE.OTHER_OPERATING"
OPEX = "EXPENSE.OPERATING"
INV = "ASSET.INVENTORY"


def D(amount):
    return Decimal(str(amount))


class FinancialIntegrityRepairTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = make_user(email="audit@j1.test", role="ADMIN")
        cls.today = hotel_today()

        from apps.pos.models import PosOrder
        cls.pos_r = PosOrder.objects.create(reference="PO-R1", mode="RESTAURANT", currency="NGN")
        cls.pos_b = PosOrder.objects.create(reference="PO-B1", mode="BAR", currency="NGN")
        cls.pos_t = PosOrder.objects.create(reference="PO-T1", mode="TAKEAWAY", currency="NGN")

        # Room-service orders must be linked to a stay + folio (DB constraint).
        from apps.bookings.models import Booking, Guest
        from apps.rooms.models import RoomType
        from apps.stays.models import Stay
        from apps.finance.models import Folio
        rt = RoomType.objects.create(name="Std", base_price=Decimal("10000.00"), max_guests=2)
        guest = Guest.objects.create(first_name="Ada", last_name="Obi", email="stay@j1.test",
                                    phone="08031234567", country="Nigeria")
        booking = Booking.objects.create(
            booking_reference="J1-TEST-STAY01", guest=guest, room_type=rt,
            check_in=cls.today, check_out=cls.today, number_of_rooms=1, adults=2,
            currency="NGN", price_per_night=rt.base_price, subtotal=Decimal("10000.00"),
            total_amount=Decimal("10000.00"), required_payment=Decimal("10000.00"),
            status=Booking.Status.CONFIRMED,
        )
        stay = Stay.objects.create(
            reference="STAY-TEST01", booking=booking, guest=guest,
            expected_arrival=cls.today, expected_departure=cls.today + timedelta(days=3),
        )
        folio = Folio.objects.create(reference="FOLIO-TEST01", stay=stay, guest=guest, currency="NGN")
        cls.pos_rs = PosOrder.objects.create(reference="PO-RS1", mode="ROOM_SERVICE", currency="NGN",
                                             stay=stay, folio=folio)

        # Laundry orders (reference drives the laundry breakout)
        from apps.guest_services.models import LaundryOrder
        cls.laun_in = LaundryOrder.objects.create(reference="LAUN-IN1", currency="NGN", total_amount=D(250))
        cls.laun_walk = LaundryOrder.objects.create(reference="LAUN-WALK1", currency="NGN", total_amount=D(120))

    def post(self, tx_type, lines, **kw):
        kw.setdefault("actor", self.user)
        kw.setdefault("currency", "NGN")
        kw.setdefault("business_date", self.today)
        tx, _created = create_posted_transaction(transaction_type=tx_type, lines=lines, **kw)
        return tx

    def summary(self):
        return financial_summary(start_date=self.today, end_date=self.today, currency="NGN")

    def amt(self, report, key):
        v = report.get(key)
        if isinstance(v, dict):
            return D(v["amount"])
        return D(v)

    def test_full_lifecycle_no_double_counting(self):
        # 1. Booking deposit (collection + deposit liability, NOT revenue)
        self.post(T.PAYMENT_COLLECTION,
                  [{"account_code": CASH, "direction": DR, "amount": 1000.00},
                   {"account_code": DEPOSITS, "direction": CR, "amount": 1000.00}],
                  source_key="bk1")

        # 2. Accommodation charge (earned revenue)
        acc = self.post(T.ACCOMMODATION_CHARGE,
                        [{"account_code": AR, "direction": DR, "amount": 2000.00},
                         {"account_code": ACCOM, "direction": CR, "amount": 2000.00}],
                        source_key="acc1")

        # 3-6. POS sales across outlets
        self.post(T.POS_CHARGE,
                  [{"account_code": AR, "direction": DR, "amount": 500.00},
                   {"account_code": FNB, "direction": CR, "amount": 500.00}],
                  source_key="r1", source_reference=self.pos_r.reference)
        self.post(T.POS_CHARGE,
                  [{"account_code": AR, "direction": DR, "amount": 300.00},
                   {"account_code": FNB, "direction": CR, "amount": 300.00}],
                  source_key="b1", source_reference=self.pos_b.reference)
        self.post(T.POS_CHARGE,
                  [{"account_code": AR, "direction": DR, "amount": 200.00},
                   {"account_code": FNB, "direction": CR, "amount": 200.00}],
                  source_key="t1", source_reference=self.pos_t.reference)
        rs = self.post(T.POS_CHARGE,
                       [{"account_code": AR, "direction": DR, "amount": 400.00},
                        {"account_code": FNB, "direction": CR, "amount": 400.00}],
                       source_key="rs1", source_reference=self.pos_rs.reference)

        # 7. Other operating revenue
        self.post(T.OTHER_CHARGE,
                  [{"account_code": AR, "direction": DR, "amount": 150.00},
                   {"account_code": OTHER, "direction": CR, "amount": 150.00}],
                  source_key="oth1")

        # 8. In-house laundry (billed to folio, revenue now, collected later)
        self.post(T.OTHER_CHARGE,
                  [{"account_code": AR, "direction": DR, "amount": 250.00},
                   {"account_code": OTHER, "direction": CR, "amount": 250.00}],
                  source_key="laun1", source_reference=self.laun_in.reference)

        # 9. Walk-in laundry (payment collects cash AND recognizes revenue)
        self.post(T.PAYMENT_COLLECTION,
                  [{"account_code": CASH, "direction": DR, "amount": 120.00},
                   {"account_code": OTHER, "direction": CR, "amount": 120.00}],
                  source_key="laun2", source_reference=self.laun_walk.reference)

        # 10. Operating expense paid in cash
        self.post(T.EXPENSE,
                  [{"account_code": OPEX, "direction": DR, "amount": 600.00},
                   {"account_code": CASH, "direction": CR, "amount": 600.00}],
                  source_key="exp1")

        # 11. Inventory acquisition (asset, NOT an expense)
        self.post(T.EXPENSE,
                  [{"account_code": INV, "direction": DR, "amount": 800.00},
                   {"account_code": CASH, "direction": CR, "amount": 800.00}],
                  source_key="inv1")

        # 12. Guest refund
        self.post(T.REFUND,
                  [{"account_code": DEPOSITS, "direction": DR, "amount": 100.00},
                   {"account_code": CASH, "direction": CR, "amount": 100.00}],
                  source_key="ref1")

        # 13. Reversal of the restaurant sale (must reduce restaurant revenue, not double count)
        self.post(T.VOID,
                  [{"account_code": AR, "direction": CR, "amount": 500.00},
                   {"account_code": FNB, "direction": DR, "amount": 500.00}],
                  source_key="rev1", source_reference=self.pos_r.reference, reversal_of=acc)

        s = self.summary()

        # --- Revenue classification -------------------------------------
        self.assertEqual(self.amt(s, "recognized_revenue"), D("3420.00"), s)
        self.assertEqual(self.amt(s, "accommodation_revenue"), D("2000.00"))
        self.assertEqual(self.amt(s, "restaurant_bar_revenue"), D("500.00"))   # 500+300+200 - 500 reversal
        self.assertEqual(self.amt(s, "room_service_revenue"), D("400.00"))
        self.assertEqual(self.amt(s, "other_operating_revenue"), D("520.00"))   # 150+250+120
        # Laundry is a breakout of other operating income, never added on top.
        self.assertEqual(self.amt(s, "laundry_revenue"), D("370.00"))           # 250 in-house + 120 walk-in
        # Room service must NOT be counted in restaurant/bar.
        self.assertEqual(self.amt(s, "restaurant_bar_revenue") + self.amt(s, "room_service_revenue"), D("900.00"))

        # --- Collections -------------------------------------------------
        self.assertEqual(self.amt(s, "collections"), D("1120.00"))              # 1000 booking + 120 walk-in laundry
        self.assertEqual(self.amt(s, "booking_payments_received"), D("1000.00"))
        self.assertEqual(self.amt(s, "laundry_collections"), D("120.00"))
        self.assertEqual(self.amt(s, "refunds"), D("100.00"))
        self.assertEqual(self.amt(s, "net_collections"), D("1020.00"))         # 1120 - 100

        # --- Expenses ----------------------------------------------------
        # Inventory is an asset, must be excluded from total/operating expenses.
        self.assertEqual(self.amt(s, "total_expenses"), D("600.00"))
        self.assertEqual(self.amt(s, "operating_expenses"), D("600.00"))
        self.assertEqual(self.amt(s, "inventory_acquisitions"), D("800.00"))
        self.assertEqual(self.amt(s, "total_cash_disbursements") if "total_cash_disbursements" in s
                         else self.amt(s["supplier_payments"], "total_cash_disbursements"),
                         D("1400.00"))  # 600 operating + 800 inventory
        self.assertEqual(self.amt(s, "net_cash_movement"), D("-380.00"))       # 1120 - 100 - 1400

        # --- Reconciliation (task §14) ----------------------------------
        recon = s["reconciliation"]
        self.assertIn(recon["status"], ("RECONCILED", "RECONCILED_WITH_EXCEPTIONS"))
        by_key = {c["key"]: c for c in recon["checks"]}
        for key in (
            "journals_balanced", "revenue_category_sum", "net_collections_identity",
            "collections_breakdown_bound", "inventory_not_in_expenses",
            "guest_advances_not_in_revenue", "duplicate_active_postings",
            "reversals_linked", "net_income", "net_result", "net_cash_movement",
            "cash_disbursement_components", "expense_breakdown", "outstanding_payables",
            "guest_receivables", "guest_receivable_control", "settled_guest_is_zero",
            "guest_credits_visible",
        ):
            self.assertIn(key, by_key, f"missing reconciliation check: {key}")
            self.assertTrue(by_key[key]["reconciles"],
                            f"{key} failed: {by_key[key]}")
        self.assertEqual(recon["duplicate_posting_scan"]["duplicate_active_postings"], 0)
        self.assertEqual(recon["duplicate_posting_scan"]["unlinked_reversals"], 0)
        self.assertEqual(recon["duplicate_posting_scan"]["orphan_reversals"], 0)

    def test_idempotent_retry_returns_same_posting(self):
        """A retried provider webhook / double-click must not create a second posting."""
        lines = [{"account_code": CASH, "direction": DR, "amount": 300.00},
                 {"account_code": DEPOSITS, "direction": CR, "amount": 300.00}]
        first, created1 = create_posted_transaction(
            transaction_type=T.PAYMENT_COLLECTION, lines=lines,
            actor=self.user, currency="NGN", business_date=self.today, source_key="idem1")
        second, created2 = create_posted_transaction(
            transaction_type=T.PAYMENT_COLLECTION, lines=lines,
            actor=self.user, currency="NGN", business_date=self.today, source_key="idem1")
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            FinancialTransaction.objects.filter(source_key="idem1").count(), 1)

    def test_reconciliation_flags_duplicate_source_event(self):
        """Two POSTED transactions sharing source_reference+type+amount+date are flagged."""
        lines = [{"account_code": CASH, "direction": DR, "amount": 250.00},
                 {"account_code": DEPOSITS, "direction": CR, "amount": 250.00}]
        self.post(T.PAYMENT_COLLECTION, lines, source_key="dupA", source_reference="DUP-EVT")
        self.post(T.PAYMENT_COLLECTION, lines, source_key="dupB", source_reference="DUP-EVT")
        s = financial_summary(start_date=self.today, end_date=self.today, currency="NGN", audit_scans=True)
        recon = s["reconciliation"]
        scan = recon["duplicate_posting_scan"]
        self.assertEqual(scan["duplicate_active_postings"], 1)
        # The arithmetic checks still reconcile; the duplicate is an exception, not a silent fix.
        self.assertIn(recon["status"], ("RECONCILED", "RECONCILED_WITH_EXCEPTIONS"))
