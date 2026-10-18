# Deployment Instructions — repair release 1.1.24 (`main @ e811fbd`)

**Prerequisite: explicit owner authorization** (constraint: deploy only through the established release process and only when authorized). The 5 commits are local and unpushed.

**Risk profile: LOW.** Zero database migrations (verified — diff of `backend/apps/*/migrations` between `f2e8a5d` and HEAD is empty) ⇒ no schema change, no data migration, no downtime requirement beyond the standard redeploy. Changes: 1 file deleted (dead manifest), 50 files version-string-only, 3 backend files (serializer-level validation ordering + tests), 1 frontend page (pure code movement), 1 CSS rule (+6 lines). No dependency changes; no environment/config changes required.

---

## 1. Pre-deploy checks (5 minutes)

1. Confirm working tree = the audited state:
   ```bash
   git log --oneline -6   # e811fbd, 3e17720, 5aa4ebe, cc56902, 29364c5, f2e8a5d
   git status --short     # must be empty
   ```
2. Re-run the gates (all must be green):
   ```bash
   cd backend && python manage.py test            # expect: 1548 OK (skipped=15)
   cd ../frontend && python3 validate.py          # expect: ALL OK
   cd .. && node --test frontend/tests-js/        # expect: 545 pass / 0 fail / 1 skip
   ```
3. Confirm production env vars unchanged/valid (Railway dashboard): `EMAIL_PROVIDER`, Paystack keys, `ALLOW_ADMIN_APPROVAL_OVERRIDE` (recommend flipping to `False` once B1 second approver exists — separate, optional change).

## 2. Backup (mandatory, even with no migrations)

- **Database (Railway/MySQL):** take a provider snapshot or `mysqldump` of the production DB before deploying. Nothing in this release writes schema, but the snapshot is the rollback anchor.
- **Frontend:** Vercel keeps every deployment immutable — the current production deployment (1.1.23) *is* the frontend rollback point; note its deployment URL/ID.
- **Backend:** note the current Railway deployment ID (previous image = rollback point).

## 3. Deploy

**Backend (Railway):**
```bash
git push origin main        # Railway auto-deploys from the connected branch
```
- No `migrate` needed (no new migrations); the standard release command running `migrate` is a safe no-op.
- Wait for the deploy to report healthy (`/api/health/ready/` → 200 `"database":"up"`).

**Frontend (Vercel):**
- Same push triggers the Vercel production build (static site — build = copy). Promote the new deployment to Production per the established process.
- `sw.js` cache key becomes `jone-v1.1.24`; already-open staff tabs receive the update-checker prompt (this release *is* the fix that makes that prompt work again — DEF-1).

## 4. Post-deploy smoke (read-only + one staging write; ~10 minutes)

Run against production **read-only** first:
```bash
curl -s https://j1-hotel-management-system-production.up.railway.app/api/health/ready/
# → {"success": true, "status": "ok", "database": "up"}
curl -s https://j1-hotel-management-system-frontend.vercel.app/version.json
# → {"version": "1.1.24"}
curl -s "https://j1-hotel-management-system-production.up.railway.app/api/rooms/availability/?check_in=<+30d>&check_out=<+32d>&adults=2"
# → 200 with room types
```
**DEF-3 verification (staging strongly preferred; if run in production, use the guaranteed-non-creating probe):**
- Staging: submit the public booking form with phone `+234 (0) 803 123 4567` → expect **201** and stored phone `+23408031234567`.
- Production (no-write probe, identical to the audit probe): POST `/api/bookings/` with a **past** check-in date AND the 23-char formatted phone → expect **400** whose errors mention the *date* (not `no more than 20 characters`). If the phone error still appears, the backend deploy did not take.
**DEF-4/#5 verification (browser, harmless):**
- Open `/dashboard/accounting.html` → console shows **no ReferenceError**; approvals/expenses tables render.
- Sidebar on a non-installable browser → no visible "Install J-ONE Staff App" button.
- Any dashboard page → assets requested with `?v=1.1.24`; hard-refresh once.
**Then (B7):** enroll a POS terminal and place one test order to close the urgent-failure-2 loop on real hardware.

## 5. Rollback plan (if any smoke step fails)

- **Frontend:** Vercel → Promote previous deployment (1.1.23) to Production. Instant; clients' update-checker will offer the older bundle again; `sw.js` cache key reverts on next visit.
- **Backend:** Railway → redeploy previous deployment/image (`f2e8a5d` build). No DB restore needed (no schema changed); restore the snapshot **only** if an unrelated data incident occurred.
- **Git:** `git revert e811fbd 3e17720 5aa4ebe cc56902 29364c5` (revert, don't force-push) if the branch itself must be unwound.
- Rollback triggers: health/ready not 200 after deploy; booking create regressing for *valid* payloads; accounting page errors worse than baseline; any 5xx spike on `/api/bookings/`.

## 6. Post-release follow-ups (tracked, non-blocking)

B1 (prod second approver + consider `ALLOW_ADMIN_APPROVAL_OVERRIDE=False`), B2 (stale payment reconciliation), B4 (payroll statutory cycle in staging), B5 (Paystack test-mode round-trip), B6 (folio-close decision), B7 (terminal enrollment sweep). Each has owner + verify step in Doc 07.
