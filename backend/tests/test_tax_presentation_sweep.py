"""A1 — the tax-presentation sweep: nothing presents tax, history is intact.

Customer-billing tax is gone (`tax_policy.customer_tax_amount()` is hard-zero)
and the statutory tax accounts were withdrawn from presentation in batch 01.
The residue the audit named is the missing TESTS: "no tax rows/keys anywhere in
accounting summary payloads", with historical tax rows preserved. These tests
sweep the whole accounting surface the page and its exports actually send:

* the ``financial_summary`` payload — no tax-named key anywhere in the tree,
  no tax-named row in any presented list, and any tax-named reconciliation
  row must be explicitly marked ``presented: False`` (the withdrawal marker);
* the balance sheet — tax accounts are folded into neutral aggregates, the
  sections still foot and the sheet still balances;
* the xlsx print export — no cell names tax;
* the ledger — every historical tax posting survives the sweep untouched.
"""
from datetime import timedelta
from decimal import Decimal

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import FinancialLine, FinancialTransaction
from apps.finance.services import accounting
from apps.finance.services.ledger_service import create_posted_transaction
from apps.finance.services.reporting_service import financial_summary, summary_xlsx_bytes
from apps.finance.services.statements_service import balance_sheet

from .base import BaseAPITestCase
from .factories import make_staff


def _walk_keys(node, path="$"):
    """Yield (path, key) for every dict key in a JSON-ish tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield path, str(key)
            yield from _walk_keys(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_keys(value, f"{path}[{index}]")


def _rows(node, key):
    value = node.get(key) if isinstance(node, dict) else None
    return value if isinstance(value, list) else []


def _all_row_lists(node):
    """Yield every list-of-dict rows list anywhere in a payload tree."""
    if isinstance(node, dict):
        for value in node.values():
            yield from _all_row_lists(value)
    elif isinstance(node, list):
        if node and isinstance(node[0], dict) and ("label" in node[0] or "account_code" in node[0]):
            yield node
        else:
            for value in node:
                yield from _all_row_lists(value)


def _all_labels(node):
    for rows in _all_row_lists(node):
        for row in rows:
            yield str(row.get("label") or "")


class TaxPresentationSweepTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.today = hotel_today()
        self.actor = make_staff("sweep.actor@staff.dev", role=User.Role.ACCOUNTANT)
        # Historical money that DID carry tax, exactly as the ledger kept it:
        # an accommodation charge with a collected-tax line, and a POS charge
        # with one, both before the customer-billing tax removal.
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.ACCOMMODATION_CHARGE, actor=self.actor,
            source_key="sweep:historic-room-tax", business_date=self.today - timedelta(days=2),
            lines=[
                {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": "DEBIT", "amount": Decimal("107500.00")},
                {"account_code": accounting.ACCOMMODATION_REVENUE, "direction": "CREDIT", "amount": Decimal("100000.00")},
                {"account_code": accounting.TAX_PAYABLE, "direction": "CREDIT", "amount": Decimal("7500.00")},
            ],
        )
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.POS_CHARGE, actor=self.actor,
            source_key="sweep:historic-pos-tax", business_date=self.today - timedelta(days=1),
            source_reference="POS-HIST-1",
            lines=[
                {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": "DEBIT", "amount": Decimal("21500.00")},
                {"account_code": accounting.FOOD_BEVERAGE_REVENUE, "direction": "CREDIT", "amount": Decimal("20000.00")},
                {"account_code": accounting.TAX_PAYABLE, "direction": "CREDIT", "amount": Decimal("1500.00")},
            ],
        )
        # And a statutory absorbed-tax expense posting, the other withdrawn
        # presentation account.
        create_posted_transaction(
            transaction_type=FinancialTransaction.Type.EXPENSE, actor=self.actor,
            source_key="sweep:statutory-absorbed", business_date=self.today - timedelta(days=1),
            lines=[
                {"account_code": accounting.STATUTORY_TAX_ABSORBED, "direction": "DEBIT", "amount": Decimal("3000.00")},
                {"account_code": accounting.BANK_CLEARING, "direction": "CREDIT", "amount": Decimal("3000.00")},
            ],
        )

    def _summary(self):
        return financial_summary(
            start_date=self.today - timedelta(days=3), end_date=self.today,
        )

    def test_no_accounting_summary_payload_key_is_tax(self):
        summary = self._summary()
        sheet = balance_sheet(as_of=self.today)
        for payload, name in ((summary, "financial_summary"), (sheet, "balance_sheet")):
            for path, key in _walk_keys(payload):
                self.assertNotIn(
                    "tax", key.lower(),
                    f"{name} payload key {path}.{key} presents tax",
                )

    def test_no_presented_row_names_tax(self):
        for payload, name in ((self._summary(), "financial_summary"), (balance_sheet(as_of=self.today), "balance_sheet")):
            for rows in _all_row_lists(payload):
                for row in rows:
                    label = str(row.get("label") or "")
                    if "tax" in label.lower():
                        # The only tax-naming row allowed anywhere is the
                        # explicit withdrawal marker used as reconciliation
                        # evidence — never a presented row.
                        self.assertFalse(
                            row.get("presented", True),
                            f"{name} presents a tax row: {label!r}",
                        )
                        self.assertNotIn(
                            accounting.TAX_PAYABLE, str(row.get("account_code") or ""),
                            f"{name} names the tax account in a row",
                        )

    def test_the_fold_keeps_the_summaries_footed(self):
        summary = self._summary()
        sheet = balance_sheet(as_of=self.today)
        self.assertTrue(sheet["balanced"], sheet)
        self.assertEqual(
            sum(Decimal(row["amount"]) for row in sheet["liabilities"]),
            Decimal(sheet["total_liabilities"]),
        )
        # The historical tax money is still COUNTED — inside neutral folds —
        # so the totals cannot silently drop it and the statements still foot.
        labels = list(_all_labels(summary)) + list(_all_labels(sheet))
        self.assertIn("Other liabilities", labels)
        self.assertIn("Other expenses", labels)

    def test_the_print_export_names_no_tax(self):
        payload = summary_xlsx_bytes(self._summary())
        self.assertTrue(payload)
        import io

        from openpyxl import load_workbook

        workbook = load_workbook(io.BytesIO(payload), read_only=True)
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows(values_only=True):
                for cell in row:
                    if isinstance(cell, str):
                        self.assertNotIn(
                            "tax", cell.lower(),
                            f"print export sheet {sheet.title!r} names tax: {cell!r}",
                        )

    def test_every_historical_tax_posting_survives(self):
        self.assertEqual(
            FinancialLine.objects.filter(account_code=accounting.TAX_PAYABLE).count(), 2,
            "the two historical collected-tax lines are untouched",
        )
        self.assertEqual(
            FinancialLine.objects.filter(account_code=accounting.STATUTORY_TAX_ABSORBED).count(), 1,
            "the statutory absorbed-tax posting is untouched",
        )
