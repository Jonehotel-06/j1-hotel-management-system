# FRONTDESK_LOGIN_FIX — ROLLBACK

Everything in this package is **additive**: no existing behaviour, permission, message, or database schema is altered — the only changes to shared code are (a) a new optional flag on `StaffSignInRestrictedError` (default `False` → payload identical to before), (b) two new payload/notice branches, and (c) new files. Rolling back is therefore safe and requires no migrations.

## Option A — restore the 7 modified files (exact, recommended)

Restore these files from the diff baseline (commit `2131cb0` = `5aeeb18` + the delivered POS fix; if the POS package was never applied, restore from `5aeeb18` for the six files it did not touch, and from your pre-POS backup for `backend/apps/core/exceptions.py`):

```
backend/apps/accounts/desktop_policy.py
backend/apps/accounts/serializers.py
backend/apps/accounts/views.py
backend/apps/core/exceptions.py
backend/apps/hotel/views_admin.py
frontend/dashboard/settings.html
frontend/login.html
```

Then delete the new files (they are new — nothing else references them except the new tests):

```
backend/apps/accounts/management/           # whole directory (__init__.py, commands/)
backend/tests/test_frontdesk_login_e2e.py
frontend/tests-js/test-frontdesk-login-ui.js
```

## Option B — git (if the files are tracked)

```
git checkout 2131cb0 -- backend/apps/accounts/desktop_policy.py backend/apps/accounts/serializers.py \
                        backend/apps/accounts/views.py backend/apps/core/exceptions.py \
                        backend/apps/hotel/views_admin.py frontend/dashboard/settings.html frontend/login.html
git clean -fd backend/apps/accounts/management backend/tests/test_frontdesk_login_e2e.py \
              frontend/tests-js/test-frontdesk-login-ui.js
```

## Option C — patch reversal

From the repo root with `PATCH.diff` (this package) applied:

```
git apply -R FRONTDESK_LOGIN_FIX/PATCH.diff
```

## After any option

1. Restart the backend service (Django app) and redeploy/re-upload the two frontend HTML files (or just revert them in your static host).
2. No database migration to reverse — none was added. No env vars, no settings, no cron/worker changes.
3. Verify with the pre-existing suites:
   - `python manage.py test tests.test_auth tests.test_receptionist_desktop_sign_in tests.test_receptionist_desktop_ip tests.test_front_desk_desktop_ip` → all OK
   - `node --test tests-js/test-login-error-states.js` (cwd `frontend/`) → all pass
4. Post-rollback behaviour returns exactly to: sign-in refusals again advise "register this address" even when the real missing step is the Front Desk workstation record, and the Settings page no longer warns about the unregistered workstation. Authentication, sessions, device authorization and all other departments are unaffected either way.

## Rollback risk

None identified. The flag defaults to off; removing the files removes the only readers. The `errors.no_active_front_desk_workstation` key simply disappears from refusal payloads (frontend treats unknown/absent keys as absent — the pre-existing branches run). No data written by the new command exists to clean up (it is read-only).
