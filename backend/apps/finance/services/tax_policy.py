"""No tax is appended to new guest invoices.

Legacy booking/POS tax snapshots and immutable journal entries remain intact;
this module no longer calculates hotel statutory-tax accruals. Payroll
obligations are managed separately in payroll accounting.
"""
from decimal import Decimal

CUSTOMER_BILLED_TAX = Decimal("0.00")


def customer_tax_amount() -> Decimal:
    """New customer-facing charges never add a tax line."""
    return CUSTOMER_BILLED_TAX
