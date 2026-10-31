# Housekeeping workflow and API

## Staff process
1. A manager defines each item and its category on the Housekeeping page. Mark toiletries and other consumed supplies CONSUMABLE. Configure only reusable hotel-owned assets, first by room type, then override individual rooms as needed (zero excludes). The system cannot determine whether a name represents a consumable; the manager must classify it correctly.
2. Assign the physical room to the booking in the existing reservation workflow before check-in. Housekeeping opens the pending pre-check-in checklist, checks every item, enters actual quantities and good/damaged condition, optionally records remarks/evidence reference and saves progress or completes. The system stamps the completing staff member and time. Present/partial/missing are derived from quantities; damage is separate.
3. After the existing checkout workflow has created and closed a matching `StayRoom`, open the post-checkout checklist. The before quantities, condition, remarks, reference, inspector and time are shown. Count actual items, mark damage/replacement, and complete. A missing quantity is `max(before - after, 0)` for POST and `max(expected - actual, 0)` for PRE. A difference from expected, damage, replacement, or shortfall is highlighted for management even where the post missing quantity is zero.
4. Management records investigation and resolution explanations. Each review is appended to history; resolved cases may be reopened by a further investigating event. Never infer individual responsibility from a booking, which may have multiple occupants. Any subsequent guest charge must be independently verified and processed via the existing authorized financial workflow; no automatic guest or staff financial action is performed.

## Backend access
Routes use the existing configurable API origin and are all under `/api/admin/housekeeping/assets/`. Responses use the existing success envelope. Collection pagination uses `page` and `page_size` (max 100). API requires active accounts, valid department context, appropriate server-side role and capability. Housekeeping and management roles need `housekeeping.task.manage` for inspections. ADMIN, MANAGER, GENERAL_MANAGER or HOUSEKEEPING_MANAGER with `housekeeping.task.assign` may configure and review. Receptionists and other staff cannot use these asset endpoints merely because they can manage housekeeping tasks. Existing task permissions are unchanged.

| Method and path | Purpose / input |
|---|---|
| GET `catalog/` | Catalog including category. |
| POST `catalog/` | Manager: `{ "name": "Hangers", "category": "REUSABLE" }` or `CONSUMABLE`. Existing normalized name is returned; changing category by reusing the name is rejected. |
| GET `configuration/` | Manager: type and room expectations plus choices. |
| POST `configuration/` | Manager: `{ "scope": "type" or "room", "scope_id": 1, "item_id": 1, "quantity": 5 }`. IDs here are examples only. Consumables are rejected. |
| GET `queue/` | Pending physical-room assignments; records with no pre evidence are marked for escalation. |
| POST `inspections/` | `{ "assignment_id": 1, "phase": "PRE" or "POST" }`. Starts a snapshot or returns the existing one for safe retry. |
| GET `inspections/<id>/` | Snapshot, draft, completion and lines. |
| POST `inspections/<id>/` | `{ "revision": 0, "complete": true, "lines": [{"item_id": 1, "actual": 5, "condition": "GOOD", "replaced": false, "remarks": "", "evidence": ""}] }`. `condition` is GOOD or DAMAGED. Drafts may contain a subset; completion must contain every distinct snapshot item. Revision conflicts and completion retries return a validation error; GET the existing record to confirm state. |
| GET `discrepancies/` | Manager: paginated flagged lines and full review history. |
| POST `discrepancies/<line_id>/reviews/` | Manager: `{ "status": "INVESTIGATING" or "RESOLVED", "explanation": "..." }`. A new event is appended. |

No URLs are hardcoded to an API origin. Evidence is a textual reference only, not uploaded media. Stay reference is available once a stay exists; before arrival the booking and room-assignment references are the link. Pre-inspections attach to booking-room assignments because this repository creates operational stays at arrival, not at reservation time.
