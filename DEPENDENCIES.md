# Dependencies and integration

No new package or framework. Uses the existing Django REST Framework serializer and `SupplierSerializer` in `backend/apps/inventory/serializers.py`.

Migration required: **No**.
API change: additive `supplier_details` field on purchase-order detail responses.

Safe integration: apply only `backend/apps/inventory/serializers.py` from this package to the target branch. Run backend tests and inspect the Accounting detail panel. This package does not change permissions, financial arithmetic, workflow transitions, payment recording, or receiving operations.
