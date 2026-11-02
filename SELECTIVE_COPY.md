# Selective Copy Instructions

**Do NOT replace the whole project with `JONE_WORK`.** Copy only the files you want, into the corresponding paths of your own deployment. Every file below exists in `changed_files/` with the exact original relative path.

## 1. Preview before copying

```bash
cd /home/user/JONE_ACCOUNTING_AUDIT_OUTPUT
cat patches/diffstat.txt
less patches/accounting-audit-repair.patch     # complete unified diff
cat changed_files/MANIFEST.md                   # per-file reason + line refs
```

## 2. Copy all repaired Accounting files (recommended)

```bash
SRC=/home/user/JONE_ACCOUNTING_AUDIT_OUTPUT/changed_files
DST=/path/to/your/j1-hotel-management-system
cd "$SRC"
find . -type f ! -name 'MANIFEST.md' ! -name 'SELECTIVE_COPY.md' -print0 |
  while IFS= read -r -d '' f; do
    mkdir -p "$DST/$(dirname "$f")"
    cp -p "$f" "$DST/$f"
  done
```

This copies **only 14 files** (12 modified + 2 new tests). It does not copy the whole project, does not delete anything and does not touch Reports or any other page.

## 3. Copy a subset / skip a problematic file

Because each file is independent, you can copy any subset — for example, backend only:

```bash
SRC=/home/user/JONE_ACCOUNTING_AUDIT_OUTPUT/changed_files
DST=/path/to/your/j1-hotel-management-system
for f in \
  backend/apps/finance/services/guest_receivables_service.py \
  backend/apps/finance/services/night_audit_service.py \
  backend/apps/finance/services/ledger_reports.py \
  backend/apps/finance/services/payment_visibility.py \
  backend/apps/finance/services/reporting_service.py \
  backend/apps/finance/services/procurement_payable_service.py \
  backend/apps/finance/services/supplier_payment_service.py \
  backend/apps/finance/services/statements_service.py \
  backend/apps/finance/views.py \
  backend/apps/finance/urls.py ; do
    mkdir -p "$DST/$(dirname "$f")"
    cp -p "$SRC/$f" "$DST/$f"
done
```

**Dependency note:** the two Accounting HTML files and the new frontend test expect the additive backend keys. If you deliberately omit a backend service, either omit the corresponding frontend file too or accept that the new card will show `--` (the code guards every missing key). No existing KPI breaks when a new key is absent.

To skip the new reversal route entirely, omit `backend/apps/finance/urls.py` and remove the `SupplierSettlementReverseView` block from `backend/apps/finance/views.py`, or simply omit both and use the rest. The reversal button in `accounting.html` will then 404 only if clicked; no other figure changes.

## 4. Apply as a patch instead (all-or-nothing, or per file)

```bash
cd /path/to/your/j1-hotel-management-system
git apply --check /home/user/JONE_ACCOUNTING_AUDIT_OUTPUT/patches/accounting-audit-repair.patch
git apply        /home/user/JONE_ACCOUNTING_AUDIT_OUTPUT/patches/accounting-audit-repair.patch
```

`git apply --check` first. To apply only some files, use `git apply --include=<path>` repeatedly, e.g.:

```bash
git apply --include='backend/apps/finance/services/guest_receivables_service.py' \
          /home/user/JONE_ACCOUNTING_AUDIT_OUTPUT/patches/accounting-audit-repair.patch
```

## 5. After copying (mandatory verification)

```bash
cd /path/to/your/j1-hotel-management-system/backend
python manage.py check
python manage.py makemigrations --check --dry-run     # expect "No changes detected"
python manage.py test tests.test_accounting_audit_repair --noinput
python manage.py test tests.test_financial_summary_report --noinput

cd ../frontend
python3 validate.py
node --test tests-js/test-accounting-audit-repair-ui.js
node --test tests-js/            # compare failure names with the recorded baseline
```

If any of these fail, restore the individual file(s) from `../rollback/` (see `ROLLBACK.md`) rather than reverting the whole project.

## 6. What NOT to do

* Do not copy `JONE_WORK` over the original project.
* Do not delete historical financial rows, draft expenses, or procurement payment evidence.
* Do not run any migration (none is supplied or needed).
* Do not modify `frontend/dashboard/reports.html`.
* Do not hardcode credentials or expose `/tmp/jone_login.json` (deleted after the read-only probe).
