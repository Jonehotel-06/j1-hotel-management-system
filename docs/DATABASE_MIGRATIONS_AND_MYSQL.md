# Moving the project to MySQL/MariaDB — and the "Conflicting migrations" error

This document covers the two things that happened when the database platform was
changed:

1. `python manage.py migrate` refused to run with
   **`CommandError: Conflicting migrations detected; multiple leaf nodes in the migration graph`**
   — this is a **file** problem in the copied folder, not a database problem;
2. MariaDB printed **`models.W036 … does not support unique constraints with conditions`**
   — one of those was a real hole in the stock-guard rules and is now fixed; the
   rest are listed below with the code-level guard that covers them.

The repository itself is correct: its migration chain is linear and migrates cleanly
(verified below on a fresh database). Nothing in the database you already built is
touched by the repair described here.

---

## 1. Why the migration error happened

Django builds a graph of migration files at startup. When a **single app** contains two
files that are not connected to each other, both are "leaf" nodes and Django cannot know
which one to apply. Nothing in your MariaDB server causes this — the same folder would
fail on SQLite too.

An older copy of this project (or an older `backend` folder merged on top of a newer one)
was left in `apps/*/migrations`, and during development some **foundation migrations were
renamed** and some **migrations were renumbered**, so the old file and the new file both
exist:

| App | Old leftover file | This release's chain |
|---|---|---|
| `inventory` | `0001_initial.py` | `0001_inventory_procurement_foundation.py` … `0007_classify_existing_stock_items.py` |
| `enquiries` | `0002_initial.py` | `0001_initial.py`, `0002_cancellation_request_workflow.py` |
| `staff_operations` | `0001_initial.py` | `0001_staff_operations_foundation.py` … `0015_payroll_salary_payment_receipt.py` |
| `finance` | `0001_initial.py` | `0001_folio_finance_foundation.py` … `0014_classify_existing_folio_money.py` |
| `maintenance` | `0001_initial.py` | `0001_maintenance_work_order_foundation.py` |
| `guest_services` | `0001_initial.py` | `0001_guest_service_requests.py` … `0006_laundry_walk_in_customers.py` |
| `bookings` | `0002_initial.py` | `0001_initial.py` … `0004_report_range_indexes.py` |
| `stays` | `0001_initial.py` | `0001_stay_foundation.py` |
| `housekeeping` | `0001_initial.py` | `0001_housekeeping_task_foundation.py` |
| `pos` | `0001_initial.py` | `0001_pos_room_service_foundation.py` … `0006_backfill_menuitem_sku_barcodes.py` |
| `portal` | `0001_initial.py` | `0001_capability_and_portal_foundation.py`, `0002_guest_portal_invitation.py` |

These 11 files are exactly the ones named in the error message. There are **no others**
— every remaining migration file belongs to this release.

### Do NOT run `makemigrations --merge`

Django suggests it, but it is the wrong tool here:

* it creates a merge migration that joins the OLD chain (whose operations no longer match
  the models) to the NEW one, so the old migrations would try to create tables that the
  new chain already creates → `Table 'X' already exists` or duplicate/lost columns;
* the old files were written against models that no longer exist, so a fresh database
  built from that chain would not match `apps/*/models.py`;
* it leaves the stale files in place, so `makemigrations --check` and every future
  release stay confused.

The stale files must go. That is what `tools/repair_migrations.py` does.

---

## 2. The repair

From the `backend` folder (the same folder `manage.py` lives in):

```bash
python tools/repair_migrations.py --dry-run   # shows what would move; changes nothing
python tools/repair_migrations.py            # moves the leftovers aside
python manage.py migrate
```

`tools/repair_migrations.py`:

* compares `apps/*/migrations/*.py` against **`tools/migration_manifest.txt`**, the
  canonical list of this release's migration files (116 files);
* **moves** — never deletes — every file that is not in the manifest to
  `backend/.migration_backup/<UTC timestamp>/…` (and drops its stale `__pycache__`
  byte-code, or Python would keep importing it);
* reports any manifest entry that is **missing** from the folder, which would mean the
  copy is older than this release (restore it from the repository instead);
* has `--restore` to put the last backup back, and `--write-manifest` to regenerate the
  manifest when a new migration is added.

Verified in this repository: the 11 leftover files were recreated to reproduce the exact
error, the repair moved them, and `python manage.py migrate` then ran cleanly
(`No migrations to apply`, `makemigrations --check` → `No changes detected`) — see §6.

> If you prefer to do it by hand, deleting those 11 files from the table above has the
> same effect on the migration graph. The script is safer because it also removes the
> compiled copies and keeps a backup.

---

## 3. Starting the client database from scratch on MariaDB/MySQL

You chose to start again, so nothing needs to be migrated from the old database.
Recommended order:

```bash
# 1. Create the database (utf8mb4 is required for emoji/₦/names beyond ASCII).
#    In the MySQL/MariaDB client:
#      CREATE DATABASE jone_hotel CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
#      CREATE USER 'jone'@'localhost' IDENTIFIED BY '…';
#      GRANT ALL PRIVILEGES ON jone_hotel.* TO 'jone'@'localhost';

# 2. Point the backend at it (backend/.env) — either one URL:
#      DATABASE_URL=mysql://jone:password@127.0.0.1:3306/jone_hotel
#    …or the components:
#      DB_ENGINE=django.db.backends.mysql
#      DB_NAME=jone_hotel
#      DB_USER=jone
#      DB_PASSWORD=…
#      DB_HOST=127.0.0.1
#      DB_PORT=3306

# 3. Repair any leftover migration files (once, after copying the project):
python tools/repair_migrations.py

# 4. Build the schema:
python manage.py check          # must show no models.W036 for inventory
python manage.py migrate        # 116 migrations, ~1 minute

# 5. Create the owner (Administrator) account:
python manage.py createsuperuser

# 6. Start the API:
python manage.py runserver
```

