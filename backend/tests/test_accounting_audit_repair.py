"""Accounting audit repair — end-to-end regression matrix.

These tests are written against an isolated, disposable Django test database.
Nothing here touches production data, and no historical posting is deleted or
rewritten: every correction is expressed as a ledger-derived calculation or an
append-only reversal journal.

Covered:

* the six mandatory guest charge/payment/receivable cases, including the
  NGN 85,000 charge / NGN 100,000 payment case (NGN 15,000 guest credit, never
  a negative receivable);
* separate classifications (unpaid, partially paid, fully paid, guest credit,
  refunded), with a credit never labelled REFUNDED and a refund requiring a
  posted REFUND transaction;
* the same figures through the DB, service, HTTP API, summary and balance sheet;
* the four mandatory supplier invoice/payment/outstanding cases, approval
  without cash, duplicate-payment prevention, partials and reversals;
* draft payables are documents, not posted liabilities;
* date filtering, multiple folios/services, zero/large amounts and history.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.core.utils import hotel_today
from apps.finance.models import (
    Expense,
    FinancialLine,
    FinancialTransaction,
    Folio,
    FolioPosting,
    SupplierSettlement,
)
from apps.finance.services import accounting
from apps.finance.services.accommodation_service import post_previous_night_accommodation_batch
from apps.finance.services.expense_service import (
    create_expense,
    post_expense,
    review_expense,
    settle_expense_payment,
    submit_expense,
)
from apps.finance.services.guest_receivables_service import (
    CLASS_FULLY_PAID,
    CLASS_GUEST_CREDIT,
    CLASS_PARTIALLY_PAID,
    CLASS_REFUNDED,
    CLASS_UNPAID_BILL,
    guest_position,
    guest_receivables,
)
from apps.finance.services.ledger_service import create_posted_transaction
from apps.finance.services.payment_visibility import (
    approved_unpaid_procurement_visibility,
    unposted_procurement_payment_visibility,
)
from apps.finance.services.procurement_payable_service import (
    posted_expense_paid_amount,
    purchase_order_payable_position,
    purchase_order_payable_summary,
    purchase_order_payables_index,
    raise_payable_from_purchase_order,
)
from apps.finance.services.reporting_service import financial_summary
from apps.finance.services.statements_service import balance_sheet
from apps.finance.services.supplier_payment_service import (
    payable_balance,
    reverse_supplier_settlement,
)
from apps.inventory.models import PurchaseOrder, PurchaseOrderPayment, StockItem, StockLocation, Supplier
from apps.inventory.services.inventory_service import (
    approve_purchase_order,
    create_purchase_order,
    receive_goods,
    submit_purchase_order,
)

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room_type, make_staff


def D(value):
    return Decimal(str(value)).quantize(Decimal("0.01"))


class GuestBalanceMatrixTests(BaseAPITestCase):
    """Transaction -> DB -> service -> API -> statement, one case at a time."""

    def setUp(self):
        super().setUp()
        self.today = hotel_today()
        self.actor = make_staff("audit-actor@staff.dev", role=User.Role.ADMIN)
        self.viewer = make_staff("audit-viewer@staff.dev", role=User.Role.ACCOUNTANT)
        self.room_type = make_room_type("Audit room", price="20000.00")
        self.counter = 0

    def _folio(self, label, *, booking_status=Booking.Status.CONFIRMED, closed=False):
        self.counter += 1
        guest = make_guest(f"audit-guest-{label}-{self.counter}@guest.dev")
        booking = make_booking(
            guest, self.room_type,
            check_in=self.today - timedelta(days=2), check_out=self.today + timedelta(days=1),
            total="0.00", status=booking_status,
        )
        return Folio.objects.create(
            reference=f"FOL-AUDIT-{label}-{self.counter}", guest=guest, booking=booking,
            currency="NGN", status=Folio.Status.CLOSED if closed else Folio.Status.OPEN,
        )

    def _post(self, folio, amount, source, *, kind="CHARGE", transaction_type=None,
              business_date=None, source_key=None):
        amount = D(amount)
        is_charge = kind == "CHARGE"
        transaction_type = transaction_type or (
            FinancialTransaction.Type.POS_CHARGE if is_charge else FinancialTransaction.Type.PAYMENT_ALLOCATION
        )
        ar_direction = "DEBIT" if is_charge else "CREDIT"
        other_account = accounting.OTHER_OPERATING_REVENUE if is_charge else accounting.GUEST_DEPOSITS
        other_direction = "CREDIT" if is_charge else "DEBIT"
        self.counter += 1
        key = source_key or f"audit-{source}-{self.counter}"
        return create_posted_transaction(
            transaction_type=transaction_type, actor=self.actor, source_key=key,
            idempotency_key=key, business_date=business_date or self.today, currency="NGN",
            lines=[
                {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": ar_direction,
                 "amount": amount, "folio": folio},
                {"account_code": other_account, "direction": other_direction, "amount": amount},
            ],
            postings=[{"line_index": 0, "folio": folio,
                       "kind": FolioPosting.Kind.CHARGE if is_charge else FolioPosting.Kind.PAYMENT,
                       "effect": FolioPosting.Effect.DEBIT if is_charge else FolioPosting.Effect.CREDIT,
                       "amount": amount}],
        )[0]

    def _case(self, charges, payments, *, label="case"):
        folio = self._folio(label)
        if D(charges) > 0:
            self._post(folio, charges, f"{label}-charge")
        if D(payments) > 0:
            self._post(folio, payments, f"{label}-payment", kind="PAYMENT")
        return folio

    def _api_ar(self):
        self.auth(self.viewer)
        response = self.client.get("/api/admin/finance/reports/ar-aging/", {"as_of": self.today.isoformat()})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]

    def _api_summary(self):
        self.auth(self.viewer)
        response = self.client.get("/api/admin/finance/reports/summary/", {
            "start": (self.today - timedelta(days=365)).isoformat(),
            "end": self.today.isoformat(), "currency": "NGN",
        })
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]

    def assert_case(self, charges, payments, receivable, credit="0.00", classification=None, label="case"):
        folio = self._case(charges, payments, label=label)
        report = guest_receivables(as_of=self.today, include_settled=True)
        position = guest_position(as_of=self.today)
        api = self._api_ar()
        summary = self._api_summary()
        expected = str(D(receivable))
        expected_credit = str(D(credit))
        # DB: posted lines are unchanged and balanced.
        self.assertEqual(
            FinancialLine.objects.filter(folio=folio, account_code=accounting.ACCOUNTS_RECEIVABLE).count(),
            int(D(charges) > 0) + int(D(payments) > 0),
        )
        # Service.
        self.assertEqual(report["gross_receivables"], expected)
        self.assertEqual(report["guest_credit_balances"], expected_credit)
        self.assertGreaterEqual(D(report["gross_receivables"]), D("0.00"))
        self.assertEqual(position["gross_guest_receivables"], expected)
        self.assertEqual(position["guest_credit_balances"], expected_credit)
        self.assertEqual(position["guest_advances"], str(D(credit) + max(D(position["unapplied_guest_deposits"]), D("0.00"))))
        # API and the Accounting-page summary block agree with the service.
        self.assertEqual(api["total_outstanding"], expected)
        self.assertEqual(api["guest_balances"]["gross_guest_receivables"], expected)
        self.assertEqual(api["guest_balances"]["guest_credit_balances"], expected_credit)
        self.assertEqual(summary["guest_balances"]["gross_guest_receivables"], expected)
        self.assertEqual(summary["guest_balances"]["guest_credit_balances"], expected_credit)
        # Balance sheet keeps the signed control balance for audit but presents
        # guest credit as a liability; it still foots.
        sheet = balance_sheet(as_of=self.today)
        self.assertTrue(sheet["presented_balanced"], sheet["presented_difference"])
        self.assertEqual(sheet["guest_credit_liability"], sheet["guest_position"]["guest_credit_balances"]
                         if D(sheet["guest_credit_liability"]) > 0 else sheet["guest_credit_liability"])
        labels = [row["label"] for row in sheet["presented_liabilities"]]
        if D(credit) > 0 or D(sheet["guest_credit_liability"]) > 0:
            self.assertIn("Guest credit / advances", labels)
        if classification:
            service_items = guest_receivables(as_of=self.today, include_settled=True)["items"]
            row = next(item for item in service_items if item["folio_id"] == folio.pk)
            self.assertEqual(row["balance_classification"], classification)
            self.assertEqual(position["classification"][classification]["folios"], 1)
        return folio

    # ------------------------------------------------ mandatory guest matrix
    def test_70000_charge_no_payment_is_70000_receivable(self):
        self.assert_case("70000", "0", "70000", classification=CLASS_UNPAID_BILL, label="m1")

    def test_70000_charge_70000_payment_is_zero_receivable(self):
        self.assert_case("70000", "70000", "0", classification=CLASS_FULLY_PAID, label="m2")

    def test_100000_charge_60000_payment_is_40000_receivable(self):
        self.assert_case("100000", "60000", "40000", classification=CLASS_PARTIALLY_PAID, label="m3")

    def test_85000_charge_100000_payment_is_zero_receivable_and_15000_credit(self):
        folio = self.assert_case(
            "85000", "100000", "0", credit="15000",
            classification=CLASS_GUEST_CREDIT, label="m4",
        )
        position = guest_position(as_of=self.today)
        self.assertEqual(position["guest_advances"], "15000.00")
        self.assertEqual(position["classification"][CLASS_GUEST_CREDIT]["amount"], "15000.00")
        self.assertEqual(position["classification"][CLASS_REFUNDED]["folios"], 0)
        self.assertEqual(position["refunded_total"], "0.00")
        # Legacy settlement_state stays compatible; the NEW classification is correct.
        api = self._api_ar()
        self.assertIn("settlement_state", api["items"][0])
        item = next(x for x in guest_receivables(as_of=self.today, include_settled=True)["items"]
                    if x["folio_id"] == folio.pk)
        self.assertEqual(item["balance_classification"], CLASS_GUEST_CREDIT)
        self.assertEqual(item["credit_balance"], "15000.00")
        self.assertEqual(item["outstanding"], "0.00")

    def test_90000_charge_70000_payment_is_20000_receivable(self):
        self.assert_case("90000", "70000", "20000", classification=CLASS_PARTIALLY_PAID, label="m5")

    def test_120000_charge_100000_payment_is_20000_receivable(self):
        self.assert_case("120000", "100000", "20000", classification=CLASS_PARTIALLY_PAID, label="m6")

    # ------------------------------------------------------ refunds / status
    def test_a_posted_refund_is_refunded_but_an_advance_is_not(self):
        folio = self._folio("refund")
        self._post(folio, "50000", "refund-charge")
        self._post(folio, "50000", "refund-payment", kind="PAYMENT")
        # Money actually returned: Dr AR / Cr cash.
        self._post(folio, "10000", "refund-return", kind="CHARGE",
                   transaction_type=FinancialTransaction.Type.REFUND)
        position = guest_position(as_of=self.today)
        self.assertEqual(position["classification"][CLASS_REFUNDED]["folios"], 1)
        self.assertEqual(position["refunded_total"], "10000.00")
        self.assertEqual(position["classification"][CLASS_GUEST_CREDIT]["folios"], 0)
        self.assertEqual(FinancialTransaction.objects.filter(
            type=FinancialTransaction.Type.REFUND, status=FinancialTransaction.Status.POSTED).count(), 1)

    def test_credit_balance_is_never_labelled_refunded_without_a_refund_posting(self):
        self.assert_case("5000", "9000", "0", credit="4000",
                         classification=CLASS_GUEST_CREDIT, label="credit")
        self.assertEqual(guest_position(as_of=self.today)["refunded_total"], "0.00")
        self.assertEqual(self._api_ar()["refunded_total"], "0.00")

    def test_cancelled_booking_and_closed_folio_do_not_pollute_active_receivables(self):
        # A cancelled booking with a charge is excluded from active receivables.
        folio = self._folio("cancelled", booking_status=Booking.Status.CANCELLED)
        self._post(folio, "25000", "cancelled-charge")
        position = guest_position(as_of=self.today)
        self.assertEqual(position["classification"]["CANCELLED"]["folios"], 1)
        self.assertEqual(position["gross_guest_receivables"], "0.00")
        # A closed folio still reports its own classification; it is not deleted.
        closed = self._folio("closed", closed=True)
        self._post(closed, "12000", "closed-charge")
        self._post(closed, "12000", "closed-payment", kind="PAYMENT")
        self.assertEqual(guest_position(as_of=self.today)["classification"][CLASS_FULLY_PAID]["folios"], 1)

    # ------------------------------------------- duplication / dates / edges
    def test_multiple_rooms_and_services_under_one_booking_are_not_double_counted(self):
        guest = make_guest("audit-multi@guest.dev")
        booking = make_booking(
            guest, self.room_type, check_in=self.today - timedelta(days=2),
            check_out=self.today + timedelta(days=1), total="0.00",
        )
        first = Folio.objects.create(reference="FOL-AUDIT-MULTI-1", guest=guest, booking=booking, currency="NGN")
        second = Folio.objects.create(reference="FOL-AUDIT-MULTI-2", guest=guest, booking=booking, currency="NGN")
        self._post(first, "30000", "multi-room")
        self._post(first, "7500", "multi-restaurant")
        self._post(second, "7500", "multi-room-service")
        self._post(second, "10000", "multi-payment", kind="PAYMENT")
        report = guest_receivables(as_of=self.today)
        # 30,000 + 7,500 on folio 1, 7,500 - 10,000 on folio 2: the 2,500
        # credit is never netted against another folio's receivable.
        self.assertEqual(report["gross_receivables"], "37500.00")
        self.assertEqual(report["guest_credit_balances"], "2500.00")
        self.assertEqual(guest_position(as_of=self.today)["gross_guest_receivables"], "37500.00")
        self.assertEqual(guest_position(as_of=self.today)["guest_credit_balances"], "2500.00")
        self.assertEqual(self._api_ar()["total_outstanding"], "37500.00")

    def test_booked_but_not_checked_in_room_posts_no_accommodation_charge(self):
        guest = make_guest("audit-not-checked-in@guest.dev")
        make_booking(guest, self.room_type, check_in=self.today - timedelta(days=1),
                     check_out=self.today + timedelta(days=1), total="120000.00",
                     status=Booking.Status.CONFIRMED)
        created, _ = post_previous_night_accommodation_batch(service_date=self.today)
        self.assertEqual(created, 0)
        self.assertEqual(FinancialTransaction.objects.filter(
            type=FinancialTransaction.Type.ACCOMMODATION_CHARGE).count(), 0)
        self.assertEqual(guest_receivables(as_of=self.today)["gross_receivables"], "0.00")

    def test_balances_are_as_of_the_selected_date(self):
        folio = self._folio("dated")
        self._post(folio, "60000", "dated-charge", business_date=self.today - timedelta(days=10))
        self._post(folio, "60000", "dated-payment", kind="PAYMENT", business_date=self.today + timedelta(days=3))
        before = guest_receivables(as_of=self.today, include_settled=True)
        self.assertEqual(before["gross_receivables"], "60000.00")
        after = guest_receivables(as_of=self.today + timedelta(days=3), include_settled=True)
        self.assertEqual(after["gross_receivables"], "0.00")

    def test_zero_and_large_values_and_multiple_partials(self):
        zero = self._folio("zero")
        self.assertEqual(guest_position(as_of=self.today)["gross_guest_receivables"], "0.00")
        large = self._folio("large")
        self._post(large, "999999999.99", "large-charge")
        for portion in ("200000000.00", "300000000.00", "199999999.99"):
            self._post(large, portion, f"large-{portion}", kind="PAYMENT")
        self.assertEqual(guest_receivables(as_of=self.today)["gross_receivables"], "300000000.00")
        self.assertEqual(SupplierSettlement.objects.count(), 0)  # guest payments are not supplier settlements
        self.assertIn(zero.reference, [f.reference for f in Folio.objects.all()])

    def test_missing_folio_and_api_error_paths_do_not_invent_balances(self):
        self.auth(self.viewer)
        bad = self.client.get("/api/admin/finance/reports/ar-aging/", {"as_of": "not-a-date"})
        self.assertEqual(bad.status_code, 400, bad.content)
        self.assertEqual(self._api_ar()["total_outstanding"], "0.00")
        self.assertEqual(self._api_summary()["guest_balances"]["gross_guest_receivables"], "0.00")


class SupplierBalanceMatrixTests(BaseAPITestCase):
    """Posted invoices, real payments, approvals, partials and reversals."""

    def setUp(self):
        super().setUp()
        self.today = hotel_today()
        self.maker = make_staff("audit-maker@staff.dev", role=User.Role.ACCOUNTANT)
        self.approver = make_staff("audit-approver@staff.dev", role=User.Role.ADMIN)
        self.second_approver = make_staff("audit-approver-2@staff.dev", role=User.Role.ADMIN)
        self.viewer = make_staff("audit-sup-viewer@staff.dev", role=User.Role.ACCOUNTANT)
        self.counter = 0

    def _expense(self, amount, *, payment_method=Expense.PaymentMethod.PAYABLE, posted=True):
        self.counter += 1
        key = f"audit-expense-{self.counter}"
        expense, _ = create_expense(
            actor=self.maker, category="Inventory purchase", amount=D(amount),
            incurred_on=self.today, payment_method=payment_method,
            accounting_class=Expense.AccountingClass.INVENTORY,
            payee="Audit Supplier Ltd", idempotency_key=key,
        )
        if not posted:
            # Approval alone is not a posting and not cash.
            submit_expense(expense=expense, actor=self.maker)
            review_expense(expense=expense, reviewer=self.approver, approved=True)
            expense.refresh_from_db()
            return expense
        submit_expense(expense=expense, actor=self.maker)
        review_expense(expense=expense, reviewer=self.approver, approved=True)
        post_expense(expense=expense, actor=self.maker)
        expense.refresh_from_db()
        return expense

    def _pay(self, expense, amount, *, suffix="1"):
        settle_expense_payment(
            expense=expense, actor=self.approver, method=Expense.PaymentMethod.BANK_TRANSFER,
            payment_reference=f"AUDIT-TRF-{expense.reference}-{suffix}",
            amount=D(amount), idempotency_key=f"audit-settle-{expense.pk}-{suffix}",
        )
        expense.refresh_from_db()

    def _summary(self, start=None, end=None):
        start = start or (self.today - timedelta(days=365))
        end = end or self.today
        return financial_summary(start_date=start, end_date=end, currency="NGN")

    def _api_summary(self):
        self.auth(self.viewer)
        response = self.client.get("/api/admin/finance/reports/summary/", {
            "start": (self.today - timedelta(days=365)).isoformat(),
            "end": self.today.isoformat(), "currency": "NGN",
        })
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]

    def _api_payables(self, reference=None):
        self.auth(self.viewer)
        params = {"references": reference} if reference else {}
        response = self.client.get("/api/admin/finance/procurement/payables/", params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]

    def assert_outstanding(self, expense, expected):
        expected = str(D(expected))
        self.assertEqual(str(payable_balance(expense)), expected)
        self.assertEqual(str(posted_expense_paid_amount(expense)), str(D(expense.amount) - D(expected)))
        payments = self._summary()["supplier_payments"]
        self.assertEqual(payments["outstanding_supplier_payables"], expected)
        block = self._api_summary()["supplier_balances"]
        self.assertEqual(block["outstanding_payables"], expected)
        self.assertEqual(block["actual_supplier_payments"], payments["supplier_payments_made"])

    # ------------------------------------------- mandatory supplier matrix
    def test_150000_invoice_no_payment_is_150000_outstanding(self):
        expense = self._expense("150000")
        self.assert_outstanding(expense, "150000")
        self.assertEqual(self._summary()["supplier_payments"]["supplier_payments_made"], "0.00")

    def test_150000_invoice_90000_payment_is_60000_outstanding(self):
        expense = self._expense("150000")
        self._pay(expense, "90000")
        self.assert_outstanding(expense, "60000")
        self.assertEqual(self._summary()["supplier_payments"]["supplier_payments_made"], "90000.00")

    def test_150000_invoice_150000_payment_is_zero_outstanding(self):
        expense = self._expense("150000")
        self._pay(expense, "150000")
        self.assert_outstanding(expense, "0")
        self.assertEqual(self._summary()["supplier_payments"]["supplier_payments_made"], "150000.00")

    def test_approved_but_unpaid_200000_posts_no_cash_and_no_paid_expense(self):
        # An APPROVED expense document with no posting: approval is not payment.
        expense = self._expense("200000", posted=False)
        self.assertEqual(expense.status, Expense.Status.APPROVED)
        summary = self._summary()
        self.assertEqual(summary["supplier_payments"]["supplier_invoices_recognized"], "0.00")
        self.assertEqual(summary["supplier_payments"]["supplier_payments_made"], "0.00")
        self.assertEqual(summary["supplier_payments"]["outstanding_supplier_payables"], "0.00")
        self.assertEqual(str(posted_expense_paid_amount(expense)), "0.00")
        # A separate approved procurement commitment is visible but not cash.
        supplier = Supplier.objects.create(reference="AUDIT-SUP", name="Audit Supplier")
        location = StockLocation.objects.create(code="AUDIT-STORE", name="Audit store")
        item = StockItem.objects.create(sku="AUDIT-ITEM", name="Audit item", base_unit="piece")
        po, _ = create_purchase_order(
            supplier=supplier, delivery_location=location, requested_by=self.approver,
            lines=[{"item_id": item.pk, "quantity_ordered": "1", "unit_cost": "200000.00"}],
        )
        submit_purchase_order(purchase_order=po, actor=self.approver)
        approve_purchase_order(purchase_order=po, actor=self.second_approver)
        visibility = approved_unpaid_procurement_visibility(end_date=self.today, currency="NGN")
        self.assertGreaterEqual(visibility["orders"], 1)
        self.assertGreaterEqual(D(visibility["amount"]), D("200000.00"))
        self.assertFalse(visibility["posted_liability"])
        self.assertEqual(self._summary()["supplier_payments"]["supplier_payments_made"], "0.00")

    # ---------------------------------------- partials, duplicates, reversal
    def test_multiple_partials_and_duplicate_payment_prevention(self):
        expense = self._expense("150000")
        self._pay(expense, "40000", suffix="a")
        self._pay(expense, "50000", suffix="b")
        self.assert_outstanding(expense, "60000")
        settlements = SupplierSettlement.objects.filter(expense=expense).count()
        self.assertEqual(settlements, 2)
        # Replaying the same idempotency key must not create a third payment.
        settle_expense_payment(
            expense=expense, actor=self.approver, method=Expense.PaymentMethod.BANK_TRANSFER,
            payment_reference=f"AUDIT-TRF-{expense.reference}-b", amount=D("50000"),
            idempotency_key=f"audit-settle-{expense.pk}-b",
        )
        self.assertEqual(SupplierSettlement.objects.filter(expense=expense).count(), settlements)
        self.assert_outstanding(expense, "60000")
        with self.assertRaises(ValidationError):
            settle_expense_payment(
                expense=expense, actor=self.approver, method=Expense.PaymentMethod.BANK_TRANSFER,
                payment_reference=f"AUDIT-TRF-{expense.reference}-b", amount=D("90000"),
                idempotency_key=f"audit-settle-{expense.pk}-b",
            )

    def test_reversed_settlement_reopens_payable_and_removes_it_from_paid_cash(self):
        expense = self._expense("150000")
        self._pay(expense, "150000")
        self.assert_outstanding(expense, "0")
        settlement = SupplierSettlement.objects.get(expense=expense)
        original = settlement.transaction
        result = reverse_supplier_settlement(
            reference=original.reference, actor=self.second_approver,
            reason="Bank transfer recalled by the supplier",
        )
        self.assertTrue(result["created"])
        original.refresh_from_db()
        self.assertEqual(original.status, FinancialTransaction.Status.POSTED)  # history preserved
        self.assertTrue(FinancialTransaction.objects.filter(
            reversal_of=original, type=FinancialTransaction.Type.VOID,
            status=FinancialTransaction.Status.POSTED).exists())
        self.assert_outstanding(expense, "150000")
        self.assertEqual(self._summary()["supplier_payments"]["supplier_payments_made"], "0.00")
        # Idempotent replay: no second VOID journal.
        again = reverse_supplier_settlement(
            reference=original.reference, actor=self.second_approver,
            reason="Bank transfer recalled by the supplier",
        )
        self.assertFalse(again["created"])
        self.assertEqual(FinancialTransaction.objects.filter(reversal_of=original).count(), 1)

    def test_api_reversal_route_rejects_anonymous_and_missing_records(self):
        expense = self._expense("150000")
        self._pay(expense, "150000")
        settlement = SupplierSettlement.objects.get(expense=expense)
        self.client.force_authenticate(user=None)
        anon = self.client.post(
            f"/api/admin/finance/supplier-settlements/{settlement.transaction.reference}/reverse/",
            {"reason": "Bank recalled"}, format="json",
        )
        self.assertIn(anon.status_code, (401, 403), anon.content)
        self.auth(self.second_approver)
        missing = self.client.post(
            "/api/admin/finance/supplier-settlements/FIN-DOES-NOT-EXIST/reverse/",
            {"reason": "Bank recalled"}, format="json",
        )
        self.assertEqual(missing.status_code, 404, missing.content)

    # ------------------------------------------- draft payables / PO position
    def _purchase_order(self, quantity="1", unit_cost="62400.00"):
        supplier = Supplier.objects.create(reference=f"AUDIT-SUP-{self.counter}", name="Audit PO Supplier")
        location = StockLocation.objects.create(code=f"AUDIT-LOC-{self.counter}", name="Audit location")
        item = StockItem.objects.create(sku=f"AUDIT-SKU-{self.counter}", name="Audit stock item", base_unit="piece")
        self.counter += 1
        po, _ = create_purchase_order(
            supplier=supplier, delivery_location=location, requested_by=self.maker,
            lines=[{"item_id": item.pk, "quantity_ordered": quantity, "unit_cost": unit_cost}],
        )
        submit_purchase_order(purchase_order=po, actor=self.maker)
        approve_purchase_order(purchase_order=po, actor=self.approver)
        po.refresh_from_db()
        line = po.lines.get()
        receive_goods(
            purchase_order=po, actor=self.approver, source_key=f"audit-receipt-{self.counter}",
            receipt_lines=[{"purchase_order_line_id": line.pk, "quantity_received": quantity}],
        )
        po.refresh_from_db()
        return po

    def test_draft_payable_is_a_document_not_a_posted_liability(self):
        po = self._purchase_order()
        expense, created = raise_payable_from_purchase_order(
            purchase_order=po, actor=self.maker, idempotency_key=f"audit-payable-{po.pk}",
        )
        self.assertTrue(created)
        self.assertEqual(expense.status, Expense.Status.DRAFT)
        position = purchase_order_payable_summary(po)
        self.assertEqual(position["payable_raised"], "62400.00")  # legacy document total retained
        self.assertEqual(position["posted_payable_liability"], "0.00")
        self.assertEqual(position["unposted_payable_documents"], "62400.00")
        self.assertEqual(position["payable_outstanding"], "0.00")
        self.assertEqual(position["actual_supplier_payments"], "0.00")
        summary = self._summary()
        self.assertEqual(summary["supplier_payments"]["supplier_invoices_recognized"], "0.00")
        self.assertEqual(summary["supplier_payments"]["outstanding_supplier_payables"], "0.00")
        api = self._api_payables(po.reference)
        self.assertEqual(api["purchase_orders"][po.reference]["payable_outstanding"], "0.00")
        self.assertIn("position", api)
        self.assertEqual(api["position"]["posted_liability"], "0.00")
        self.assertEqual(purchase_order_payable_position(
            purchase_order_payables_index(references=[po.reference]), as_of=self.today,
        )["posted_liability"], "0.00")

    def test_unposted_procurement_exception_is_scoped_to_the_selected_period(self):
        po = self._purchase_order()
        PurchaseOrderPayment.objects.create(
            purchase_order=po, amount=D("62400.00"), method=PurchaseOrderPayment.PaymentMethod.BANK_TRANSFER,
            reference="AUDIT-PO-PAYMENT-2024", paid_on=date(2024, 5, 1),
            accounting_posting_status=PurchaseOrderPayment.AccountingPostingStatus.NOT_POSTED,
        )
        in_2024 = self._summary(start=date(2024, 1, 1), end=date(2024, 12, 31))
        in_2025 = self._summary(start=date(2025, 1, 1), end=date(2025, 12, 31))
        self.assertEqual(in_2024["supplier_payments"]["unposted_procurement_payments"]["count"], 1)
        self.assertEqual(in_2025["supplier_payments"]["unposted_procurement_payments"]["count"], 0)
        self.assertEqual(
            unposted_procurement_payment_visibility(
                currency="NGN", start_date=date(2025, 1, 1), end_date=date(2025, 12, 31),
            )["count"], 0,
        )
        self.assertFalse(in_2024["supplier_payments"]["unposted_procurement_payments"]["added_to_posted_totals"])

    def test_supplier_invoice_status_counts_unpaid_partial_and_paid(self):
        unpaid = self._expense("150000")
        partial = self._expense("150000")
        self._pay(partial, "90000")
        paid = self._expense("150000")
        self._pay(paid, "150000")
        po = self._purchase_order()
        api = self._api_payables(po.reference)["position"]["invoice_status"]
        self.assertEqual(api["UNPAID"]["invoices"], 1)
        self.assertEqual(api["PARTIALLY_PAID"]["invoices"], 1)
        self.assertEqual(api["PAID"]["invoices"], 1)
        self.assertEqual(api["PARTIALLY_PAID"]["outstanding_amount"], "60000.00")
        self.assertEqual(unpaid.status, Expense.Status.POSTED)
