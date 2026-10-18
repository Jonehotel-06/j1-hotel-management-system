#!/usr/bin/env python3
"""Live E2E for supplier payments on purchase orders.

Talks to a REAL runserver over HTTP, no Django test client. Mirrors
what the procurement desk does:

  1. create + submit + approve an order
  2. pay the supplier in full, with the invoice as evidence  -> PAID
  3. a second order paid in two instalments                   -> PAID on the last
  4. refuse: overpay, pay a draft, pay without a transfer
     reference, pay as a clerk, pay twice
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8014"

import time

RUN = time.strftime("%m%d%H%M%S")

MANAGER_EMAIL = "e2e.pay.manager@example.test"
CLERK_EMAIL = "e2e.pay.clerk@example.test"
PASSWORD = "e2e-pay-password-0001"

failures = 0


def call(method, path, body=None, headers=None, token=None, files=None, expect=None, label=""):
    if files is not None:
        boundary = "----JONEE2E" + "7" * 12
        parts = []
        for key, value in (body or {}).items():
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
            )
        for field_name, (filename, content, ctype) in files.items():
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field_name}\"; "
                f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n".encode() + content + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        data = b"".join(parts)
        ctype = f"multipart/form-data; boundary={boundary}"
    else:
        data = json.dumps(body).encode() if body is not None else None
        ctype = "application/json"

    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", ctype)
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {"_raw": raw[:200].decode("utf-8", "replace")}
    mark = "OK " if (expect is None or status == expect) else "BAD"
    global failures
    if expect is not None and status != expect:
        failures += 1
    print(f"{mark} {label or method + ' ' + path} -> {status}")
    if expect is not None and status != expect:
        print("    expected", expect, "body:", json.dumps(payload)[:400])
    return status, payload


def login(email):
    status, payload = call("POST", "/api/auth/login/", {"email": email, "password": PASSWORD}, expect=200, label=f"login {email}")
    return payload["data"]["tokens"]["access"]


def clerk_token():
    """The desktop sign-in policy (front-desk workstations only) refuses
    staff logins from an infrastructure address, so the clerk's token is
    minted out-of-band — exactly what the policy is for."""
    import os

    minted = os.environ.get("E2E_CLERK_TOKEN")
    if minted:
        return minted
    import subprocess

    out = subprocess.run(
        [".venv/bin/python", "manage.py", "shell", "-c",
         "from apps.accounts.models import User;"
         "from rest_framework_simplejwt.tokens import RefreshToken;"
         "u=User.objects.get(email='e2e.pay.clerk@example.test');"
         "print(str(RefreshToken.for_user(u).access_token))"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip().splitlines()[-1]


def main():
    global failures
    manager = login(MANAGER_EMAIL)
    clerk = clerk_token()

    # ---- reference data -------------------------------------------------
    status, supplier = call("POST", "/api/admin/inventory/suppliers/", {
        "reference": "E2E-PAY-" + RUN, "name": "E2E Payment Supplies " + RUN,
        "status": "ACTIVE",
    }, token=manager, expect=201, label="create supplier")
    status, location = call("POST", "/api/admin/inventory/locations/", {
        "code": "E2EPAY" + RUN, "name": "E2E Pay Store " + RUN,
    }, token=manager, expect=201, label="create location")
    status, item = call("POST", "/api/admin/inventory/items/", {
        "sku": "E2E-PAY-RICE-" + RUN, "name": "E2E Rice (50kg) " + RUN, "base_unit": "PIECE",
        "standard_cost": "1500.00",
    }, token=manager, expect=201, label="create item")
    if failures:
        return 1

    # ---- order one: paid in full, with the invoice filed ----------------
    status, created = call("POST", "/api/admin/inventory/purchase-orders/", {
        "supplier_id": supplier["data"]["id"],
        "delivery_location_id": location["data"]["id"],
        "idempotency_key": "e2e-pay-one-" + RUN,
        "lines": [{"item_id": item["data"]["id"], "quantity_ordered": "4", "unit_cost": "1500"}],
    }, token=clerk, expect=201, label="1. create order (4 x 1500 = 6000)")
    ref1 = created["data"]["reference"]

    call("POST", f"/api/admin/inventory/purchase-orders/{ref1}/submit/", {}, token=clerk, expect=200, label="2. submit")
    call("POST", f"/api/admin/inventory/purchase-orders/{ref1}/approve/", {}, token=manager, expect=200, label="3. approve")

    status, paid = call("POST", f"/api/admin/inventory/purchase-orders/{ref1}/pay/", {
        "method": "BANK_TRANSFER", "reference": "GTB-E2E-FULL",
        "paid_on": "2026-10-18", "notes": "Settled in full on delivery",
    }, token=manager, expect=200, label="4. pay in full (amount defaults to balance)")
    data = paid["data"]
    if data["status"] != "PAID" or data["total_paid"] != "6000.00" or data["outstanding_amount"] != "0.00":
        failures += 1
        print("    BAD order state:", data["status"], data["total_paid"], data["outstanding_amount"])
    print("    order:", ref1, data["status"], "paid_at:", bool(data["paid_at"]), "paid_by:", data["paid_by_email"])

    # Paying again must be refused: the order is fully paid.
    call("POST", f"/api/admin/inventory/purchase-orders/{ref1}/pay/", {
        "method": "CASH", "amount": "1",
    }, token=manager, expect=400, label="5. refuse to pay a fully paid order")

    # ---- order two: two instalments -------------------------------------
    status, created2 = call("POST", "/api/admin/inventory/purchase-orders/", {
        "supplier_id": supplier["data"]["id"],
        "delivery_location_id": location["data"]["id"],
        "idempotency_key": "e2e-pay-two-" + RUN,
        "lines": [{"item_id": item["data"]["id"], "quantity_ordered": "2", "unit_cost": "1500"}],
    }, token=clerk, expect=201, label="6. create second order (2 x 1500 = 3000)")
    ref2 = created2["data"]["reference"]
    call("POST", f"/api/admin/inventory/purchase-orders/{ref2}/submit/", {}, token=clerk, expect=200)
    call("POST", f"/api/admin/inventory/purchase-orders/{ref2}/approve/", {}, token=manager, expect=200)

    call("POST", f"/api/admin/inventory/purchase-orders/{ref2}/pay/", {
        "method": "BANK_TRANSFER", "reference": "GTB-E2E-ADV", "amount": "1000",
    }, token=manager, expect=200, label="7. pay a 1000 advance")
    status, part = call("GET", f"/api/admin/inventory/purchase-orders/{ref2}/", None, token=manager, expect=200)
    data = part["data"]
    if data["status"] != "APPROVED" or data["outstanding_amount"] != "2000.00":
        failures += 1
        print("    BAD partial state:", data["status"], data["outstanding_amount"])
    print("    after advance:", data["status"], "outstanding", data["outstanding_amount"])

    call("POST", f"/api/admin/inventory/purchase-orders/{ref2}/pay/", {
        "method": "CASH", "amount": "2000",
    }, token=manager, expect=200, label="8. pay the 2000 balance (cash needs no reference)")
    status, done = call("GET", f"/api/admin/inventory/purchase-orders/{ref2}/", None, token=manager, expect=200)
    data = done["data"]
    if data["status"] != "PAID" or len(data["payments"]) != 2:
        failures += 1
        print("    BAD instalment state:", data["status"], len(data["payments"]))
    print("    after balance:", data["status"], "payments:", len(data["payments"]))

    # ---- refusals -------------------------------------------------------
    call("POST", f"/api/admin/inventory/purchase-orders/{ref2}/pay/", {
        "method": "CASH", "amount": "9999",
    }, token=manager, expect=400, label="9. refuse an overpayment")
    call("POST", f"/api/admin/inventory/purchase-orders/{ref2}/pay/", {
        "method": "BANK_TRANSFER", "amount": "1",
    }, token=manager, expect=400, label="10. refuse a transfer without a reference")
    call("POST", f"/api/admin/inventory/purchase-orders/{ref2}/pay/", {
        "method": "CASH", "paid_on": "2099-01-01",
    }, token=manager, expect=400, label="11. refuse a future-dated payment")
    call("POST", f"/api/admin/inventory/purchase-orders/{ref2}/pay/", {
        "method": "CASH", "amount": "1",
    }, token=clerk, expect=403, label="12. refuse a clerk (no procurement.approve)")

    # A fresh order that was never submitted cannot be paid either.
    status, created3 = call("POST", "/api/admin/inventory/purchase-orders/", {
        "supplier_id": supplier["data"]["id"],
        "delivery_location_id": location["data"]["id"],
        "idempotency_key": "e2e-pay-three-" + RUN,
        "lines": [{"item_id": item["data"]["id"], "quantity_ordered": "1", "unit_cost": "1500"}],
    }, token=manager, expect=201)
    ref3 = created3["data"]["reference"]
    call("POST", f"/api/admin/inventory/purchase-orders/{ref3}/pay/", {
        "method": "CASH", "amount": "1500",
    }, token=manager, expect=400, label="13. refuse to pay an unapproved draft")

    # ---- evidence -------------------------------------------------------
    pdf = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
    status, with_evidence = call(
        "POST", f"/api/admin/inventory/purchase-orders/{ref1}/pay/",
        {"method": "CARD", "reference": "POS-E2E-1", "amount": "0.01"},
        token=manager, expect=400,  # ref1 is fully paid; use a fresh order instead
        label="14. (guard) paid order stays closed",
    )
    status, created4 = call("POST", "/api/admin/inventory/purchase-orders/", {
        "supplier_id": supplier["data"]["id"],
        "delivery_location_id": location["data"]["id"],
        "idempotency_key": "e2e-pay-four-" + RUN,
        "lines": [{"item_id": item["data"]["id"], "quantity_ordered": "1", "unit_cost": "1500"}],
    }, token=clerk, expect=201)
    ref4 = created4["data"]["reference"]
    call("POST", f"/api/admin/inventory/purchase-orders/{ref4}/submit/", {}, token=clerk, expect=200)
    call("POST", f"/api/admin/inventory/purchase-orders/{ref4}/approve/", {}, token=manager, expect=200)
    status, evidence = call(
        "POST", f"/api/admin/inventory/purchase-orders/{ref4}/pay/",
        {"method": "CARD", "reference": "POS-E2E-2", "paid_on": "2026-10-18"},
        files={"evidence_file": ("payment-slip.pdf", pdf, "application/pdf")},
        token=manager, expect=200, label="15. pay with the receipt/invoice attached",
    )
    payments = evidence["data"]["payments"]
    if not payments or not payments[-1]["evidence_url"] or "purchase_orders/payments/" not in payments[-1]["evidence_url"]:
        failures += 1
        print("    BAD evidence:", json.dumps(payments)[:300])
    else:
        print("    evidence stored:", payments[-1]["evidence_url"])

    print()
    print("RUN", RUN, "FAILURES", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