Notes:

* `python manage.py migrate` also seeds the capability matrix, the role grants, the
  finance control policies and the hotel settings singleton — no extra command is needed
  before the first sign-in.
* `python manage.py seed_demo` exists for **development/demo content only** (placeholder
  room types and prices). Do not run it against the client's live database; create the
  real room types, rooms, amenities and staff through the dashboard instead.
* `python manage.py seed_nigeria_payroll_rules` loads the statutory payroll rules and is
  safe to run on a real database if you use the payroll module.
* If you ever want to carry data over from the old SQLite file instead of starting fresh:
  `python manage.py dumpdata --natural-foreign --natural-primary -e contenttypes -e auth.Permission -o backup.json`
  on the old database, then `python manage.py loaddata backup.json` on the new one. That
  path was **not** used for this deployment and is not required by it.

---

## 4. The MariaDB warnings (`models.W036`)

MySQL and MariaDB do not support **partial (conditional) indexes**, so Django creates the
table **without** a conditional unique constraint and only prints the warning. Six
constraints were affected. One of them was a real hole; it is fixed. The rest are listed
here with the code that already prevents the duplicate.

| Model | Constraint | What it protects | On MariaDB |
|---|---|---|---|
| `inventory.StockItem` | `one_direct_sale_stock_item_per_menu_item` | one stock item per POS menu item (otherwise one sale deducts twice) | **fixed** — see below |
| `payments.Refund` | `unique_active_refund_per_payment` | no two live refund attempts on one payment | code guard: `payment_service.py` locks the payment (`select_for_update`), re-reads the existing refund for the cancellation request and reconciles `processed_total` / `active_total` / `available` before submitting |
| `payments.Refund` | `unique_refund_per_cancellation_request` | one refund per cancellation request | same lookup (`Refund.objects.filter(cancellation_request=…)`) inside the locked transaction |
| `payments.Refund` | `unique_paystack_refund_id_when_present` | a replayed Paystack callback cannot create a second refund row | `Refund.objects.filter(paystack_refund_id=provider_id).first()` inside the locked transaction |
| `payments.Refund` | `unique_paystack_refund_ref_when_present` | same, by provider reference | same lookup chain |
| `staff_operations.FaceTemplate` | one active template per staff member | two active face templates | `face_service.request_enrollment` locks the staff's templates, rejects every pending one and only one can become `ACTIVE` |
| `staff_operations.FingerprintTemplate` | one active template per staff member | two active fingerprint templates | `fingerprint_service` reads the single `ACTIVE` template and supersedes the old one inside a locked transaction |

### The fix: `inventory.StockItem` now uses a portable constraint

```python
models.UniqueConstraint(fields=["direct_sale_menu_item"],
                        name="one_direct_sale_stock_item_per_menu_item")
```

A **plain** unique constraint has exactly the meaning the condition expressed for a
nullable column: `NULL` (not configured as a direct sale) counts as distinct on every
backend, so any number of items may be unconfigured, while two items can never claim the
same menu item. Migration
`inventory/0008_portable_direct_sale_unique.py` replaces the old constraint, so the guard
now **really exists** on MariaDB instead of silently disappearing. It is covered by
`tests/test_inventory_item_management.py::PortableConstraintTests`, which asserts the
constraint has no condition and that the database itself refuses the duplicate.

The two biometric and four refund constraints are left as they are on purpose: replacing
them means adding derived columns and rewriting the money-refund flow, which is a larger
change than a platform move should carry, and each one already has the application-level
guard listed above.

---

## 5. Verifying the new database

```bash
python manage.py check                        # no issues
python manage.py showmigrations --plan | tail # one linear chain per app
python manage.py makemigrations --check --dry-run   # "No changes detected"
python manage.py reconcile_accommodation --help     # read-only audit commands are available
```

After signing in as the owner: **Settings → Hotel**, then the room types and rooms, then
staff accounts, then the POS menu and stock items (`Inventory` → the configuration audit
`python manage.py inventory_configuration_audit` lists anything still unconfigured).

---

## 6. Evidence for the repair (reproduced in this repository)

| Step | Result |
|---|---|
| Recreate the 11 leftover files listed in §1 | `python manage.py migrate` → **the exact reported error**, naming the same 11 apps |
| `python tools/repair_migrations.py --dry-run` | lists 11 leftovers; nothing moved |
| `python tools/repair_migrations.py` | moves 11 files to `.migration_backup/<timestamp>/`; nothing deleted |
| `python manage.py migrate` | `No migrations to apply` (schema already complete); `showmigrations --plan` shows one chain |
| `python tools/repair_migrations.py --restore` | puts all 11 files back and the error returns (backup is complete) |
| `python manage.py check` / `makemigrations --check --dry-run` | no issues / `No changes detected` |
| `python manage.py test tests.test_inventory_item_management --parallel 1` | **33 tests OK** (includes the new portability test) |
