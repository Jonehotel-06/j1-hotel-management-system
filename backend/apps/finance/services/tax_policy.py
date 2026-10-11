"""Customer-billing tax removal and the separate statutory-tax policy.

Owner-approved policy (2026-10-11)
=================================
Customers of J-ONE HOTEL & LODGE are no longer charged an additional tax on
top of approved selling prices.  For every NEW customer-facing transaction
(booking quotations, reservations, POS / restaurant / bar / room-service
orders, folio charges, deposits, receipts and checkout balances) the amount
charged is::

    approved selling prices + permitted charges (e.g. service fee)
        - permitted discounts

with NO appended tax amount.  An approved price of ₦10,000 is charged as
₦10,000.

Historical transactions are NOT rewritten: bookings, POS orders, receipts,
folios, ledger postings and statutory records created before this change keep
their original ``tax_amount`` snapshots and continue to report the tax they
actually carried.

Statutory boundary
==================
Removing a charge from the guest's bill does not remove the hotel's own legal
obligations.  This module therefore keeps a STATUTORY tax calculation that is
deliberately separate from customer billing:

* It is never added to any guest-facing total, receipt, folio, Paystack
  amount or checkout balance.
* When enabled (the default) it accrues the potential liability to
  ``LIABILITY.STATUTORY_TAX_PAYABLE`` against
  ``EXPENSE.STATUTORY_TAX_ABSORBED`` so the ledger shows the exposure without
  charging the guest.
* The current research baseline is Nigeria's Value Added Tax at 7.5%
  (Value Added Tax Act as amended by the Finance Act 2019; retained by the
  Nigeria Tax Act 2025 in force from 2026-01-01).  Hotel accommodation and
  food & beverage sales are standard-rated supplies; the zero-rated list
  covers basic foods, medicines, books, tuition and residential rent — not
  hospitality services.
* Whether the hotel must actually register and remit VAT (small-company
  relief applies below the Nigeria Tax Act 2025 turnover thresholds), whether
  prices are treated as tax-inclusive or tax-exclusive, and any state-level
  hospitality levies are OWNER / PROFESSIONAL decisions.  The
  ``statutory_tax_basis`` setting records that decision instead of silently
  assuming one; both bases are implemented and the default
  (``EXCLUSIVE``) simply mirrors the pre-change price semantics — no price is
  silently converted between tax-exclusive and tax-inclusive.
* A nil or disabled switch does not delete history: it only stops NEW
  accruals.  Reports distinguish historical tax collected (LIABILITY.TAX_PAYABLE),
  current customer charges (never taxed), and statutory accruals
  (LIABILITY.STATUTORY_TAX_PAYABLE).

Payroll obligations (PAYE, pension, NSITF/ECA, NHF) are untouched by the
customer-billing tax removal and are handled by the payroll statutory rules.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
_HUNDRED = Decimal("100")

# Explicit, single source of truth for new customer billing.  Everything that
# creates a customer charge must call this instead of re-deriving tax, so a
# future change cannot quietly re-introduce a billed tax in one corner.
CUSTOMER_BILLED_TAX = ZERO


def customer_tax_amount() -> Decimal:
    """Tax appended to a NEW customer charge.  Always zero, by owner policy."""
    return CUSTOMER_BILLED_TAX


def _quantize(value: Decimal) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


class StatutoryTaxPolicy:
    """Resolved view of the statutory-tax configuration for one calculation."""

    def __init__(self, *, enabled: bool, rate_percent: Decimal, basis: str, currency: str = "NGN"):
        self.enabled = bool(enabled)
        self.rate_percent = Decimal(rate_percent or ZERO)
        self.basis = basis if basis in ("EXCLUSIVE", "INCLUSIVE") else "EXCLUSIVE"
        self.currency = currency

    def amount_for(self, *, net_amount: Decimal, gross_amount: Decimal | None = None) -> Decimal:
        """Return the statutory tax (NOT billed to the guest) for one sale.

        ``net_amount`` is the customer-charged amount excluding any historical
        customer-billed tax.  ``gross_amount`` defaults to ``net_amount``.

        * ``EXCLUSIVE`` — statutory tax is ``rate%`` of the net amount.  This
          mirrors the old tax-exclusive price semantics (the approved price is
          unchanged; the tax exposure sits with the hotel).
        * ``INCLUSIVE`` — the customer amount is deemed to already contain the
          statutory tax; the liability is extracted as ``rate / (100 + rate)``
          of the gross.  Only use this after an owner/accountant decision —
          switching bases is exactly the silent conversion the policy forbids,
          so it must be an explicit, recorded choice.
        """
        if not self.enabled or self.rate_percent <= ZERO:
            return ZERO
        net = _quantize(net_amount or ZERO)
        if net <= ZERO:
            return ZERO
        if self.basis == "INCLUSIVE":
            gross = _quantize(gross_amount if gross_amount is not None else net)
            if gross <= ZERO:
                return ZERO
            return _quantize(gross * self.rate_percent / (_HUNDRED + self.rate_percent))
        return _quantize(net * self.rate_percent / _HUNDRED)

    def describe(self) -> dict:
        return {
            "enabled": self.enabled,
            "rate_percent": str(self.rate_percent),
            "basis": self.basis,
            "customer_billed_tax": str(CUSTOMER_BILLED_TAX),
            "note": "Statutory tax is accrued for reporting/remittance only and is never billed to guests.",
        }


def statutory_policy(settings_obj=None) -> StatutoryTaxPolicy:
    """Load the policy from HotelSettings (cached singleton)."""
    if settings_obj is None:
        from apps.hotel.models import HotelSettings

        settings_obj = HotelSettings.get_settings()
    return StatutoryTaxPolicy(
        enabled=getattr(settings_obj, "statutory_tax_enabled", True),
        rate_percent=getattr(settings_obj, "statutory_tax_rate_percent", Decimal("7.50")),
        basis=getattr(settings_obj, "statutory_tax_basis", "EXCLUSIVE"),
        currency=getattr(settings_obj, "currency", "NGN"),
    )


def statutory_ledger_lines(*, net_amount: Decimal, reference: str, settings_obj=None) -> list[dict]:
    """Balanced ledger lines for the statutory accrual (expense + liability).

    Returns an empty list when the policy is disabled or the amount is nil.
    These lines never touch a guest receivable or any guest-facing total.
    """
    from apps.finance.services import accounting

    policy = statutory_policy(settings_obj)
    amount = policy.amount_for(net_amount=net_amount)
    if amount <= ZERO:
        return []
    return [
        {
            "account_code": accounting.STATUTORY_TAX_ABSORBED,
            "direction": "DEBIT",
            "amount": amount,
            "description": f"Statutory tax absorbed (not billed to guests) {reference}",
        },
        {
            "account_code": accounting.STATUTORY_TAX_PAYABLE,
            "direction": "CREDIT",
            "amount": amount,
            "description": f"Statutory tax accrued (not billed to guests) {reference}",
        },
    ]
