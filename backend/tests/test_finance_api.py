"""Capability-scoped, paginated finance read/control API tests."""
from datetime import timedelta
from decimal import Decimal

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import FinancialControlPolicy, FinancialLine, FinancialTransaction
from apps.finance.services import accounting
from apps.finance.services.cash_session_service import close_cash_session, open_cash_session
from apps.finance.services.folio_service import get_or_create_main_folio_for_stay
from apps.finance.services.ledger_service import create_posted_transaction
from apps.stays.models import Stay

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room_type, make_staff


class FinanceApiTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("finance-api-manager@staff.dev", role=User.Role.MANAGER)
        self.cashier = make_staff("finance-api-cashier@staff.dev", role=User.Role.CASHIER)
        room_type = make_room_type("Finance API suite", price="1000.00")
        booking = make_booking(
            make_guest("finance-api-guest@example.com"), room_type,
            check_in=hotel_today(), check_out=hotel_today() + timedelta(days=2), total="2000.00",
        )
        stay = Stay.objects.create(
            reference=f"STY-{booking.booking_reference}", booking=booking, guest=booking.guest,
            status=Stay.Status.IN_HOUSE, expected_arrival=booking.check_in, expected_departure=booking.check_out,
        )
        self.folio, _ = get_or_create_main_folio_for_stay(stay=stay, actor=self.manager)
        self.transaction, _ = create_posted_transaction(
            transaction_type=FinancialTransaction.Type.OTHER_CHARGE,
            source_key="finance-api:charge:1",
            actor=self.manager,
            source_reference=booking.booking_reference,
            lines=[
                {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": FinancialLine.Direction.DEBIT, "amount": "100.00", "folio": self.folio},
                {"account_code": accounting.OTHER_OPERATING_REVENUE, "direction": FinancialLine.Direction.CREDIT, "amount": "100.00"},
            ],
            postings=[{"line_index": 0, "folio": self.folio, "kind": "CHARGE", "effect": "DEBIT", "amount": "100.00"}],
        )

    def test_folio_and_transaction_views_are_paged_and_capability_scoped(self):
        self.auth(self.manager)
        folios = self.client.get("/api/admin/finance/folios/?page_size=10")
        self.assertEqual(folios.status_code, 200, folios.content)
        self.assertEqual(folios.json()["data"][0]["reference"], self.folio.reference)
        self.assertEqual(folios.json()["data"][0]["balance"], "100.00")
        postings = self.client.get(f"/api/admin/finance/folios/{self.folio.reference}/postings/")
        self.assertEqual(postings.status_code, 200, postings.content)
        transaction = self.client.get(f"/api/admin/finance/transactions/{self.transaction.reference}/")
        self.assertEqual(transaction.status_code, 200, transaction.content)
        self.assertEqual(len(transaction.json()["data"]["lines"]), 2)

        self.auth(self.cashier)
        self.assertEqual(self.client.get("/api/admin/finance/folios/").status_code, 403)
        self.assertEqual(self.client.get("/api/admin/finance/transactions/").status_code, 403)

    def test_manager_uses_controlled_endpoint_to_finish_cash_variance_review(self):
        policy = FinancialControlPolicy.objects.get(name="Default financial controls")
        policy.cash_variance_manager_approval_threshold = Decimal("10.00")
        policy.save(update_fields=["cash_variance_manager_approval_threshold", "updated_at"])
        session = open_cash_session(cashier=self.cashier, actor=self.cashier, opening_float="100.00")
        close_cash_session(cash_session=session, cashier=self.cashier, counted_cash="0.00")
        self.auth(self.manager)
        response = self.client.post(
            f"/api/admin/finance/cash-sessions/{session.reference}/variance-review/",
            {"approved": True, "review_note": "Count reviewed."},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["status"], "CLOSED")

    def test_manager_can_create_effective_dated_controls(self):
        self.auth(self.manager)
        response = self.client.post("/api/admin/finance/control-policies/", {
            "name": "API future policy",
            "effective_from": (hotel_today() + timedelta(days=1)).isoformat(),
            "is_active": True,
            "require_separate_approver": True,
            "refund_manager_approval_threshold": "5000.00",
            "discount_manager_approval_threshold": "1000.00",
            "cash_variance_manager_approval_threshold": "750.00",
            "notes": "Test policy",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["data"]["refund_manager_approval_threshold"], "5000.00")
