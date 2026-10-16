# apps/inventory/management/commands/inventory_configuration_audit.py
"""Read-only audit of how every stock item is meant to leave the store.

Answers the three questions an operator has to answer before the stock ledger
can be trusted:

1. **Which products deduct themselves through POS, and how?** Either a real
   stock recipe or an explicit direct-sale configuration (POS menu item +
   store + units per menu unit). A SKU or name that merely resembles a menu
   item deducts nothing — those items are listed here as candidates to
   configure, because a sale of them currently parks a reviewable hand-off
   instead of inventing a recipe.
2. **Which consumables and reusable assets leave through an internal issue?**
   Reusable assets must name the person taking custody; the report shows any
   asset issue that has no recipient and how much of each asset is still out.
3. **Are there records that look like duplicates?** Names that repeat are
   listed side by side with their per-location balances, movement counts and
   receipt history. Nothing is merged, renamed or deleted: two items can be
   genuinely different products, and only a human who has compared the
   balances and histories can decide.

Nothing in this command writes, resets, merges or fabricates a stock figure.

    python manage.py inventory_configuration_audit
    python manage.py inventory_configuration_audit --json
    python manage.py inventory_configuration_audit --limit 200
"""
import json
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db.models import Count, Q, Sum

from apps.inventory.models import StockBalance, StockItem, StockMovement, StockRecipe, StockRecipeLine
from apps.pos.models import MenuItem


