"""Keep cashiers on operational POS/cash flows, not guest folio browsing."""
from django.db import migrations


def revoke_cashier_folio_view(apps, schema_editor):
    Capability = apps.get_model("accounts", "Capability")
    RoleCapability = apps.get_model("accounts", "RoleCapability")
    capability = Capability.objects.filter(code="folio.view").first()
    if capability:
        RoleCapability.objects.filter(role="CASHIER", capability=capability).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0005_finance_controls_capability")]
    operations = [migrations.RunPython(revoke_cashier_folio_view, migrations.RunPython.noop)]
