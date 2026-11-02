# Rollback and isolation

No changes have been applied to the user's original project or production database. The isolated source clone and output are separate. To discard without deploying, delete `ai_updates/refactored_system/` and optionally `/home/user/j1-isolated-work`; the original project remains untouched. The zip is another copy and can be deleted independently.

If an authorized operator has copied these files into a project, stop deployment, compare the five code/test files against commit `b613cd20e8de7b6a610a6145ff02392596122a90` and restore the pre-deployment copies using that project's release procedure. Re-run checks and finance reconciliation before restarting services. Do not run broad destructive reset commands against an unknown working tree. Restore pre-deployment code from a verified backup if the project differs from the inspected commit.

No migrations are included, so this overlay has no migration to reverse. Code rollback does not reverse financial activity posted while the code was live. Never delete or rewrite posted ledger data for rollback; any necessary correction requires independently authorized balanced reversal/adjustment transactions and verified backups. Reconcile NGN and foreign currencies separately when comparing statements before/after rollback: old statements mixed them.
