from django.db import migrations

# Stable codes already used by the existing ledger, plus empty configured
# accounts for future detailed classification. This creates NO transactions.
ACCOUNTS = [
    ("ASSET.CASH_ON_HAND", "Cash at hand"),
    ("ASSET.BANK_CLEARING", "Bank and transfer clearing"),
    ("ASSET.CARD_CLEARING", "POS settlement receivable"),
    ("ASSET.PAYSTACK_SETTLEMENT", "Paystack settlement receivable"),
    ("ASSET.GUEST_RECEIVABLE", "Guest receivables"),
    ("ASSET.SUPPLIER_ADVANCES", "Supplier advances"),
    ("ASSET.INVENTORY", "Inventory"),
    ("ASSET.OTHER_CURRENT", "Other current assets"),
    ("ASSET.FIXED", "Fixed assets"),
    ("LIABILITY.GUEST_DEPOSITS", "Guest advances"),
    ("LIABILITY.ACCOUNTS_PAYABLE", "Accounts payable"),
    ("LIABILITY.OPERATING_OBLIGATIONS", "Unpaid operating obligations"),
    ("LIABILITY.OTHER_CURRENT", "Other current liabilities"),
    ("LIABILITY.PAYROLL_PAYABLE", "Payroll payable"),
    ("LIABILITY.PAYROLL_DEDUCTIONS", "Payroll deductions payable"),
    ("LIABILITY.PAYROLL_TAX", "Payroll tax payable (historical)"),
    ("LIABILITY.PAYROLL_PENSION", "Payroll pension payable (historical)"),
    ("LIABILITY.TAX_PAYABLE", "Historical customer tax payable"),
    ("LIABILITY.STATUTORY_TAX_PAYABLE", "Hotel statutory tax payable"),
    ("LIABILITY.CASH_FLOAT", "Cash float"),
    ("EQUITY.OWNER_CAPITAL", "Owner capital"),
    ("EQUITY.OWNER_DRAWINGS", "Owner drawings"),
    ("EQUITY.OPENING_BALANCE", "Evidence-based opening balance offset"),
    ("EQUITY.RETAINED_EARNINGS", "Retained earnings"),
    ("EQUITY.CURRENT_EARNINGS", "Current period earnings"),
    ("REVENUE.ACCOMMODATION", "Room revenue"),
    ("REVENUE.FOOD_BEVERAGE", "Food and beverage revenue (legacy shared account)"),
    ("REVENUE.RESTAURANT", "Restaurant revenue"),
    ("REVENUE.BAR", "Bar revenue"),
    ("REVENUE.ROOM_SERVICE", "Room service revenue"),
    ("REVENUE.OTHER_OPERATING", "Other operating revenue"),
    ("REVENUE.SERVICE_FEES", "Service fee revenue"),
    ("CONTRA_REVENUE.DISCOUNTS_ALLOWED", "Discounts allowed"),
    ("CONTRA_REVENUE.REFUNDS_ISSUED", "Refunds issued"),
    ("EXPENSE.OPERATING", "Operating expense"),
    ("EXPENSE.COST_OF_GOODS", "Cost of goods sold"),
    ("EXPENSE.FOOD_BEVERAGE", "Food and beverage cost"),
    ("EXPENSE.ELECTRICITY", "Electricity"),
    ("EXPENSE.WATER", "Water"),
    ("EXPENSE.INTERNET", "Internet"),
    ("EXPENSE.MAINTENANCE", "Maintenance"),
    ("EXPENSE.PAYROLL", "Staff expense"),
    ("EXPENSE.PAYROLL_PENSION", "Payroll pension expense (historical)"),
    ("EXPENSE.OTHER", "Other expense"),
    ("EXPENSE.CASH_OVER_SHORT", "Cash over or short"),
    ("EXPENSE.STATUTORY_TAX_ABSORBED", "Hotel statutory tax expense"),
]


def seed(apps, schema_editor):
    account = apps.get_model("finance", "ChartAccount")
    db = schema_editor.connection.alias
    for code, label in ACCOUNTS:
        account.objects.using(db).get_or_create(
            code=code,
            defaults={"label": label, "kind": code.split(".", 1)[0],
                      "purpose": code, "is_active": True, "is_system": True},
        )


class Migration(migrations.Migration):
    dependencies = [("finance", "0019_accounting_rebuild_foundations")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
