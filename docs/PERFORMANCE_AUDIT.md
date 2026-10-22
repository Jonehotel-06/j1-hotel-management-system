# J-ONE Hotel & Lodge — Performance Audit

**Date:** 2026-10-22  ·  **Revision audited:** `ea56da5`
**Environment:** Django test runner on SQLite (WAL), same container as the audit.
**Companion:** `docs/COMPREHENSIVE_SYSTEM_AUDIT.md` §6.

> **Read this first.** Every number below is a *local, comparative* measurement
> on SQLite. They are valid for "did this change reduce work per request?" and
> invalid as a production latency promise. No production database, network,
> CDN or device was measured. Where a conclusion depends on production, it is
> marked **unverified**.

---

## 1. Method

* **Query counts** — `CaptureQueriesContext` around one API call, after a warm
  request, on a seeded dataset. Query count is engine-independent and is the
  primary metric here.
* **Wall clock** — `time.perf_counter()` around the same call, median of three
  runs, inside the test process. SQLite is in-process, so absolute times are
  optimistic and dominated by Python/serializer work rather than I/O.
* **Dataset** — 60 rooms, 8 room types, 200 bookings, 40 staff with profiles,
  ~2 400 attendance records (including 40 open), one enrolled fingerprint per
  measured staff member, one reception workstation.
* **Frontend** — byte census over `frontend/` (`du`, per-file `wc -c`), script
  tag placement read from the shipped HTML.

The harness was temporary (removed from the final tree to avoid shipping a
timing-sensitive test). To recreate it: seed the factories above, wrap each
`self.client.get(...)` in `CaptureQueriesContext(connection)`, print
`len(ctx)` and elapsed milliseconds.

---

## 2. Before / after

| Endpoint | Before | After | Δ queries | Δ wall |
| --- | --- | --- | --- | --- |
| `GET /api/admin/staff-operations/profiles/` | **82 q / 52.1 ms** | **4 q / 17.0 ms** | **−78 (−95 %)** | **−67 %** |

### 2.1 What was wrong

`_profile_queryset()` selected the profile row and its relations, but the list
serializer asked each row for two things that were not prefetched:

* the staff member's fingerprint templates (to render enrolment state),
* their open attendance record (to render "on duty / on break").

That is two extra queries per row — 40 rows produced 82 queries. It is a classic
N+1, invisible on a two-person test fixture and painful on a real staff list.

### 2.2 The change

```python
.prefetch_related(
    Prefetch("user__fingerprint_templates",
             queryset=FingerprintTemplate.objects.only("staff_id", "status"),
             to_attr="_jone_fingerprint_templates"),
    Prefetch("user__attendance_records",
             queryset=AttendanceRecord.objects.filter(clock_out_at__isnull=True)
                      .only("staff_id", "status", "clock_in_at").order_by("-clock_in_at"),
             to_attr="_jone_open_attendance"),
)
```

The serializer reads the prefetched attributes when present and keeps its
per-row fallback for detail views and any caller that loads a profile outside
this queryset. Response shape, field names and ordering are unchanged; the 259
backend tests that touch staff and attendance pass unmodified.

### 2.3 Why not more

