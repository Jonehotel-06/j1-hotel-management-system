# Changes

Purchase-order detail serialization now includes `supplier_details`, the existing supplier representation (reference, name, status, contact, email, phone, address, payment terms, tax reference and notes). It uses the already-related supplier and adds no database query or migration. This is additive; existing response fields/contracts remain unchanged.

Source inspection confirms the existing Accounting hand-off detail route is keyed by PO reference and existing serializers expose order totals, paid/outstanding amounts, payment history, line items, receipt summaries and approval fields. The reported “Purchase order unavailable” path was not verified because tests could not start in this environment. Do not treat the underlying reported error as conclusively fixed.

Approval history/submission timestamp: the current `PurchaseOrder` schema stores current approval (`approved_by`, `approved_at`) but has no append-only approval decision history or submission timestamp/submitter field. A trustworthy history requires a persisted audit/history design and migration; none is fabricated here.