class Command(BaseCommand):
    help = "Report how stock items are configured to leave the store (read-only)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=100,
            help="Maximum rows printed per section (default 100).",
        )
        parser.add_argument("--json", action="store_true", help="Emit the report as JSON.")

    # ------------------------------------------------------------------ helpers
    def _balances(self, item_ids):
        rows = defaultdict(list)
        for row in (
            StockBalance.objects.filter(item_id__in=item_ids)
            .select_related("location")
            .order_by("item_id", "location__code")
        ):
            rows[row.item_id].append({
                "location": row.location.code,
                "quantity": str(row.quantity_on_hand),
                "last_movement_at": row.last_movement_at.isoformat() if row.last_movement_at else "",
            })
        return rows

    def _history(self, item_ids):
        rows = defaultdict(lambda: {"movements": 0, "last_movement_at": "", "received_units": "0.0000"})
        for row in (
            StockMovement.objects.filter(item_id__in=item_ids)
            .values("item_id")
            .annotate(
                movements=Count("pk"),
                received=Sum("quantity_delta", filter=Q(type=StockMovement.Type.RECEIPT)),
            )
        ):
            rows[row["item_id"]]["movements"] = row["movements"]
            rows[row["item_id"]]["received_units"] = str(row["received"] or "0.0000")
        for row in (
            StockMovement.objects.filter(item_id__in=item_ids, occurred_at__isnull=False)
            .order_by("item_id", "-occurred_at")
            .values("item_id", "occurred_at")
        ):
            bucket = rows[row["item_id"]]
            if not bucket["last_movement_at"]:
                bucket["last_movement_at"] = row["occurred_at"].isoformat()
        return rows

    def handle(self, *args, **options):
        limit = max(1, options["limit"])
        report = {}

        # ------------------------------------------------ 1. recipe-based items
        recipe_lines = (
            StockRecipeLine.objects.select_related("recipe", "recipe__menu_item", "recipe__location", "item")
            .order_by("recipe__menu_item__name", "item__sku")
        )
        recipes_by_item = defaultdict(list)
        for line in recipe_lines:
            recipes_by_item[line.item_id].append({
                "recipe": line.recipe.reference,
                "menu_item": line.recipe.menu_item.sku,
                "menu_item_name": line.recipe.menu_item.name,
                "location": line.recipe.location.code,
                "active": line.recipe.is_active,
                "quantity_per_menu_unit": str(line.quantity_per_menu_unit),
                "entered": f"{line.entered_quantity or ''} {line.entered_unit or ''}".strip(),
            })

        auto_recipes = []
        for recipe in (
            StockRecipe.objects.filter(notes__startswith="Auto-linked 1:1")
            .select_related("menu_item", "location")
            .prefetch_related("lines__item")
            .order_by("menu_item__name")[:limit]
        ):
            auto_recipes.append({
                "recipe": recipe.reference,
                "menu_item": recipe.menu_item.sku,
                "menu_item_name": recipe.menu_item.name,
                "location": recipe.location.code,
                "active": recipe.is_active,
                "components": [
                    f"{line.item.sku} × {line.quantity_per_menu_unit} {line.item.base_unit}" for line in recipe.lines.all()
                ],
                "advice": (
                    "This recipe keeps deducting stock; it was created automatically from a SKU/name match. "
                    "Confirm it is correct, or configure the stock item's direct sale (menu item + store + units) "
                    "and retire this recipe deliberately."
                ),
            })

        configured = []
        for item in (
            StockItem.objects.filter(direct_sale_menu_item__isnull=False)
            .select_related("direct_sale_menu_item", "direct_sale_location")
            .order_by("sku")[:limit]
        ):
            configured.append({
                "sku": item.sku,
                "name": item.name,
                "item_type": item.item_type,
                "menu_item": item.direct_sale_menu_item.sku,
                "menu_item_name": item.direct_sale_menu_item.name,
                "location": item.direct_sale_location.code if item.direct_sale_location else "",
                "units_per_menu_unit": str(item.direct_sale_quantity),
                "complete": item.is_direct_sale_configured,
            })

        # Candidate menu-item matches for items that deduct nothing today.
        active_recipe_item_ids = {
            line.item_id for line in recipe_lines if line.recipe.is_active
        }
        unconsumed = list(
            StockItem.objects.filter(is_active=True, direct_sale_menu_item__isnull=True)
            .order_by("sku")
        )
        menu_rows = list(MenuItem.objects.filter(is_active=True).only("pk", "sku", "name"))
        by_sku = {str(row.sku or "").strip().lower(): row for row in menu_rows}
        by_name = {str(row.name or "").strip().lower(): row for row in menu_rows}
        candidates = []
        for item in unconsumed:
            if item.pk in active_recipe_item_ids:
                continue
            menu = by_sku.get(str(item.sku or "").strip().lower()) or by_name.get(str(item.name or "").strip().lower())
            if menu is None:
                continue
            candidates.append({
                "sku": item.sku,
                "name": item.name,
                "item_type": item.item_type,
                "looks_like_menu_item": menu.sku,
                "menu_item_name": menu.name,
                "matched_on": "sku" if by_sku.get(str(item.sku or "").strip().lower()) is menu else "name",
                "advice": (
                    "No recipe and no direct-sale configuration: a sale of this menu item parks a hand-off "
                    "for review instead of deducting stock. If the product is sold exactly as stocked, PATCH "
                    f"/api/admin/inventory/items/{item.pk}/ with direct_sale_menu_item, direct_sale_location and "
                    "direct_sale_quantity; otherwise write a stock recipe."
                ),
            })
            if len(candidates) >= limit:
                break
        report["menu_item_matches_without_configuration"] = candidates

        # ------------------------------------------- 2. issues / reusable assets
        asset_items = list(StockItem.objects.filter(item_type=StockItem.ItemType.REUSABLE_ASSET).order_by("sku"))
        asset_ids = [item.pk for item in asset_items]
        asset_balances = self._balances(asset_ids) if asset_ids else {}
        asset_history = self._history(asset_ids) if asset_ids else {}
        issued = defaultdict(lambda: {"issued": 0, "issued_without_recipient": 0, "returned": 0})
        if asset_ids:
            for row in (
                StockMovement.objects.filter(
                    item_id__in=asset_ids,
                    type__in=[StockMovement.Type.ISSUE, StockMovement.Type.MAINTENANCE_ISSUE],
                )
                .values("item_id", "recipient")
                .annotate(units=Sum("quantity_delta"), rows=Count("pk"))
            ):
                bucket = issued[row["item_id"]]
                bucket["issued"] += row["rows"]
                if not (row["recipient"] or "").strip():
                    bucket["issued_without_recipient"] += row["rows"]
            for row in (
                StockMovement.objects.filter(item_id__in=asset_ids, type=StockMovement.Type.MAINTENANCE_RETURN)
                .values("item_id")
                .annotate(rows=Count("pk"))
            ):
                issued[row["item_id"]]["returned"] += row["rows"]
        reusable_assets = []
        for item in asset_items[:limit]:
            reusable_assets.append({
                "sku": item.sku,
                "name": item.name,
                "on_hand": asset_balances.get(item.pk, []),
                "out_with_departments": issued[item.pk]["issued"],
                "issues_without_a_named_recipient": issued[item.pk]["issued_without_recipient"],
                "return_movements": issued[item.pk]["returned"],
                **asset_history.get(item.pk, {"movements": 0, "received_units": "0.0000", "last_movement_at": ""}),
            })
        report["reusable_assets"] = reusable_assets

        internal_issues = []
        for row in (
            StockMovement.objects.filter(
                Q(type=StockMovement.Type.ISSUE) | Q(type=StockMovement.Type.MAINTENANCE_ISSUE)
            )
            .select_related("item", "location", "actor")
            .order_by("-occurred_at")[:limit]
        ):
            internal_issues.append({
                "reference": row.reference,
                "item": row.item.sku,
                "location": row.location.code,
                "quantity_delta": str(row.quantity_delta),
                "department": row.department,
                "recipient": row.recipient,
                "issued_by": row.actor.email if row.actor_id else "",
                "occurred_at": row.occurred_at.isoformat() if row.occurred_at else "",
                "reason": row.notes,
                "source_reference": row.source_reference,
            })
        report["recent_internal_issues"] = internal_issues

        # ------------------------------------------------- 3. duplicate warning
        name_buckets = defaultdict(list)
        for item in StockItem.objects.all().only("pk", "sku", "name", "item_type", "is_active", "created_at"):
            name_buckets[str(item.name or "").strip().lower()].append(item)
        duplicate_candidates = []
        for _key, items in sorted(name_buckets.items()):
            if len(items) < 2:
                continue
            item_ids = [item.pk for item in items]
            balances = self._balances(item_ids)
            history = self._history(item_ids)
            duplicate_candidates.append({
                "name": items[0].name,
                "note": "Candidate only — nothing is merged. Compare balances, movements and receipts first.",
                "items": [
                    {
                        "sku": item.sku,
                        "id": item.pk,
                        "item_type": item.item_type,
                        "is_active": item.is_active,
                        "balances": balances.get(item.pk, []),
                        "movements": history.get(item.pk, {}).get("movements", 0),
                        "received_units": history.get(item.pk, {}).get("received_units", "0.0000"),
                        "last_movement_at": history.get(item.pk, {}).get("last_movement_at", ""),
                    }
                    for item in items
                ],
            })
            if len(duplicate_candidates) >= limit:
                break
        report["possible_duplicate_records"] = duplicate_candidates

        type_counts = {
            row["item_type"]: row["total"]
            for row in StockItem.objects.values("item_type").annotate(total=Count("pk"))
        }
        report["item_type_summary"] = type_counts
        report["auto_created_recipes"] = auto_recipes
        report["configured_direct_sales"] = configured
        report["recipes_by_item"] = {
            str(item_id): rows for item_id, rows in list(recipes_by_item.items())[:limit]
        }

        if options["json"]:
            self.stdout.write(json.dumps(report, indent=2, default=str))
            return

        w = self.stdout.write
        w(self.style.MIGRATE_HEADING("Inventory configuration audit (read-only)"))
        w(f"Items by type: {json.dumps(type_counts)}")
        w("")

        w(self.style.MIGRATE_HEADING("1. Direct-sale configuration (products sold through POS)"))
        if configured:
            for row in configured:
                state = "complete" if row["complete"] else "INCOMPLETE — no store, no deduction"
                w(f"  {row['sku']:<18} {row['name']:<32} → {row['menu_item']} @ {row['location'] or '—'} "
                  f"× {row['units_per_menu_unit']}  [{state}]")
        else:
            w("  None configured yet.")
        if candidates:
            w(self.style.WARNING(f"  {len(candidates)} item(s) look like an active menu item but deduct nothing:"))
            for row in candidates:
                w(f"    {row['sku']:<18} {row['name']:<32} matches {row['looks_like_menu_item']} "
                  f"(by {row['matched_on']})")
        w("")

        w(self.style.MIGRATE_HEADING("2. Recipes"))
        w(f"  {len(recipes_by_item)} stock item(s) are consumed by a recipe.")
        if auto_recipes:
            w(self.style.WARNING(f"  {len(auto_recipes)} auto-created recipe(s) still deduct stock:"))
            for row in auto_recipes:
                w(f"    {row['menu_item']} ({row['menu_item_name']}) @ {row['location']} "
                  f"[{'active' if row['active'] else 'retired'}] ← {', '.join(row['components'])}")
            w("    " + auto_recipes[0]["advice"])
        else:
            w("  No auto-created recipes remain.")
        w("")

        w(self.style.MIGRATE_HEADING("3. Internal issues (consumables and reusable assets)"))
        if internal_issues:
            for row in internal_issues:
                w(f"  {row['occurred_at'][:19]}  {row['item']:<16} {row['quantity_delta']:>12} "
                  f"@{row['location']:<12} dept={row['department'] or '—':<14} recipient={row['recipient'] or '—'}")
        else:
            w("  No internal issues recorded.")
        if reusable_assets:
            w("")
            w("  Reusable assets (custody must be traceable):")
            for row in reusable_assets:
                on_hand = ", ".join(f"{b['location']}={b['quantity']}" for b in row["on_hand"]) or "none"
                flag = "  ⚠ issues without a named recipient" if row["issues_without_a_named_recipient"] else ""
                w(f"    {row['sku']:<18} {row['name']:<28} on hand: {on_hand:<28} "
                  f"issues={row['out_with_departments']} returns={row['return_movements']}{flag}")
        w("")

        w(self.style.MIGRATE_HEADING("4. Possible duplicate records (verify before acting)"))
        if not duplicate_candidates:
            w("  No two active items share a name.")
        for bucket in duplicate_candidates:
            w(f"  “{bucket['name']}” appears {len(bucket['items'])} times — {bucket['note']}")
            for item in bucket["items"]:
                on_hand = ", ".join(f"{b['location']}={b['quantity']}" for b in item["balances"]) or "no balance"
                w(f"    #{item['id']} {item['sku']:<16} type={item['item_type']:<17} active={item['is_active']} "
                  f"movements={item['movements']:<5} received={item['received_units']:<10} {on_hand}")
        w("")
        w(self.style.SUCCESS("Read-only report: nothing was changed, merged, reset or deleted."))
