# tests/test_critical_repair_folio_correction.py
"""Folio balance correction repair (2026-11-26).

Reported fault: there was no way to edit or delete a balance added to a guest
folio — from the application or from Django Admin — and no admin screen that
could find a folio by guest/booking or show its balance.

Nothing here deletes or rewrites posted financial history. A posted entry is
corrected by an approved offsetting VOID journal (the same architecture the
ledger already uses for manual journals), and only DRAFT transactions can be
edited/deleted in the admin.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib import admin as django_admin

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.core.utils import hotel_today
from apps.finance.admin import FinancialTransactionAdmin, FolioAdmin, FolioPostingAdmin
from apps.finance.models import (
    FinancialLine, FinancialTransaction, Folio, FolioPosting,
)
from apps.finance.services import accounting
from apps.finance.services.folio_service import folio_balance_queryset
from apps.finance.services.ledger_service import create_posted_transaction
from apps.stays.models import Stay

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room_type, make_staff


class FolioCorrectionTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.today = hotel_today()
        self.desk = make_staff("folio-fix-desk@staff.dev", role=User.Role.RECEPTIONIST)
        self.manager = make_staff("folio-fix-manager@staff.dev", role=User.Role.MANAGER)
        room_type = make_room_type("Folio fix suite", price="20000.00")
        booking = make_booking(
            make_guest("folio-fix@example.com"), room_type,
            check_in=self.today - timedelta(days=1), check_out=self.today + timedelta(days=1),
            total="40000.00", status="CHECKED_IN",
        )
        self.stay = Stay.objects.create(
            reference=f"STY-{booking.booking_reference}", booking=booking, guest=booking.guest,
            status=Stay.Status.IN_HOUSE, expected_arrival=booking.check_in,
            expected_departure=booking.check_out,
        )
        self.folio = Folio.objects.create(
            reference="FOL-FIX-0001", stay=self.stay, booking=booking, guest=booking.guest,
            status=Folio.Status.OPEN, currency="NGN", opened_by=self.desk,
        )

    # ------------------------------------------------------------- helpers

    def post_charge(self, amount="2500.00", *, kind=FolioPosting.Kind.CHARGE,
                    effect=FolioPosting.Effect.DEBIT, reference="FOL-FIX-CHG-1", actor=None):
        return create_posted_transaction(
            transaction_type=FinancialTransaction.Type.OTHER_CHARGE,
            source_key=reference, idempotency_key=reference, actor=actor or self.desk,
            source_reference=self.folio.reference, narrative="Miscellaneous charge",
            lines=[
                {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": FinancialLine.Direction.DEBIT,
                 "amount": amount, "description": "Misc charge", "folio": self.folio},
                {"account_code": accounting.OTHER_OPERATING_REVENUE, "direction": FinancialLine.Direction.CREDIT,
                 "amount": amount, "description": "Misc charge"},
            ],
            postings=[{
                "line_index": 0, "folio": self.folio, "kind": kind, "effect": effect,
                "amount": amount, "description": "Misc charge", "source_reference": reference,
            }],
        )[0]

    def balance(self):
        row = folio_balance_queryset(Folio.objects.filter(pk=self.folio.pk)).first()
        return Decimal(row.balance)

    def void_url(self, posting):
        return f"/api/admin/finance/folios/{self.folio.reference}/postings/{posting.pk}/void/"

    def posting(self):
        return FolioPosting.objects.get(folio=self.folio, reversal_of__isnull=True)

    # --------------------------------------------------------------- tests

    def test_manager_voids_a_posted_charge_and_the_balance_corrects(self):
        charge = self.post_charge("2500.00")
        self.assertEqual(self.balance(), Decimal("2500.00"))
        self.auth(self.manager)
        response = self.client.post(
            self.void_url(self.posting()), {"reason": "Charged to the wrong guest room"}, format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        # The guest no longer owes the mistyped charge.
        self.assertEqual(self.balance(), Decimal("0.00"))
        # Nothing was deleted: both the original and its reversal exist.
        self.assertTrue(FinancialTransaction.objects.filter(pk=charge.pk).exists())
        reversal = FinancialTransaction.objects.get(source_key=f"folio-void:{self.posting().pk}")
        charge.refresh_from_db()
        self.assertEqual(charge.status, FinancialTransaction.Status.POSTED)
        self.assertEqual(reversal.reversal_of_id, charge.pk)
        self.assertEqual(reversal.status, FinancialTransaction.Status.POSTED)
        # Statement evidence: the reversal points at the entry it reverses.
        self.assertTrue(FolioPosting.objects.filter(reversal_of__isnull=False).exists())
        self.assertTrue(AuditLog.objects.filter(action="FOLIO_POSTING_VOIDED").exists())
        # The journal is balanced, so the ledger still reconciles.
        debits = sum(line.amount for line in reversal.lines.filter(direction=FinancialLine.Direction.DEBIT))
        credits = sum(line.amount for line in reversal.lines.filter(direction=FinancialLine.Direction.CREDIT))
        self.assertEqual(debits, credits)

    def test_a_reason_is_mandatory(self):
        self.post_charge()
        self.auth(self.manager)
        response = self.client.post(self.void_url(self.posting()), {}, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(self.balance(), Decimal("2500.00"))
        self.assertFalse(FinancialTransaction.objects.filter(type=FinancialTransaction.Type.VOID).exists())

    def test_unauthorized_staff_cannot_void(self):
        self.post_charge()
        self.auth(self.desk)   # holds payment.refund.request, not .approve
        response = self.client.post(
            self.void_url(self.posting()), {"reason": "Trying to remove the charge"}, format="json",
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(self.balance(), Decimal("2500.00"))

    def test_voiding_is_idempotent(self):
        self.post_charge()
        self.auth(self.manager)
        first = self.client.post(self.void_url(self.posting()), {"reason": "Duplicate test correction"}, format="json")
        second = self.client.post(self.void_url(self.posting()), {"reason": "Duplicate test correction"}, format="json")
        self.assertIn(first.status_code, (200, 201))
        self.assertEqual(second.status_code, 200, second.content)
        self.assertFalse(second.json()["data"]["created"])
        self.assertEqual(
            FinancialTransaction.objects.filter(type=FinancialTransaction.Type.VOID).count(), 1,
        )
        self.assertEqual(self.balance(), Decimal("0.00"))

    def test_a_payment_posting_cannot_be_voided_with_this_workflow(self):
        payment = create_posted_transaction(
            transaction_type=FinancialTransaction.Type.PAYMENT_ALLOCATION,
            source_key="FOL-FIX-PAY-1", idempotency_key="FOL-FIX-PAY-1", actor=self.desk,
            source_reference=self.folio.reference, narrative="Cash payment",
            lines=[
                {"account_code": accounting.CASH_ON_HAND, "direction": FinancialLine.Direction.DEBIT,
                 "amount": "1000.00", "description": "Cash"},
                {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": FinancialLine.Direction.CREDIT,
                 "amount": "1000.00", "description": "Cash", "folio": self.folio},
            ],
            postings=[{
                "line_index": 1, "folio": self.folio, "kind": FolioPosting.Kind.PAYMENT,
                "effect": FolioPosting.Effect.CREDIT, "amount": "1000.00", "description": "Cash payment",
            }],
        )[0]
        posting = FolioPosting.objects.get(transaction=payment)
        self.auth(self.manager)
        response = self.client.post(
            self.void_url(posting), {"reason": "Trying to void a payment instead"}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)

    def test_a_closed_folio_needs_an_authorised_correction(self):
        from django.core.exceptions import ValidationError

        from apps.finance.services.folio_service import void_folio_posting

        self.post_charge()
        self.folio.status = Folio.Status.CLOSED
        self.folio.save(update_fields=["status", "updated_at"])
        # Without the closed-folio override the service refuses, and says why.
        with self.assertRaises(ValidationError) as caught:
            void_folio_posting(
                posting=self.posting(), actor=self.desk, reason="Correction after close",
            )
        self.assertIn("reopen", str(caught.exception).lower())
        self.assertEqual(self.balance(), Decimal("2500.00"))
        # A desk user cannot reach the sensitive endpoint at all.
        self.auth(self.desk)
        self.assertEqual(
            self.client.post(
                self.void_url(self.posting()), {"reason": "Correction after close"}, format="json",
            ).status_code,
            403,
        )
        # An authorised manager performs the recorded correction.
        self.auth(self.manager)
        allowed = self.client.post(
            self.void_url(self.posting()), {"reason": "Authorised correction after close"}, format="json",
        )
        self.assertEqual(allowed.status_code, 201, allowed.content)
        self.assertEqual(self.balance(), Decimal("0.00"))

    # ----------------------------------------------------------- admin tests

    def test_admin_exposes_guest_booking_and_balances(self):
        self.assertIn("balance", FolioAdmin.list_display)
        self.assertIn("guest__full_name", FolioAdmin.search_fields)
        self.assertIn("booking__booking_reference", FolioAdmin.search_fields)
        self.assertIn("folio__guest__full_name", FolioPostingAdmin.search_fields)
        self.assertIn("void_selected_entries", FolioPostingAdmin.actions)
        # Read-only rows stay read-only: the action posts a reversal instead.
        model_admin = FolioPostingAdmin(FolioPosting, django_admin.site)
        self.assertFalse(model_admin.has_change_permission(None))
        self.assertFalse(model_admin.has_delete_permission(None))

    def test_admin_only_allows_changing_draft_transactions(self):
        posted = self.post_charge()
        draft = FinancialTransaction.objects.create(
            reference="FIN-FIX-DRAFT", type=FinancialTransaction.Type.OTHER_CHARGE,
            status=FinancialTransaction.Status.DRAFT, currency="NGN",
        )
        model_admin = FinancialTransactionAdmin(FinancialTransaction, django_admin.site)
        self.assertTrue(model_admin.has_change_permission(None, draft))
        self.assertTrue(model_admin.has_delete_permission(None, draft))
        self.assertFalse(model_admin.has_change_permission(None, posted))
        self.assertFalse(model_admin.has_delete_permission(None, posted))
