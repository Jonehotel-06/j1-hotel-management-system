"""Focused accounting rebuild tests; all data is synthetic and isolated."""
from datetime import timedelta
from decimal import Decimal
from io import StringIO, BytesIO
from unittest.mock import patch
import json

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.utils import timezone

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import (CashMovement, CashSession, Expense, FinancialLine, FinancialTransaction,
                                 FolioPosting, SupplierSettlement, GuestCollectionKey)
from apps.finance.services import accounting
from apps.finance.services.accommodation_service import post_accommodation_charge_for_night
from apps.finance.services.expense_service import create_expense, post_expense, review_expense, submit_expense, settle_expense_payment
from apps.finance.services.folio_service import get_or_create_main_folio_for_stay
from apps.finance.services.ledger_service import LedgerIntegrityError, create_posted_transaction
from apps.finance.services.payment_collection_service import (ensure_payment_collection, apply_payment_to_folio,
                                                              settle_outstanding_balance)
from apps.finance.services.supplier_payment_service import (payable_balance, propose_supplier_payment,
    submit_supplier_payment, review_supplier_payment, execute_supplier_payment)
from apps.payments.models import Payment
from apps.pos.models import PosOrder
from apps.pos.services.order_service import _post_order_charge
from apps.stays.models import Stay, StayRoom

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room, make_room_type, make_staff


def balance(code):
    rows = FinancialLine.objects.filter(account_code=code, transaction__status="POSTED")
    return sum((r.amount if r.direction == "DEBIT" else -r.amount for r in rows), Decimal("0.00"))


class RebuildLedgerTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("rebuild-manager@staff.dev", role=User.Role.MANAGER)
        self.reviewer = make_staff("rebuild-reviewer@staff.dev", role=User.Role.MANAGER)
        self.desk = make_staff("rebuild-desk@staff.dev", role=User.Role.RECEPTIONIST)
        self.today = hotel_today()

    def journal(self, key, debit, credit, amount):
        return create_posted_transaction(transaction_type=FinancialTransaction.Type.ADJUSTMENT,
            source_key=key, idempotency_key=key, actor=self.manager,
            lines=[{"account_code": debit, "direction": "DEBIT", "amount": amount},
                   {"account_code": credit, "direction": "CREDIT", "amount": amount}])

    def test_atomic_balance_idempotency_and_querysets_are_guarded(self):
        with self.assertRaises(LedgerIntegrityError):
            create_posted_transaction(transaction_type=FinancialTransaction.Type.ADJUSTMENT,
                source_key="bad-rebuild", lines=[{"account_code": "ASSET.CASH_ON_HAND", "direction": "DEBIT", "amount": "2"},
                                                 {"account_code": "EQUITY.OWNER_CAPITAL", "direction": "CREDIT", "amount": "1"}])
        self.assertFalse(FinancialTransaction.objects.filter(source_key="bad-rebuild").exists())
        tx, created = self.journal("safe-rebuild", accounting.CASH_ON_HAND, accounting.OWNER_CAPITAL, "100.00")
        self.assertTrue(created)
        same, created = self.journal("safe-rebuild", accounting.CASH_ON_HAND, accounting.OWNER_CAPITAL, "100.00")
        self.assertEqual(same.pk, tx.pk)
        self.assertFalse(created)
        with self.assertRaises(LedgerIntegrityError):
            self.journal("safe-rebuild", accounting.CASH_ON_HAND, accounting.OWNER_CAPITAL, "101.00")
        with self.assertRaises(ValidationError):
            FinancialTransaction.objects.filter(pk=tx.pk).update(narrative="tamper")
        with self.assertRaises(ValidationError):
            FinancialLine.objects.filter(transaction=tx).update(amount=Decimal("1.00"))
        with self.assertRaises(ValidationError):
            FinancialLine.objects.filter(transaction=tx).delete()
        with self.assertRaises(ValidationError):
            FinancialTransaction.objects.create(reference="FAKE", type="ADJUSTMENT", status="POSTED")

    def test_reports_reconcile_and_internal_transfer_is_not_revenue(self):
        self.journal("receipt", accounting.BANK_CLEARING, accounting.GUEST_DEPOSITS, "100000.00")
        self.journal("room-earned", accounting.ACCOUNTS_RECEIVABLE, accounting.ACCOMMODATION_REVENUE, "100000.00")
        self.journal("room-applied", accounting.GUEST_DEPOSITS, accounting.ACCOUNTS_RECEIVABLE, "100000.00")
        self.journal("food-earned", accounting.ACCOUNTS_RECEIVABLE, accounting.FOOD_BEVERAGE_REVENUE, "15000.00")
        self.journal("cash-transfer", accounting.CASH_ON_HAND, accounting.BANK_CLEARING, "10000.00")
        self.auth(self.manager)
        root = "/api/admin/finance/rebuild/"
        overview = self.client.get(root+"overview/").json()["data"]
        self.assertEqual(overview["earned_revenue"], "115000.00")
        self.assertEqual(overview["guest_receivables_control"], "15000.00")
        self.assertEqual(overview["guest_advances_control"], "0.00")
        self.assertEqual(overview["cash_balance"], "10000.00")
        self.assertEqual(overview["bank_total"], "90000.00")
        trial = self.client.get(root+"reports/trial-balance/").json()["data"]
        sheet = self.client.get(root+"reports/balance-sheet/").json()["data"]
        self.assertTrue(trial["balanced"])
        self.assertTrue(sheet["balanced"])
        self.assertEqual(sheet["totals"]["assets"], "115000.00")
        self.assertEqual(sheet["totals"]["equity_including_earnings"], "115000.00")
        self.assertEqual(self.client.get(root+"reports/profit-loss/").json()["data"]["totals"]["net_profit"], "115000.00")
        ledger = self.client.get(root+"reports/general-ledger/", {"account": accounting.BANK_CLEARING, "page_size": 1, "page": 2}).json()["data"]
        self.assertEqual(ledger["records"][0]["running_debit_balance"], "90000.00")

    def test_report_security_and_exports(self):
        root = "/api/admin/finance/rebuild/reports/trial-balance/"
        self.auth(self.desk)
        self.assertEqual(self.client.get(root).status_code, 403)
        self.auth(self.manager)
        self.journal("export", accounting.CASH_ON_HAND, accounting.OWNER_CAPITAL, "11.00")
        self.assertIn("no-store", self.client.get(root)["Cache-Control"])
        self.assertEqual(self.client.get(root, {"start": "2026-02-30"}).status_code, 400)
        for fmt in ("csv", "xlsx", "pdf"):
            response = self.client.get(root, {"export": fmt})
            self.assertEqual(response.status_code, 200, (fmt, response.content))
            self.assertIn("attachment", response["Content-Disposition"])
            if fmt == "pdf": self.assertTrue(response.content.startswith(b"%PDF"))
            if fmt == "xlsx": self.assertTrue(response.content.startswith(b"PK"))

    def test_exports_keep_preview_context_escape_formulas_and_preserve_numeric_values(self):
        from openpyxl import load_workbook
        create_posted_transaction(transaction_type=FinancialTransaction.Type.ADJUSTMENT,
            source_key="export-injection", idempotency_key="export-injection", actor=self.manager,
            lines=[{"account_code": accounting.CASH_ON_HAND, "direction": "DEBIT", "amount": "11.00"},
                   {"account_code": accounting.OWNER_CAPITAL, "direction": "CREDIT", "amount": "11.00",
                    "description": "=HYPERLINK(\"https://example.invalid\")"}])
        self.auth(self.manager)
        url = "/api/admin/finance/rebuild/reports/general-ledger/"
        args = {"account": accounting.OWNER_CAPITAL, "export": "csv"}
        csv_response = self.client.get(url, args)
        self.assertEqual(csv_response.status_code, 200)
        csv_text = csv_response.content.decode("utf-8-sig")
        self.assertIn("PREVIEW ONLY", csv_text)
        self.assertIn("'=HYPERLINK", csv_text)
        self.assertIn("-11.00", csv_text)
        # Even when every row fits in the chosen page size, asking for page 2
        # must never produce a misleading partial/empty "complete" export.
        self.assertEqual(self.client.get(url, {**args, "page": 2, "page_size": 100}).status_code, 400)
        args["export"] = "xlsx"
        workbook = load_workbook(BytesIO(self.client.get(url, args).content), data_only=False)
        self.assertIn("Report details", workbook.sheetnames)
        sheet = workbook["Ledger report"]
        headers = [cell.value for cell in sheet[1]]
        description = sheet.cell(2, headers.index("description") + 1)
        running = sheet.cell(2, headers.index("running_debit_balance") + 1)
        self.assertTrue(description.value.startswith("'=HYPERLINK"))
        self.assertEqual(running.data_type, "n")
        self.assertEqual(running.value, -11)

    def test_chart_admin_and_controlled_manual_reversal(self):
        self.auth(self.desk)
        create = "/api/admin/finance/rebuild/accounts/create/"
        self.assertEqual(self.client.post(create, {"code": "ASSET.BANK.A", "kind": "ASSET", "label": "Bank A"}, format="json").status_code, 403)
        self.auth(self.manager)
        self.assertEqual(self.client.post(create, {"code": "ASSET.BANK.A", "kind": "ASSET", "label": "Bank A"}, format="json").status_code, 201)
        from apps.finance.services.journal_service import post_manual_journal
        tx, _ = post_manual_journal(actor=self.manager, narrative="Evidence supported deposit", idempotency_key="manual-x",
            lines=[{"account_code": "ASSET.BANK.A", "direction": "DEBIT", "amount": "50.00"},
                   {"account_code": accounting.OWNER_CAPITAL, "direction": "CREDIT", "amount": "50.00"}])
        endpoint = f"/api/admin/finance/rebuild/journals/{tx.reference}/reverse/"
        self.assertEqual(self.client.post(endpoint, {"reason": "Correct the bank entry"}, format="json").status_code, 403)
        self.auth(self.reviewer)
        self.assertEqual(self.client.post(endpoint, {"reason": "Correct the bank entry"}, format="json").status_code, 201)
        self.assertEqual(self.client.post(endpoint, {"reason": "Correct the bank entry"}, format="json").status_code, 200)
        tx.refresh_from_db()
        self.assertEqual(tx.status, "POSTED")
        self.assertEqual(balance("ASSET.BANK.A"), Decimal("0.00"))
        self.assertEqual(tx.reversals.count(), 1)

    def test_migration_audit_is_read_only_and_reports_missing_payment(self):
        rt = make_room_type("Migration evidence room")
        booking = make_booking(make_guest("mig@example.com"), rt)
        Payment.objects.create(booking=booking, user=self.manager, reference="MISSING-COLLECTION", provider="CASH",
                               amount=Decimal("500.00"), currency="NGN", status="SUCCESS", paid_at=timezone.now())
        before = FinancialTransaction.objects.count()
        output = StringIO()
        call_command("accounting_migration_audit", stdout=output, max_exceptions=0)
        data = json.loads(output.getvalue())
        self.assertFalse(data["production_cutover_approved"])
        self.assertTrue(any(x["reason"] == "MISSING_COLLECTION_JOURNAL" for x in data["exceptions"]))
        self.assertEqual(FinancialTransaction.objects.count(), before)
        self.assertEqual(data["source_counts"]["payments.Payment"], 1)


class RebuildGuestTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.actor = make_staff("guest-account-manager@staff.dev", role=User.Role.MANAGER)
        self.today = hotel_today()

    def test_prepayment_food_and_overpayment_remain_separate(self):
        rt = make_room_type("Guest rebuilt suite", price="100000.00")
        room = make_room(rt, "701")
        booking = make_booking(make_guest("rebuild-guest@example.com"), rt, [room],
                               check_in=self.today, check_out=self.today+timedelta(days=1), total="100000.00", status="CHECKED_IN")
        stay = Stay.objects.create(reference="STY-REBUILD-GUEST", booking=booking, guest=booking.guest,
                    status="IN_HOUSE", expected_arrival=booking.check_in, expected_departure=booking.check_out)
        folio, _ = get_or_create_main_folio_for_stay(stay=stay, actor=self.actor)
        deposit = Payment.objects.create(booking=booking, user=self.actor, reference="PREPAY-X", provider="BANK_TRANSFER",
                    amount=Decimal("100000"), currency="NGN", status="SUCCESS", paid_at=timezone.now())
        ensure_payment_collection(payment=deposit, actor=self.actor)
        order = PosOrder.objects.create(reference="ROOM-FOOD-X", mode="ROOM_SERVICE", status="DELIVERED",
                    stay=stay, folio=folio, created_by=self.actor, subtotal=Decimal("15000"), total_amount=Decimal("15000"))
        _post_order_charge(order=order, actor=self.actor)
        self.assertEqual(balance(accounting.GUEST_DEPOSITS), Decimal("-100000.00"))
        self.assertEqual(balance(accounting.ACCOUNTS_RECEIVABLE), Decimal("15000.00"))
        extra = Payment.objects.create(booking=booking, user=self.actor, reference="EXTRA-X", provider="BANK_TRANSFER",
                    amount=Decimal("20000"), currency="NGN", status="SUCCESS", paid_at=timezone.now())
        ensure_payment_collection(payment=extra, actor=self.actor)
        apply_payment_to_folio(payment=extra, folio=folio, actor=self.actor, amount=Decimal("15000"),
                               settles=FolioPosting.Settles.FOLIO_CHARGES)
        self.assertEqual(balance(accounting.ACCOUNTS_RECEIVABLE), Decimal("0.00"))
        self.assertEqual(balance(accounting.GUEST_DEPOSITS), Decimal("-105000.00"))

    def test_desk_overpayment_replays_once_and_remains_an_unapplied_advance(self):
        rt = make_room_type("Overpayment suite", price="100000.00")
        room = make_room(rt, "980")
        booking = make_booking(make_guest("overpay-guest@example.com"), rt, [room],
                 check_in=self.today, check_out=self.today+timedelta(days=1), total="100000.00",
                 amount_paid="100000.00", status="CHECKED_IN")
        stay = Stay.objects.create(reference="STY-OVERPAY", booking=booking, guest=booking.guest,
                    status="IN_HOUSE", expected_arrival=booking.check_in, expected_departure=booking.check_out)
        folio, _ = get_or_create_main_folio_for_stay(stay=stay, actor=self.actor)
        order = PosOrder.objects.create(reference="ROOM-FOOD-OVERPAY", mode="ROOM_SERVICE", status="DELIVERED",
                    stay=stay, folio=folio, created_by=self.actor, subtotal=Decimal("15000"), total_amount=Decimal("15000"))
        _post_order_charge(order=order, actor=self.actor)
        self.auth(self.actor)
        url = f"/api/admin/bookings/{booking.booking_reference}/settle-balance/"
        payload = {"amount": "20000.00", "provider": "BANK_TRANSFER", "idempotency_key": "overpay-001",
                   "external_reference": "BANK-GUEST-OVERPAY-001", "include_unbilled": False}
        first = self.client.post(url, payload, format="json")
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(first.json()["data"]["settlement"]["unapplied_advance"], "5000.00")
        second = self.client.post(url, payload, format="json")
        self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(first.json()["data"]["reference"], second.json()["data"]["reference"])
        self.assertEqual(Payment.objects.filter(booking=booking).count(), 1)
        self.assertEqual(balance(accounting.ACCOUNTS_RECEIVABLE), Decimal("0.00"))
        self.assertEqual(balance(accounting.GUEST_DEPOSITS), Decimal("-5000.00"))
        self.assertEqual(booking.amount_paid, Decimal("100000.00"))
        changed = {**payload, "amount": "21000.00"}
        self.assertEqual(self.client.post(url, changed, format="json").status_code, 400)
        repeated_ref = {**payload, "idempotency_key": "other-key"}
        self.assertEqual(self.client.post(url, repeated_ref, format="json").status_code, 400)
        self.assertEqual(Payment.objects.filter(booking=booking).count(), 1)

    def test_keyed_cash_requires_own_open_session_and_replay_adds_one_movement(self):
        rt = make_room_type("Cash room")
        booking = make_booking(make_guest("cash-overpay@example.com"), rt, total="1000.00", status="CONFIRMED")
        session = CashSession.objects.create(reference="DRAWER-REBUILD", cashier=self.actor,
                                             expected_cash=Decimal("20.00"), opening_float=Decimal("20.00"))
        self.auth(self.actor)
        url = f"/api/admin/bookings/{booking.booking_reference}/settle-balance/"
        body = {"amount": "1500.00", "provider": "CASH", "include_unbilled": False,
                "idempotency_key": "cash-key"}
        self.assertEqual(self.client.post(url, body, format="json").status_code, 400)
        body["cash_session_reference"] = session.reference
        first = self.client.post(url, body, format="json")
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(first.json()["data"]["settlement"]["stay_portion"], "1000.00")
        self.assertEqual(first.json()["data"]["settlement"]["unapplied_advance"], "500.00")
        self.assertEqual(self.client.post(url, body, format="json").status_code, 201)
        session.refresh_from_db()
        booking.refresh_from_db()
        self.assertEqual(session.expected_cash, Decimal("1520.00"))
        self.assertEqual(CashMovement.objects.filter(cash_session=session, type="COLLECTION").count(), 1)
        self.assertEqual(booking.amount_paid, Decimal("1000.00"))
        self.assertEqual(Payment.objects.filter(booking=booking).count(), 1)
        self.assertEqual(GuestCollectionKey.objects.count(), 1)
        self.assertEqual(balance(accounting.GUEST_DEPOSITS), Decimal("-1500.00"))

    def test_keyed_cash_failure_rolls_back_payment_journal_and_drawer(self):
        rt = make_room_type("Rollback room")
        booking = make_booking(make_guest("cash-rollback@example.com"), rt, total="1000.00", status="CONFIRMED")
        session = CashSession.objects.create(reference="DRAWER-ROLLBACK", cashier=self.actor,
                                             expected_cash=Decimal("20.00"), opening_float=Decimal("20.00"))
        with patch("apps.finance.services.payment_collection_service.CashMovement.objects.create", side_effect=RuntimeError("simulated drawer failure")):
            with self.assertRaises(RuntimeError):
                settle_outstanding_balance(booking=booking, actor=self.actor, amount="500.00", provider="CASH",
                    cash_session_reference=session.reference, idempotency_key="failure-key", include_unbilled=False)
        session.refresh_from_db()
        self.assertEqual(session.expected_cash, Decimal("20.00"))
        self.assertFalse(Payment.objects.filter(booking=booking).exists())
        self.assertFalse(GuestCollectionKey.objects.exists())
        self.assertFalse(FinancialTransaction.objects.exists())

    def test_later_room_occupancy_keeps_its_own_arrival_timestamp(self):
        from apps.stays.services import _ensure_stay_rooms
        rt = make_room_type("Later room arrival")
        first_room, later_room = make_room(rt, "981"), make_room(rt, "982")
        booking = make_booking(make_guest("later-room@example.com"), rt, [first_room, later_room],
                               number_of_rooms=2, status="CHECKED_IN")
        first_time = timezone.now() - timedelta(hours=5)
        later_time = timezone.now()
        assignment = booking.room_assignments.get(room=later_room)
        assignment.checked_in_at = later_time
        assignment.save(update_fields=["checked_in_at"])
        stay = Stay.objects.create(reference="STY-LATER-ROOM", booking=booking, guest=booking.guest,
                    status="IN_HOUSE", expected_arrival=booking.check_in, expected_departure=booking.check_out,
                    actual_check_in_at=first_time)
        _ensure_stay_rooms(stay=stay, assignments=[assignment], actor=self.actor, checked_in_at=first_time)
        room_history = StayRoom.objects.get(stay=stay, booking_room=assignment)
        self.assertEqual(room_history.actual_check_in_at, later_time)
        self.assertEqual(room_history.assigned_at, later_time)
        self.assertEqual(stay.events.get(type="ROOM_ASSIGNED").occurred_at, later_time)

    def test_multi_room_recognition_counts_only_selected_occupied_room(self):
        yesterday = self.today-timedelta(days=1)
        rt = make_room_type("Three room rebuild", price="100000.00")
        rooms = [make_room(rt, f"R-{i}") for i in range(3)]
        booking = make_booking(make_guest("rebuild-multi@example.com"), rt, rooms,
                  check_in=yesterday, check_out=self.today, number_of_rooms=3, total="300000.00", status="CHECKED_IN")
        assignments = list(booking.room_assignments.order_by("pk"))
        assignments[0].checked_in_at = timezone.now()-timedelta(days=1)
        assignments[0].save(update_fields=["checked_in_at"])
        stay = Stay.objects.create(reference="STY-REBUILD-MULTI", booking=booking, guest=booking.guest,
                    status="IN_HOUSE", expected_arrival=booking.check_in, expected_departure=booking.check_out)
        StayRoom.objects.create(stay=stay, room=rooms[0], booking_room=assignments[0], actual_check_in_at=assignments[0].checked_in_at)
        journal, created = post_accommodation_charge_for_night(stay=stay, service_date=yesterday, actor=self.actor)
        self.assertTrue(created)
        self.assertEqual(balance(accounting.ACCOUNTS_RECEIVABLE), Decimal("100000.00"))
        self.assertEqual(len(journal.metadata["occupied_rooms"]), 1)
        self.assertEqual(journal.metadata["occupied_rooms"][0]["room_number"], "R-0")
        self.assertFalse(post_accommodation_charge_for_night(stay=stay, service_date=yesterday, actor=self.actor)[1])
        self.assertEqual(sum(1 for a in assignments if a.checked_in_at), 1)


class RebuildSupplierTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("supplier-rebuild-manager@staff.dev", role=User.Role.MANAGER)
        self.accountant = make_staff("supplier-rebuild-accountant@staff.dev", role=User.Role.ACCOUNTANT)
        self.cashier = make_staff("supplier-rebuild-cashier@staff.dev", role=User.Role.CASHIER)
        expense, _ = create_expense(actor=self.cashier, category="Inventory goods", amount="200000.00",
                        accounting_class="INVENTORY", payment_method="PAYABLE", idempotency_key="rebuild-payable")
        expense = submit_expense(expense=expense, actor=self.cashier)
        expense = review_expense(expense=expense, reviewer=self.manager, approved=True)
        self.expense, _ = post_expense(expense=expense, actor=self.cashier)

    def test_partial_and_final_settlement_are_separately_idempotent(self):
        self.assertEqual(balance(accounting.INVENTORY_ASSET), Decimal("200000.00"))
        self.assertEqual(balance(accounting.BANK_CLEARING), Decimal("0.00"))
        first, created = settle_expense_payment(expense=self.expense, actor=self.manager, method="BANK_TRANSFER",
                         payment_reference="TRF-PART-1", amount="100000.00", receipt_url="https://supplier.example/receipt-1")
        self.assertTrue(created)
        self.assertIsNone(first.paid_at)
        self.assertEqual(payable_balance(first), Decimal("100000.00"))
        self.auth(self.manager)
        response = self.client.get(f"/api/admin/finance/expenses/{first.reference}/")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["paid_amount"], "100000.00")
        self.assertEqual(data["outstanding_amount"], "100000.00")
        self.assertEqual(data["payment_records"][0]["receipt_url"], "https://supplier.example/receipt-1")
        _same, created = settle_expense_payment(expense=first, actor=self.manager, method="BANK_TRANSFER",
                          payment_reference="TRF-PART-1", amount="100000.00")
        self.assertFalse(created)
        with self.assertRaises(ValidationError):
            settle_expense_payment(expense=first, actor=self.manager, method="BANK_TRANSFER",
                                   payment_reference="TRF-PART-1", amount="50000.00")
        with self.assertRaises(ValidationError):
            settle_expense_payment(expense=first, actor=self.manager, method="BANK_TRANSFER",
                                   payment_reference="TRF-OVER", amount="100000.01")
        final, created = settle_expense_payment(expense=first, actor=self.manager, method="BANK_TRANSFER",
                           payment_reference="TRF-PART-2", amount="100000.00")
        self.assertTrue(created)
        self.assertIsNotNone(final.paid_at)
        self.assertEqual(payable_balance(final), Decimal("0.00"))
        self.assertEqual(SupplierSettlement.objects.filter(expense=final).count(), 2)
        self.assertEqual(balance(accounting.ACCOUNTS_PAYABLE), Decimal("0.00"))
        self.assertEqual(balance(accounting.BANK_CLEARING), Decimal("-200000.00"))

    def test_partial_request_uses_existing_independent_approval(self):
        request, _ = propose_supplier_payment(expense=self.expense, actor=self.accountant,
                    method="BANK_TRANSFER", amount="75000.00", payment_reference="TRF-REQ-1", idempotency_key="request-one")
        request = submit_supplier_payment(request=request, actor=self.accountant)
        request = review_supplier_payment(request=request, reviewer=self.manager, approved=True)
        request, result = execute_supplier_payment(request=request, actor=self.accountant)
        self.assertEqual(request.status, "EXECUTED")
        self.assertIsNotNone(request.settlement_transaction_id)
        self.assertIsNone(result.paid_at)
        self.assertEqual(payable_balance(result), Decimal("125000.00"))
        again, result = execute_supplier_payment(request=request, actor=self.accountant)
        self.assertIsNone(result)
        self.assertEqual(SupplierSettlement.objects.count(), 1)
