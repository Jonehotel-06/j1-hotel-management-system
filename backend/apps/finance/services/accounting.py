"""Stable chart-of-account identifiers used by finance posting services.

The strings are durable integration contracts rather than a user-editable chart.
A configurable chart mapping can be introduced later without changing historic
ledger evidence.
"""

CASH_ON_HAND = "ASSET.CASH_ON_HAND"
BANK_CLEARING = "ASSET.BANK_CLEARING"
CARD_CLEARING = "ASSET.CARD_CLEARING"
ACCOUNTS_RECEIVABLE = "ASSET.GUEST_RECEIVABLE"
INVENTORY_ASSET = "ASSET.INVENTORY"
GUEST_DEPOSITS = "LIABILITY.GUEST_DEPOSITS"
ACCOUNTS_PAYABLE = "LIABILITY.ACCOUNTS_PAYABLE"
ACCOMMODATION_REVENUE = "REVENUE.ACCOMMODATION"
FOOD_BEVERAGE_REVENUE = "REVENUE.FOOD_BEVERAGE"
OTHER_OPERATING_REVENUE = "REVENUE.OTHER_OPERATING"
DISCOUNTS_ALLOWED = "CONTRA_REVENUE.DISCOUNTS_ALLOWED"
REFUNDS_ISSUED = "CONTRA_REVENUE.REFUNDS_ISSUED"
OPERATING_EXPENSE = "EXPENSE.OPERATING"
PAYROLL_EXPENSE = "EXPENSE.PAYROLL"
PAYROLL_PENSION_EXPENSE = "EXPENSE.PAYROLL_PENSION"
PAYROLL_PAYABLE = "LIABILITY.PAYROLL_PAYABLE"
PAYROLL_DEDUCTIONS_PAYABLE = "LIABILITY.PAYROLL_DEDUCTIONS"
PAYROLL_TAX_PAYABLE = "LIABILITY.PAYROLL_TAX"
PAYROLL_PENSION_PAYABLE = "LIABILITY.PAYROLL_PENSION"
CASH_OVER_SHORT = "EXPENSE.CASH_OVER_SHORT"
# LIABILITY.TAX_PAYABLE holds HISTORICAL customer-billed tax collected before
# the customer-billing tax removal. New customer transactions never post here;
# the account is preserved so historical receipts, folios and statutory records
# keep their original financial breakdown. See apps.finance.services.tax_policy.
TAX_PAYABLE = "LIABILITY.TAX_PAYABLE"
# Statutory tax that the hotel may owe but NEVER bills to the guest: it is
# accrued against an absorbing expense account so customer totals, receipts,
# folios and checkout balances stay tax-free while the potential remittance
# liability remains visible for the owner's accountant. Both accounts are only
# used when the statutory-tax policy switch is enabled in HotelSettings.
STATUTORY_TAX_ABSORBED = "EXPENSE.STATUTORY_TAX_ABSORBED"
STATUTORY_TAX_PAYABLE = "LIABILITY.STATUTORY_TAX_PAYABLE"
SERVICE_FEE_REVENUE = "REVENUE.SERVICE_FEES"
CASH_FLOAT = "LIABILITY.CASH_FLOAT"
