# Dependencies and integration

No new dependencies. Uses existing Django/DRF, openpyxl, ReportLab, native HTML date inputs and shared JONE API/modal clients. Install the repository's `backend/requirements.txt`; run its existing application as usual.

No database migration is required (`makemigrations --check --dry-run`: no changes). **Do not delete** historic tax account codes, hotel-settings columns, immutable journals or older migrations. The historical settings fields remain in the DB but are no longer exposed or consulted for new accruals. Existing backend finance-detail endpoint now adds `reversal_of_reference` and `reversals` response fields; existing routes and response wrappers are preserved. Summary removes the current `statutory_tax_accrued` field, retains `tax_accrued` for legacy API audit consumers, and adds signed-reversal metrics. Review any external client relying on the removed current statutory field before deployment.

Copy package paths onto an integration branch at repository root, review diff, run checks/tests, then deploy backend and frontend together. Do not replace the original repository or apply this ZIP as a standalone application.