A second pass on the same endpoint (adding `select_related` for the reporting
manager's department, or joining attendance status into the profile row) showed
no measurable gain at this dataset size and would have coupled the profile
serializer to attendance history. Stopped at 4 queries.

---

## 3. Per-endpoint baseline (current tree)

| Endpoint | Queries | Wall (SQLite) | Comment |
| --- | --- | --- | --- |
| `POST /api/auth/login/` | 8 | 224.8 ms | PBKDF2 password hashing dominates; queries are trivial. Throttled at 5/min. |
| `POST /api/admin/users/` (create staff) | 15 | 230.5 ms | includes password hashing for the new account |
| `POST …/biometrics/reception-terminal/` `ISSUE_CODE` | 21 | 9.8 ms | creates attendance + credential + audit atomically |
| `GET /api/admin/dashboard/` | 34 | 76.6 ms | 14 of the queries are the two ledger projections (§4) |
| `GET /api/admin/bookings/` | 5 | 24.7 ms | |
| `GET /api/admin/users/` | 2 | 9.8 ms | |
| `GET /api/admin/staff-operations/profiles/` | 4 | 17.0 ms | fixed (§2) |
| `GET /api/admin/staff-operations/attendance/` (team, 100 rows) | 2 | 6.4 ms | |
| `GET …/attendance/?open=true` | 2 | 5.3 ms | |
| `GET /api/admin/users/directory/` | 2 | 5.5 ms | |
| `GET /api/admin/pos/menu/` | 1 | 1.5 ms | |
| `GET /api/admin/inventory/items/` | 1 | 1.9 ms | |
| `GET /api/auth/profile/` | 0 | 1.9 ms | served from the session/JWT payload |
| Reception terminal `GET` (desk key) | 8 | 7.9 ms | includes liveness write when stale |

**Per-request overhead** — `WorkstationJWTAuthentication` resolves the
workstation credential with one `select_related` query per authenticated
request. That is the floor for every staff API call and is acceptable.

Pagination is server-side (`PAGE_SIZE=10`, max 100). No endpoint measured here
loads an unbounded collection except the ones that are inherently bounded by
configuration (POS menu, inventory items) and the terminal's active-attendance
list (currently on duty only).

---

## 4. Dashboard ledger projection — measured, deliberately not changed

`reports/services/dashboard.py` calls `financial_summary()` twice:

| Call | Queries | Wall |
| --- | --- | --- |
| `ledger` (today only) | 7 | 27.0 ms |
| `ledger_month` (month to date) | 7 | 26.9 ms |
| **Total** | **14 of 34** | **≈54 of 76.6 ms (≈70 %)** |

The two ranges overlap: today ⊂ month-to-date. On the 1st of the month they are
the *same* query with the same parameters.

**Why it was not de-duplicated in this audit.** `financial_summary()` is the
accounting projection that the Accounting page consumes; it returns totals,
per-day rows and a per-revenue-account breakdown. Deriving the day payload from
the month payload changes that contract (per-day rows do not carry transaction
counts, and the day breakdown is not a subset of the month breakdown) and risks
a financial reporting error for a ~27 ms local saving. The audit's rule is that
performance never buys correctness.

**Smallest safe change if it is wanted later:**

1. add an opt-in keyword (`include_breakdowns=False`) used only by the dashboard's
   day call, dropping the per-account aggregate — saves ~2 of 14 queries;
2. or compute both ranges in a single conditional aggregate (`CASE WHEN
   business_date = today`) and keep the existing serializers — saves ~6 of 14;
3. or cache the month summary for 60 s keyed by the ledger's last posted
   transaction id — requires explicit invalidation on posting, which is the
   reason it is not recommended as a first step.

Acceptance for any of these: identical JSON (field by field) against the
current implementation on a seeded ledger, plus the accounting tests.

---

## 5. Frontend

| Area | Size | Observation |
| --- | --- | --- |
| Images / assets | 2.1 MB (1.9 MB images) | largest single cost on first load; served with far-future caching |
| CSS | 240 KB (`dashboard.css` 105 KB) | one shared sheet, no per-page split |
| JS | 556 KB across 21 516 lines | 13 external versioned scripts at the end of `<body>` |
| `accounting.html` | 106 KB | mostly inline `<script>` |
| `pos.html` | 83 KB | inline |
| `inventory.html` | 72 KB | inline |
| `payroll.html` | 66 KB | inline |
| `workforce.html` | 61 KB | inline (this audit edited it; net +23 lines) |

* Scripts are correctly placed at the end of the document and carry a version
  query (`?v=1.1.23`), so caching is safe across deploys.
* The shell renders the sidebar from a per-role `sessionStorage` snapshot and
  reconciles once after the capability read — no layout thrash, no re-render
  loop (verified in `frontend/js/dashboard.js`).
* The dashboard's own polling is limited to notifications/approvals badges and a
  `/version.json` check every 15 minutes (5 on focus) that never auto-reloads.
* **No new framework, bundler, dependency or build step was introduced.**

**Biggest real-world win (unverified in production):** move the inline page
script/style into the shared bundle. `accounting.html` and `pos.html` alone ship
more bytes than every external JS file combined, and they are fetched on every
visit unlike the cached bundle.

---

## 6. Database

* 22 apps; no index was added by this audit because no measured query lacked one
  (every measured list endpoint ran in 1–5 queries with bounded scans).
* **Unverified:** `EXPLAIN` on MySQL was not possible (no client in the audit
  container). If production feels slow at scale, capture `EXPLAIN ANALYZE` for
  the dashboard, attendance list and bookings list before adding any index —
  index changes without a plan are guesswork.
* Attendance, cash sessions, payroll and folio posting use `select_for_update`
  inside `transaction.atomic`; that costs a row lock but prevents double posting.
  It was not removed for speed.

---

## 7. Summary

| Question | Answer |
| --- | --- |
| Was a real bottleneck fixed? | Yes — staff profile list: 82 → 4 queries, 52.1 → 17.0 ms (SQLite, 40 staff). |
| Is the backend systemically slow? | Not on this evidence. Most list endpoints are 1–6 queries; the dashboard's cost is two accounting projections, and login's cost is password hashing. |
| Is the frontend the likelier cause of "slow" on real connections? | **Probably** — 2.1 MB of assets and 60–106 KB HTML pages with large inline scripts; unverified against production RUM because none was available. |
| Any measurement fabricated? | No. Everything in this document was produced by the harness described in §1; nothing is an estimate presented as a measurement. |
