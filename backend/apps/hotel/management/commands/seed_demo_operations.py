# apps/hotel/management/commands/seed_demo_operations.py
"""Seed the ISOLATED demo/operations dataset (menu, staff, suppliers, stock).

Scope (owner brief §5.2, §7–§11):
* Nigerian food, beverage and menu catalogue on the existing POS menu models.
* Synthetic DEMO employees for every operational department.
* DEMO suppliers, stock items (ingredients, beverages, consumables, equipment),
  and clearly-marked opening balances recorded as immutable OPENING movements.
* Extends the seeded demo room inventory to six physical rooms per demo room
  type (the repository's approved demo catalogue: Standard / Deluxe / Executive
  Suite at their existing approved demo rates — no price is invented or changed).

SAFETY
* Refuses to run against production settings. Demo seeding happens only in an
  isolated development/test environment — never in place of a staging database.
* Idempotent: every record is keyed (SKU / employee code / reference / source
  key) and re-runs report what already exists without duplicating.
* Every synthetic record carries an explicit DEMO marker in its notes/
  description so demo rows can never be mistaken for production data.
* Production stock balances and movements are never truncated, reset or edited.

The 24-room baseline (4 room types x 6 rooms) from the owner brief could NOT be
confirmed: the repository's approved demo catalogue has 3 room types and the
live database currently publishes 0. The 4th room type and its approved rate
are an OWNER DECISION; this command does not invent either.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.conf import settings as django_settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.core.formatting import format_money
from apps.inventory.models import (
    StockItem, StockLocation, StockMovement, Supplier,
)
from apps.inventory.models import StockRecipe, StockRecipeLine
from apps.inventory.services.inventory_service import record_stock_movement
from apps.pos.models import MenuCategory, MenuItem
from apps.rooms.models import Room, RoomType
from apps.staff_operations.models import ShiftAssignment, ShiftTemplate, StaffProfile

DEMO = "DEMO"
DEMO_EMAIL_DOMAIN = "demo.j-one.test"


# --------------------------------------------------------------------------- #
# Room inventory — six physical rooms per existing demo room type.
# Room NUMBERS only; the approved demo rates on the room types are untouched.
# --------------------------------------------------------------------------- #
ROOM_INVENTORY_PLAN = {
    "Standard Room": [(f"10{i}", 1) for i in range(1, 7)],          # 101–106
    "Deluxe Room": [(f"20{i}", 2) for i in range(1, 7)],            # 201–206
    "Executive Suite": [(f"30{i}", 3) for i in range(1, 7)],        # 301–306
}


# --------------------------------------------------------------------------- #
# Nigerian menu catalogue. Prices are DEMO VALUES — flagged in the description
# and confined to the isolated environment. Replace via the staff POS menu
# tools with owner-approved prices before any live service.
# --------------------------------------------------------------------------- #
MENU_CATALOGUE = [
    ("Rice Dishes", "RESTAURANT", "kitchen", [
        ("Jollof Rice & Chicken", "DEMO-M-JOL-CHK", "2500.00", "Smoky party jollof rice served with grilled chicken."),
        ("Jollof Rice & Goat Meat", "DEMO-M-JOL-GOT", "3000.00", "Party jollof rice with assorted goat meat."),
        ("Fried Rice & Turkey", "DEMO-M-FRD-TUR", "3200.00", "Nigerian fried rice with fried turkey."),
        ("White Rice & Stew (Beef)", "DEMO-M-WHT-BEF", "2200.00", "White rice with tomato stew and beef."),
        ("Ofada Rice & Ayamase", "DEMO-M-OFD-AYA", "3000.00", "Ofada rice served with ayamase sauce."),
        ("Coconut Rice & Fish", "DEMO-M-COC-FSH", "2800.00", "Coconut rice with fried fish."),
        ("Beans, Plantain & Stew", "DEMO-M-BNS-PLT", "1800.00", "Ewa agoyin with fried plantain."),
        ("Yam Porridge (Asaro)", "DEMO-M-YAM-ASR", "1700.00", "Yam porridge with palm oil sauce."),
        ("Moi Moi (2 pieces)", "DEMO-M-MOI-02", "800.00", "Steamed bean pudding."),
        ("Akara (4 pieces)", "DEMO-M-AKR-04", "700.00", "Bean fritters."),
        ("Indomie Noodles & Egg", "DEMO-M-NDL-EGG", "1500.00", "Stir-fried noodles with egg."),
        ("Spaghetti & Meat Sauce", "DEMO-M-SPG-MTS", "2000.00", "Spaghetti in rich meat sauce."),
    ]),
    ("Soups & Swallow", "RESTAURANT", "kitchen", [
        ("Egusi Soup & Pounded Yam", "DEMO-M-EGU-PND", "2800.00", "Egusi soup with pounded yam."),
        ("Ogbono Soup & Eba", "DEMO-M-OGB-EBA", "2600.00", "Ogbono soup with eba."),
        ("Oha Soup & Fufu", "DEMO-M-OHA-FUF", "2800.00", "Oha soup with fufu."),
        ("Nsala Soup & Semovita", "DEMO-M-NSL-SEM", "2800.00", "Nsala (white soup) with semovita."),
        ("Okra Soup & Wheat", "DEMO-M-OKR-WHT", "2400.00", "Okra soup with wheat swallow."),
        ("Bitterleaf Soup & Garri", "DEMO-M-BTL-GAR", "2600.00", "Bitterleaf soup with eba."),
        ("Afang Soup & Fufu", "DEMO-M-AFG-FUF", "3000.00", "Afang soup with fufu."),
        ("Edikang Ikong & Pounded Yam", "DEMO-M-EDI-PND", "3200.00", "Edikang ikong vegetable soup."),
        ("Efo Riro & Amala", "DEMO-M-EFO-AML", "2800.00", "Efo riro with amala."),
        ("Pepper Soup (Goat)", "DEMO-M-PPR-GOT", "2500.00", "Spicy goat-meat pepper soup."),
        ("Pepper Soup (Catfish)", "DEMO-M-PPR-CTF", "2800.00", "Catfish pepper soup."),
    ]),
    ("Local Delicacies", "RESTAURANT", "kitchen", [
        ("Abacha (African Salad)", "DEMO-M-ABA-STD", "1800.00", "Abacha with garden eggs and ugba."),
        ("Nkwobi", "DEMO-M-NKW-STD", "2500.00", "Spicy nkwobi (cow-foot delicacy)."),
        ("Isi Ewu", "DEMO-M-ISI-STD", "2800.00", "Goat-head isi ewu."),
        ("Suya Platter", "DEMO-M-SUY-PLT", "2000.00", "Spicy beef suya with onions & yaji."),
        ("Shawarma (Chicken)", "DEMO-M-SHW-CHK", "2000.00", "Chicken shawarma wrap."),
        ("Barbecue Chicken & Fries", "DEMO-M-BBQ-FRY", "3500.00", "Barbecue chicken with fries."),
        ("Grilled Fish & Yam", "DEMO-M-GRF-YAM", "4000.00", "Grilled croaker fish with boiled yam."),
        ("Beef Burger & Fries", "DEMO-M-BRG-FRY", "2500.00", "Beef burger with chips."),
        ("Chicken & Chips", "DEMO-M-CHK-CHP", "2500.00", "Fried chicken with chips."),
    ]),
    ("Breakfast, Snacks & Pastries", "RESTAURANT", "kitchen", [
        ("English Breakfast", "DEMO-M-BRK-ENG", "2500.00", "Eggs, sausage, beans, toast & tea."),
        ("Akara & Pap", "DEMO-M-BRK-AKR", "1200.00", "Akara with pap (ogi)."),
        ("Bread & Egg Sauce", "DEMO-M-BRK-BRD", "1500.00", "Toasted bread with egg sauce."),
        ("Yam & Egg Sauce", "DEMO-M-BRK-YAM", "1800.00", "Fried yam with egg sauce."),
        ("Meat Pie", "DEMO-M-SNK-MTP", "800.00", "Nigerian meat pie."),
        ("Doughnut", "DEMO-M-SNK-DGH", "500.00", "Glazed doughnut."),
        ("Puff Puff (5 pieces)", "DEMO-M-SNK-PPF", "500.00", "Fried puff puff."),
        ("Chin Chin (pack)", "DEMO-M-SNK-CHN", "700.00", "Crunchy chin chin pack."),
        ("Small Chops Combo", "DEMO-M-SNK-SML", "2000.00", "Puff puff, samosa, spring rolls, gizdodo."),
        ("Fruit Salad", "DEMO-M-DES-FRT", "1500.00", "Fresh seasonal fruit salad."),
        ("Chocolate Cake Slice", "DEMO-M-DES-CKE", "1200.00", "Slice of chocolate cake."),
        ("Ice Cream (2 scoops)", "DEMO-M-DES-ICE", "1000.00", "Two scoops of ice cream."),
    ]),
    ("Beverages — Non-Alcoholic", "BAR", "bar", [
        ("Bottled Water 75cl", "DEMO-B-WAT-75", "500.00", "Bottled still water."),
        ("Sachet Water", "DEMO-B-WAT-SCH", "200.00", "Sachet (pure) water."),
        ("Coke 50cl", "DEMO-B-SFT-COK", "800.00", "Coca-Cola 50cl bottle."),
        ("Fanta 50cl", "DEMO-B-SFT-FAN", "800.00", "Fanta 50cl bottle."),
        ("Sprite 50cl", "DEMO-B-SFT-SPR", "800.00", "Sprite 50cl bottle."),
        ("Malt Drink", "DEMO-B-MLT-STD", "1000.00", "Malted drink can."),
        ("Orange Juice (fresh)", "DEMO-B-JCE-ORG", "1500.00", "Freshly squeezed orange juice."),
        ("Pineapple Juice", "DEMO-B-JCE-PIN", "1500.00", "Pineapple juice."),
        ("Energy Drink", "DEMO-B-ENG-STD", "1500.00", "Energy drink can."),
        ("Lipton Tea", "DEMO-B-TEA-LIP", "700.00", "Cup of tea with milk & sugar."),
        ("Coffee", "DEMO-B-COF-STD", "800.00", "Cup of coffee."),
        ("Milo (hot)", "DEMO-B-MIL-HOT", "800.00", "Hot milo drink."),
        ("Yogurt Drink", "DEMO-B-YGT-STD", "1200.00", "Yogurt drink."),
        ("Smoothie (banana)", "DEMO-B-SMO-BAN", "2000.00", "Banana smoothie."),
        ("Non-Alcoholic Cocktail", "DEMO-B-CTL-NA", "2500.00", "Mocktail of the day."),
    ]),
    ("Beverages — Alcoholic", "BAR", "bar", [
        ("Lager Beer (bottle)", "DEMO-B-BER-LAG", "1200.00", "Chilled lager beer."),
        ("Wine (glass)", "DEMO-B-WIN-GLS", "2500.00", "Glass of wine (red or white)."),
        ("Wine (bottle)", "DEMO-B-WIN-BTL", "12000.00", "Bottle of wine."),
        ("Gin & Tonic", "DEMO-B-SPT-GNT", "3500.00", "Gin and tonic."),
        ("Whiskey (shot)", "DEMO-B-SPT-WHK", "3000.00", "Whiskey shot."),
    ]),
]


DEMO_STAFF = [
    # (first, last, role, department, job_title) — uses ONLY the 11 approved
    # staff roles and 11 approved departments while retaining the 16-account
    # demo dataset size across every hotel function.
    ("Demo", "GeneralManager", User.Role.GENERAL_MANAGER, "MANAGEMENT", "General Manager"),
    ("Demo", "SeniorReceptionist", User.Role.RECEPTIONIST, "FRONT_DESK", "Senior Receptionist"),
    ("Demo", "Receptionist", User.Role.RECEPTIONIST, "FRONT_DESK", "Receptionist"),
    ("Demo", "SeniorAccountant", User.Role.ACCOUNTANT, "ACCOUNTS", "Senior Accountant"),
    ("Demo", "Accountant", User.Role.ACCOUNTANT, "ACCOUNTS", "Accountant"),
    ("Demo", "AdminProcurement", User.Role.PROCUREMENT_OFFICER, "PROCUREMENT", "Admin Manager/Procurement"),
    ("Demo", "HeadChef", User.Role.CHEF, "KITCHEN", "Head Chef"),
    ("Demo", "Chef", User.Role.CHEF, "KITCHEN", "Chefs"),
    ("Demo", "KitchenAssistant", User.Role.KITCHEN_ASSISTANT, "KITCHEN_ASSISTANT", "Kitchen Assistant"),
    ("Demo", "Waiter", User.Role.WAITER, "RESTAURANT", "Waiters/Waitresses"),
    ("Demo", "Cashier", User.Role.WAITER, "RESTAURANT", "POS Cashier"),
    ("Demo", "Porter", User.Role.PORTER, "PORTER", "Porters"),
    ("Demo", "Housekeeper", User.Role.HOUSEKEEPING, "HOUSEKEEPING", "Housekeepers"),
    ("Demo", "FacilityHousekeeper", User.Role.HOUSEKEEPING, "HOUSEKEEPING", "Facility & Room Housekeeper"),
    ("Demo", "SecurityOfficer", User.Role.SECURITY, "SECURITY", "Security"),
    ("Demo", "Storekeeper", User.Role.STOREKEEPER, "INVENTORY", "Store Keeper"),
]


DEMO_SUPPLIERS = [
    ("DEMO-SUP-001", "DEMO Fresh Foods Market Ltd"),
    ("DEMO-SUP-002", "DEMO Beverages Distribution Co"),
    ("DEMO-SUP-003", "DEMO Hotel Supplies & Linens Ltd"),
    ("DEMO-SUP-004", "DEMO Cleaning & Consumables Ltd"),
    ("DEMO-SUP-005", "DEMO IT & Electronics Ltd"),
]

DEMO_LOCATIONS = [
    ("DEMO-STORE", "Demo Main Store"),
    ("DEMO-BAR", "Demo Bar Store"),
]

# (sku, name, category, base_unit, reorder_level, standard_cost)
DEMO_STOCK_ITEMS = [
    ("DEMO-I-RIC-01", "Long Grain Rice (50kg bag)", "Ingredients", "BAG", "2", "75000.00"),
    ("DEMO-I-TOM-01", "Tomato Paste (carton)", "Ingredients", "CARTON", "3", "18000.00"),
    ("DEMO-I-POL-01", "Palm Oil (25L keg)", "Ingredients", "KEG", "1", "45000.00"),
    ("DEMO-I-VEG-01", "Fresh Vegetables (crate)", "Ingredients", "CRATE", "2", "12000.00"),
    ("DEMO-I-CHK-01", "Chicken (carton)", "Ingredients", "CARTON", "3", "42000.00"),
    ("DEMO-I-GOT-01", "Goat Meat (kg)", "Ingredients", "KG", "10", "4500.00"),
    ("DEMO-I-CTF-01", "Catfish (kg)", "Ingredients", "KG", "8", "3800.00"),
    ("DEMO-B-WAT-75", "Bottled Water 75cl (pack of 12)", "Beverages", "PACK", "5", "2200.00"),
    ("DEMO-B-COK-50", "Coke 50cl (pack of 12)", "Beverages", "PACK", "5", "6500.00"),
    ("DEMO-B-BER-LG", "Lager Beer (crate of 12)", "Beverages", "CRATE", "4", "9000.00"),
    ("DEMO-C-LIN-01", "Bed Linen Set", "Linens", "SET", "10", "8500.00"),
    ("DEMO-C-TWL-01", "Bath Towel", "Linens", "PIECE", "20", "2500.00"),
    ("DEMO-C-CLN-01", "All-purpose Cleaner (5L)", "Cleaning", "JERRICAN", "4", "4500.00"),
    ("DEMO-C-AMN-01", "Guest Amenity Kit", "Consumables", "KIT", "30", "900.00"),
    ("DEMO-E-TVS-01", "Television 43\" (room asset)", "Equipment", "UNIT", "0", "280000.00"),
    ("DEMO-E-ACU-01", "Air Conditioner 1.5HP (room asset)", "Equipment", "UNIT", "0", "320000.00"),
    ("DEMO-E-SCN-01", "Barcode Scanner (POS)", "Equipment", "UNIT", "0", "45000.00"),
    ("DEMO-E-PTR-01", "Thermal Receipt Printer 80mm", "Equipment", "UNIT", "0", "85000.00"),
]

# (sku, opening quantity at DEMO-STORE)
DEMO_OPENING_BALANCES = [
    ("DEMO-I-RIC-01", "6"), ("DEMO-I-TOM-01", "8"), ("DEMO-I-POL-01", "3"),
    ("DEMO-I-VEG-01", "5"), ("DEMO-I-CHK-01", "7"), ("DEMO-I-GOT-01", "25"),
    ("DEMO-I-CTF-01", "20"), ("DEMO-B-WAT-75", "15"), ("DEMO-B-COK-50", "12"),
    ("DEMO-B-BER-LG", "10"), ("DEMO-C-LIN-01", "40"), ("DEMO-C-TWL-01", "80"),
    ("DEMO-C-CLN-01", "12"), ("DEMO-C-AMN-01", "120"), ("DEMO-E-TVS-01", "12"),
    ("DEMO-E-ACU-01", "12"), ("DEMO-E-SCN-01", "2"), ("DEMO-E-PTR-01", "3"),
]


# (menu_sku, location_code, [(stock_sku, quantity_per_menu_unit), ...])
DEMO_RECIPES = [
    ("DEMO-M-JOL-CHK", "DEMO-STORE", [
        ("DEMO-I-RIC-01", "0.0100"), ("DEMO-I-TOM-01", "0.0100"), ("DEMO-I-CHK-01", "0.1000"),
    ]),
    ("DEMO-M-EGU-PND", "DEMO-STORE", [
        ("DEMO-I-VEG-01", "0.0500"), ("DEMO-I-GOT-01", "0.1500"),
    ]),
    ("DEMO-M-GRF-YAM", "DEMO-STORE", [
        ("DEMO-I-CTF-01", "0.2500"), ("DEMO-I-POL-01", "0.0050"),
    ]),
    ("DEMO-B-SFT-COK", "DEMO-BAR", [("DEMO-B-COK-50", "0.0833")]),
    ("DEMO-B-BER-LAG", "DEMO-BAR", [("DEMO-B-BER-LG", "0.0833")]),
    ("DEMO-B-WAT-75", "DEMO-BAR", [("DEMO-B-WAT-75", "0.0833")]),
]

DEMO_SHIFT_TEMPLATES = [
    ("DEMO-MORNING", "Demo Morning Shift", "FRONT_DESK", "07:00", "15:00", 30),
    ("DEMO-AFTERNOON", "Demo Afternoon Shift", "RESTAURANT", "15:00", "23:00", 30),
    ("DEMO-NIGHT", "Demo Night Shift", "SECURITY", "23:00", "07:00", 30),
    ("DEMO-KITCHEN", "Demo Kitchen Day", "KITCHEN", "08:00", "16:00", 30),
    ("DEMO-HOUSEKEEPING", "Demo Housekeeping Day", "HOUSEKEEPING", "08:00", "16:00", 30),
]


class Command(BaseCommand):
    help = "Seed the isolated DEMO operations dataset (menu, staff, suppliers, stock). Idempotent."

    def add_arguments(self, parser):
        parser.add_argument(
            "--acknowledge-demo-prices",
            action="store_true",
            help="Acknowledge that all menu prices seeded here are DEMO placeholder "
                 "values that must be replaced with owner-approved prices before live use.",
        )

    # ---- safety ---------------------------------------------------------- #
    def _refuse_production(self):
        module = getattr(django_settings, "DJANGO_SETTINGS_MODULE", "") or ""
        if "production" in module.lower():
            raise CommandError(
                "Refusing to seed DEMO data with production settings "
                f"(DJANGO_SETTINGS_MODULE={module}). Demo seeding requires an "
                "isolated development/test environment or a dedicated staging "
                "database — never the live database."
            )
        if getattr(django_settings, "ENVIRONMENT", "") == "production":
            raise CommandError("Refusing to seed DEMO data: settings.ENVIRONMENT == production.")

    # ---- helpers --------------------------------------------------------- #
    def _get_or_create(self, model, defaults, label, created, **lookup):
        obj, was_created = model.objects.get_or_create(defaults=defaults, **lookup)
        if was_created:
            created.append(label)
        return obj, was_created

    @transaction.atomic
    def handle(self, *args, **options):
        self._refuse_production()
        if not options.get("acknowledge_demo_prices"):
            self.stdout.write(self.style.WARNING(
                "Menu prices seeded by this command are DEMO placeholder values "
                "(every item description is marked DEMO)."
            ))
        created = []

        # 1. Room inventory: six physical rooms per existing demo room type.
        for type_name, rooms in ROOM_INVENTORY_PLAN.items():
            room_type = RoomType.objects.filter(name=type_name).first()
            if room_type is None:
                self.stdout.write(self.style.WARNING(
                    f"Room type '{type_name}' is absent — run seed_demo first. "
                    "Skipping its rooms (no room type or price is invented here)."
                ))
                continue
            for number, floor in rooms:
                self._get_or_create(
                    Room, defaults={"room_type": room_type, "floor": floor},
                    label=f"room:{number}", created=created, room_number=str(number),
                )

        # 2. Nigerian menu catalogue.
        for cat_name, service_area, station, items in MENU_CATALOGUE:
            category, _ = self._get_or_create(
                MenuCategory,
                defaults={
                    "slug": "demo-" + cat_name.lower().replace(" ", "-").replace("—", "-").replace("&", "and"),
                    "service_area": service_area,
                    "description": f"{cat_name} ({DEMO} catalogue)",
                    "sort_order": 0,
                },
                label=f"menu-category:{cat_name}", created=created, name=cat_name,
            )
            for row in items:
                name, sku, price, description = row
                self._get_or_create(
                    MenuItem,
                    defaults={
                        "category": category,
                        "name": name,
                        "description": f"{description} ({DEMO} price {format_money(price)} — placeholder; replace with owner-approved price)",
                        "base_price": Decimal(price),
                        "tax_rate_percent": Decimal("0.00"),  # customer-billing tax removal
                        "is_available": True,
                        "is_active": True,
                    },
                    label=f"menu-item:{sku}", created=created, sku=sku,
                )

        # 3. Synthetic DEMO staff.
        for first, last, role, department, title in DEMO_STAFF:
            email = f"{first.lower()}.{last.lower()}@{DEMO_EMAIL_DOMAIN}"
            user, was_created = self._get_or_create(
                User,
                defaults={
                    "first_name": first, "last_name": f"{last} ({DEMO})",
                    "role": role, "is_active": True,
                },
                label=f"staff:{email}", created=created, email=email,
            )
            if was_created:
                user.set_unusable_password()  # no demo credentials are minted or logged
                user.save(update_fields=["password"])
            StaffProfile.objects.get_or_create(
                user=user,
                defaults={
                    "employee_code": f"{DEMO}-EMP-{user.pk:04d}",
                    "department": department,
                    "job_title": f"{title} ({DEMO})",
                    "employment_status": StaffProfile.EmploymentStatus.ACTIVE,
                    "employment_start": date.today() - timedelta(days=30),
                    "notes": "Synthetic DEMO employee created by seed_demo_operations. "
                             "Not a real person; unusable password; do not attribute "
                             "real activity to this account.",
                },
            )

        # 4. Suppliers.
        suppliers = {}
        for reference, name in DEMO_SUPPLIERS:
            obj, _ = self._get_or_create(
                Supplier,
                defaults={
                    "name": name,
                    "notes": f"Synthetic {DEMO} supplier for isolated testing.",
                },
                label=f"supplier:{reference}", created=created, reference=reference,
            )
            suppliers[reference] = obj

        # 5. Stock locations.
        locations = {}
        for code, name in DEMO_LOCATIONS:
            obj, _ = self._get_or_create(
                StockLocation,
                defaults={"name": name, "description": f"Isolated {DEMO} stock location."},
                label=f"location:{code}", created=created, code=code,
            )
            locations[code] = obj

        # 6. Stock items + demo opening balances (immutable OPENING movements).
        store = locations["DEMO-STORE"]
        items = {}
        for sku, name, category, unit, reorder, cost in DEMO_STOCK_ITEMS:
            obj, _ = self._get_or_create(
                StockItem,
                defaults={
                    "name": name,
                    "category": f"{category} ({DEMO})",
                    "base_unit": unit,
                    "reorder_level": Decimal(reorder),
                    "standard_cost": Decimal(cost),
                    "preferred_supplier": suppliers["DEMO-SUP-001"],
                    "notes": f"Synthetic {DEMO} stock item — quantities and costs are demo values.",
                },
                label=f"stock-item:{sku}", created=created, sku=sku,
            )
            items[sku] = obj

        for sku, qty in DEMO_OPENING_BALANCES:
            record_stock_movement(
                item=items[sku], location=store,
                movement_type=StockMovement.Type.OPENING,
                quantity_delta=Decimal(qty),
                source_key=f"demo-opening:{sku}:DEMO-STORE",
                actor=None,
                notes=f"{DEMO} opening balance (isolated test data)",
                source_reference=f"{DEMO}-OPENING",
            )

        # 7. Demo recipes (menu item -> stock components, versioned BOM).
        from datetime import time as _time

        for menu_sku, location_code, components in DEMO_RECIPES:
            menu_item = MenuItem.objects.filter(sku=menu_sku).first()
            if menu_item is None or any(stock_sku not in items for stock_sku, _ in components):
                continue
            recipe, was_created = StockRecipe.objects.get_or_create(
                menu_item=menu_item, version=1,
                defaults={
                    "reference": f"DEMO-RCP-{menu_sku}",
                    "idempotency_key": f"demo-recipe:{menu_sku}:v1",
                    "location": locations[location_code],
                    "tracking_mode": StockRecipe.TrackingMode.COMPONENTS,
                    "notes": f"Synthetic {DEMO} recipe for isolated testing.",
                },
            )
            if was_created:
                created.append(f"recipe:{menu_sku}")
                for stock_sku, qty in components:
                    StockRecipeLine.objects.create(
                        recipe=recipe, item=items[stock_sku],
                        quantity_per_menu_unit=Decimal(qty),
                    )

        # 8. Demo shift templates and one scheduled shift per demo staff member.
        templates = {}
        for code, name, department, start_hm, end_hm, break_min in DEMO_SHIFT_TEMPLATES:
            start_h, start_m = (int(x) for x in start_hm.split(":"))
            end_h, end_m = (int(x) for x in end_hm.split(":"))
            tpl, was_created = self._get_or_create(
                ShiftTemplate,
                defaults={
                    "name": f"{name} ({DEMO})",
                    "department": department,
                    "start_time": _time(start_h, start_m),
                    "end_time": _time(end_h, end_m),
                    "unpaid_break_minutes": break_min,
                    "notes": f"Synthetic {DEMO} shift template.",
                },
                label=f"shift-template:{code}", created=created, code=code,
            )
            templates[code] = tpl

        tomorrow = date.today() + timedelta(days=1)
        dept_template = {
            "Reception": "DEMO-MORNING", "Restaurant": "DEMO-AFTERNOON", "Bar": "DEMO-AFTERNOON",
            "Kitchen": "DEMO-KITCHEN", "Housekeeping": "DEMO-HOUSEKEEPING",
            "Security": "DEMO-NIGHT", "Maintenance": "DEMO-MORNING", "Stores": "DEMO-MORNING",
        }
        for profile in StaffProfile.objects.filter(notes__contains=DEMO):
            template = templates[dept_template.get(profile.department, "DEMO-MORNING")]
            reference = f"DEMO-SHIFT-{profile.user_id:04d}-001"
            _, was_created = self._get_or_create(
                ShiftAssignment,
                defaults={
                    "idempotency_key": f"demo-shift:{profile.user_id}:001",
                    "staff": profile.user,
                    "template": template,
                    "status": ShiftAssignment.Status.SCHEDULED,
                    "planned_start_at": timezone.make_aware(
                        datetime.combine(tomorrow, template.start_time)
                    ),
                    "planned_end_at": timezone.make_aware(
                        datetime.combine(tomorrow, template.end_time)
                        if template.end_time > template.start_time
                        else datetime.combine(tomorrow + timedelta(days=1), template.end_time)
                    ),
                    "department": profile.department,
                    "notes": f"Synthetic {DEMO} shift assignment.",
                },
                label=f"shift:{reference}", created=created, reference=reference,
            )

        self.stdout.write(self.style.SUCCESS(
            f"Demo operations seed complete. {len(created)} object(s) created "
            f"(re-runs are idempotent)."
        ))
        if created:
            self.stdout.write("Created: " + ", ".join(created))
        self.stdout.write(
            "Demo records are labelled DEMO in names/descriptions/notes and are "
            "confined to this isolated environment. Production inventory and "
            "financial records were not touched."
        )
