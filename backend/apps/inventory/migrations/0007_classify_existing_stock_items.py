"""Classify the stock items that the existing data already proves a use for.

Requirement 1 of the inventory review asks for sellable products, recipe
ingredients, operational consumables and reusable assets to be told apart.
``0006`` adds the ``item_type`` column with a ``CONSUMABLE`` default, which is
the safe value: every pre-existing row keeps exactly the behaviour it has
today, because only ``REUSABLE_ASSET`` changes an internal rule (a named
custodian) and nothing about deduction, valuation or availability depends on
the other two labels.

This migration therefore classifies **only** what the ledger and the recipe
book already prove, and never guesses from a name or a SKU:

* an item that is a BOM line of an active ``COMPONENTS`` recipe which is not a
  one-for-one single-line recipe is a **recipe ingredient** (rice, Maggi, oil);
* an item that is the single, one-for-one line of an active recipe is a
  **directly sellable product** — it is deducted exactly once per menu unit,
  which is what a bar bottle/water/pack does.

Items that appear in no active recipe are deliberately left at the default:
an operational consumable and a reusable asset are told apart by how the
hotel uses them (issued to a department, or lent out and returned), not by
their name, so ``manage.py inventory_configuration_audit`` lists the
candidate assets — with the exact advice — for an authorised user to confirm.

Nothing is deleted, no balance, movement, recipe, purchase, sale or receipt is
touched, and a row an operator has already classified is never overwritten.
"""
from collections import defaultdict
from decimal import Decimal

from django.db import migrations

INGREDIENT = "RECIPE_INGREDIENT"
SELLABLE = "SELLABLE_PRODUCT"
DEFAULT = "CONSUMABLE"
ONE = Decimal("1.0000")


def _classification(models):
    """Return ``({item_id: type}, rules)`` proved by the recipe book."""
    StockRecipeLine = models["inventory.stockrecipeline"]

    lines = (
        StockRecipeLine.objects.filter(
            recipe__is_active=True,
            # Literal: a historical model carries no nested TextChoices class.
            recipe__tracking_mode="COMPONENTS",
        )
        .values_list("recipe_id", "item_id", "quantity_per_menu_unit")
    )
    per_recipe = defaultdict(list)
    for recipe_id, item_id, quantity in lines:
        per_recipe[recipe_id].append((item_id, quantity))

    proposed = {}
    for recipe_id, component_lines in per_recipe.items():
        one_for_one = len(component_lines) == 1 and Decimal(component_lines[0][1]) == ONE
        for item_id, _quantity in component_lines:
            # An ingredient is proved by a multi-line BOM or a measured amount
            # (250 g of rice). A one-for-one single-line recipe is a product
            # sold exactly as it is stocked.
            proposed[item_id] = SELLABLE if one_for_one else INGREDIENT
    return proposed


def classify_stock_items(apps, schema_editor):
    models = apps.get_models()
    by_label = {model._meta.label_lower: model for model in models}
    StockItem = by_label["inventory.stockitem"]
    proposed = _classification(by_label)
    if not proposed:
        return
    items = StockItem.objects.filter(pk__in=list(proposed), item_type=DEFAULT)
    for item in items:
        item.item_type = proposed[item.pk]
        item.save(update_fields=["item_type"])


def revert_classification(apps, schema_editor):
    """Undo only the rows this migration set, and only while they still match."""
    models = apps.get_models()
    by_label = {model._meta.label_lower: model for model in models}
    StockItem = by_label["inventory.stockitem"]
    proposed = _classification(by_label)
    if not proposed:
        return
    for item_id, item_type in proposed.items():
        item = StockItem.objects.filter(pk=item_id, item_type=item_type).first()
        if item is not None:
            item.item_type = DEFAULT
            item.save(update_fields=["item_type"])


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0006_item_types_direct_sale_and_issue_departments"),
    ]

    operations = [
        migrations.RunPython(classify_stock_items, revert_classification),
    ]
