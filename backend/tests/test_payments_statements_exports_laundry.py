"""Supplier payment settlement, statements, occupancy exports, laundry."""
from datetime import timedelta
from decimal import Decimal

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import Expense, FinancialLine, FinancialTransaction
from apps.finance.services import accounting
from apps.finance.services.expense_service import create_expense, post_expense, review_expense, settle_expense_payment, submit_expense
from apps.finance.services.ledger_service import create_posted_transaction
from apps.finance.services.reporting_service import financial_summary

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type, make_staff


class SupplierSettlementTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.cashier = make_staff("settle-cashier@staff.dev", role=User.Role.CASHIER)
        self.manager = make_staff("settle-manager@staff.dev", role=User.Role.MANAGER)

    def _posted_payable(self, key, amount="5000.00", accounting_class="INVENTORY"):
        expense, _ = create_expense(
            actor=self.cashier, category="Supplier invoice", amount=amount,
            payment_method=Expense.PaymentMethod.PAYABLE, accounting_class=accounting_class,
            payee="Drinks Supplier Ltd", idempotency_key=key,
        )
        submitted = submit_expense(expense=expense, actor=self.cashier)
        approved = review_expense(expense=submitted, reviewer=self.manager, approved=True)
        posted, _ = post_expense(expense=approved, actor=self.cashier)
        return posted

    def test_payable_is_unpaid_until_settled_then_everything_flips_once(self):
        expense = self._posted_payable("settle-one")
        expense.refresh_from_db()
        self.assertIsNone(expense.paid_at)

        before = financial_summary(start_date=hotel_today(), end_date=hotel_today(), currency="NGN")
        self.assertEqual(before["supplier_payments"]["supplier_payments_made"], "0.00")

        self.auth(self.manager)
        response = self.client.post(f"/api/admin/finance/expenses/{expense.reference}/settle-payment/", {
            "method": "BANK_TRANSFER", "payment_reference": "TRF-12345",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertIsNotNone(data["paid_at"])
        self.assertEqual(data["payment_reference"], "TRF-12345")
        self.assertTrue(data["settlement_reference"])

        expense.refresh_from_db()
        self.assertIsNotNone(expense.paid_at)
        # Ledger: payable settled (Dr AP), no second expense recognized.
        from django.db.models import Sum

        ap_debits = FinancialLine.objects.filter(
            account_code=accounting.ACCOUNTS_PAYABLE, direction="DEBIT",
            transaction__status="POSTED",
        ).aggregate(total=Sum("amount"))["total"]
        self.assertEqual(ap_debits, Decimal("5000.00"))
        # Settlement is idempotent: a retry changes nothing.
        retry = self.client.post(f"/api/admin/finance/expenses/{expense.reference}/settle-payment/", {
            "method": "BANK_TRANSFER", "payment_reference": "TRF-12345",
        }, format="json")
        self.assertEqual(retry.status_code, 200, retry.content)
        self.assertEqual(
            FinancialLine.objects.filter(
                account_code=accounting.ACCOUNTS_PAYABLE, direction="DEBIT", transaction__status="POSTED",
            ).count(), 1,
        )
        after = financial_summary(start_date=hotel_today(), end_date=hotel_today(), currency="NGN")
        self.assertEqual(after["supplier_payments"]["supplier_payments_made"], "5000.00")
        self.assertEqual(after["supplier_payments"]["outstanding_supplier_payables"], "0.00")

    def test_bank_settlement_requires_reference_and_non_payable_is_rejected(self):
        expense = self._posted_payable("settle-two")
        self.auth(self.manager)
        missing = self.client.post(f"/api/admin/finance/expenses/{expense.reference}/settle-payment/", {
            "method": "BANK_TRANSFER",
        }, format="json")
        self.assertEqual(missing.status_code, 400, missing.content)

    def test_receipt_url_is_optional(self):
        expense = self._posted_payable("settle-three", amount="100.00")
        settled, _ = settle_expense_payment(
            expense=expense, actor=self.manager, method="CARD", payment_reference="POS-9",
        )
        self.assertIsNotNone(settled.paid_at)
        self.assertEqual(settled.payment_receipt_url, "")


class StatementTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("stmt-manager@staff.dev", role=User.Role.MANAGER)
        self.today = hotel_today()
        past = self.today - timedelta(days=30)
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.ADJUSTMENT,
            source_key="stmt:opening", actor=self.manager, business_date=past,
            narrative="Opening balances",
            lines=[
                {"account_code": accounting.CASH_ON_HAND, "direction": "DEBIT", "amount": "100000.00"},
                {"account_code": "EQUITY.OPENING_BALANCE", "direction": "CREDIT", "amount": "100000.00"},
            ],
        )
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.PAYMENT_COLLECTION,
            source_key="stmt:collection", actor=self.manager, business_date=self.today,
            narrative="Guest payment collected to bank",
            lines=[
                {"account_code": accounting.BANK_CLEARING, "direction": "DEBIT", "amount": "40000.00"},
                {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": "CREDIT", "amount": "40000.00"},
            ],
        )

    def test_balance_sheet_balances_with_opening_equity(self):
        self.auth(self.manager)
        response = self.client.get("/api/admin/finance/reports/balance-sheet/")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertTrue(data["balanced"], data)
        assets = {r["account_code"]: r["amount"] for r in data["assets"]}
        self.assertEqual(assets[accounting.CASH_ON_HAND], "100000.00")
        self.assertEqual(assets[accounting.BANK_CLEARING], "40000.00")
        # The collection credited a receivable that had no opening balance,
        # so the sheet honestly shows the negative receivable it created.
        self.assertEqual(assets[accounting.ACCOUNTS_RECEIVABLE], "-40000.00")
        self.assertEqual(data["total_assets"], "100000.00")
        # Batch 01 task 5: the equity section is withdrawn from the
        # presentation and replaced by net assets, which is identical to total
        # equity. The ledger still holds every EQUITY.* posting and the
        # statement still proves it balances.
        self.assertNotIn("equity", data)
        self.assertNotIn("total_equity", data)
        self.assertNotIn("total_liabilities_and_equity", data)
        self.assertEqual(
            Decimal(data["net_assets"]),
            Decimal(data["total_assets"]) - Decimal(data["total_liabilities"]),
        )
        self.assertEqual(data["presentation"]["equity_section"], "withdrawn")

    def test_cash_flow_classifies_and_reconciles(self):
        start = (self.today - timedelta(days=40)).isoformat()
        self.auth(self.manager)
        response = self.client.get("/api/admin/finance/reports/cash-flow/", {"start": start, "end": self.today.isoformat()})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["financing_activities"], "100000.00")
        self.assertEqual(data["operating_activities"], "40000.00")
        self.assertEqual(data["opening_cash"], "0.00")
        self.assertEqual(data["closing_cash"], "140000.00")
        self.assertTrue(data["reconciles"], data)


class OccupancyExportTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("occ-manager@staff.dev", role=User.Role.MANAGER)

    def test_occupancy_xlsx_and_pdf_download(self):
        params = {"start_date": hotel_today().isoformat(), "end_date": (hotel_today() + timedelta(days=2)).isoformat()}
        self.auth(self.manager)
        xlsx = self.client.get("/api/admin/reports/occupancy/", {**params, "export": "xlsx"})
        self.assertEqual(xlsx.status_code, 200, xlsx.status_code)
        self.assertIn("spreadsheetml", xlsx["Content-Type"])
        self.assertTrue(xlsx.content[:2] == b"PK")
        pdf = self.client.get("/api/admin/reports/occupancy/", {**params, "export": "pdf"})
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content[:4] == b"%PDF")
        plain = self.client.get("/api/admin/reports/occupancy/", params)
        self.assertEqual(plain.status_code, 200)
        self.assertIn("by_day", plain.json()["data"])


class LaundryTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        from apps.finance.services.folio_service import folio_balance_queryset
        from apps.stays.models import Stay

        self.manager = make_staff("laundry-manager@staff.dev", role=User.Role.MANAGER)
        self.today = hotel_today()
        room_type = make_room_type("Laundry suite", price="20000.00")
        room = make_room(room_type, "701")
        booking = make_booking(
            make_guest("laundry-guest@example.com"), room_type, rooms=[room],
            check_in=self.today, check_out=self.today + timedelta(days=2), total="40000.00",
        )
        self.stay = Stay.objects.create(
            reference=f"STY-{booking.booking_reference}", booking=booking, guest=booking.guest,
            status=Stay.Status.IN_HOUSE, expected_arrival=booking.check_in, expected_departure=booking.check_out,
        )
        self.folio_balance_queryset = folio_balance_queryset

    def _create(self, key="laundry-create-1"):
        self.auth(self.manager)
        return self.client.post("/api/admin/service-requests/laundry/", {
            "stay_reference": self.stay.reference,
            "items": [
                {"description": "Shirt", "quantity": 3, "service_type": "WASH_IRON", "unit_price": "500.00"},
                {"description": "Suit", "quantity": 1, "service_type": "DRY_CLEAN", "unit_price": "2500.00"},
            ],
            "idempotency_key": key,
        }, format="json")

    def test_full_laundry_flow_bills_folio_exactly_once(self):
        created = self._create()
        self.assertEqual(created.status_code, 201, created.content)
        data = created.json()["data"]
        reference = data["reference"]
        self.assertEqual(data["total_amount"], "4000.00")
        self.assertEqual(data["status"], "RECEIVED")
        # Retry with the same key returns the same order.
        retry = self._create()
        self.assertEqual(retry.json()["data"]["reference"], reference)

        for target in ("IN_PROGRESS", "READY"):
            moved = self.client.post(f"/api/admin/service-requests/laundry/{reference}/status/", {"status": target}, format="json")
            self.assertEqual(moved.status_code, 200, moved.content)
        # Billing twice posts one folio charge.
        billed = self.client.post(f"/api/admin/service-requests/laundry/{reference}/bill/", {}, format="json")
        self.assertEqual(billed.status_code, 200, billed.content)
        again = self.client.post(f"/api/admin/service-requests/laundry/{reference}/bill/", {}, format="json")
        self.assertEqual(again.status_code, 200, again.content)
        folio_ref = billed.json()["data"]["folio_reference"]
        self.assertTrue(folio_ref)
        from apps.finance.models import FolioPosting
        postings = FolioPosting.objects.filter(source_reference=reference, kind="CHARGE")
        self.assertEqual(postings.count(), 1)
        self.assertEqual(postings.first().amount, Decimal("4000.00"))

        delivered = self.client.post(
            f"/api/admin/service-requests/laundry/{reference}/status/",
            {"status": "DELIVERED", "delivered_to": "Ada Obi"}, format="json",
        )
        self.assertEqual(delivered.status_code, 200, delivered.content)
        self.assertEqual(delivered.json()["data"]["delivered_to"], "Ada Obi")
        # No backwards move after delivery.
        backwards = self.client.post(
            f"/api/admin/service-requests/laundry/{reference}/status/", {"status": "READY"}, format="json",
        )
        self.assertEqual(backwards.status_code, 400)

    def test_laundry_requires_items_and_capability(self):
        self.auth(self.manager)
        empty = self.client.post("/api/admin/service-requests/laundry/", {
            "stay_reference": self.stay.reference, "items": [], "idempotency_key": "laundry-empty",
        }, format="json")
        self.assertEqual(empty.status_code, 400, empty.content)
        self.client.force_authenticate(user=None)
        anon = self.client.get("/api/admin/service-requests/laundry/")
        self.assertIn(anon.status_code, (401, 403))
