# apps/finance/management/commands/guest_receivable_check.py
"""Explain the guest-receivable balance, folio by folio. READ-ONLY.

Owner question: the balance sheet shows **Guest receivables** as a credit
(negative asset) — where does that come from, and how is it corrected?

Guest receivables is a debit-normal asset account. It is debited when a guest
is charged (accommodation nights, POS sales and room-charge laundry all post
through it) and credited when money is applied to the guest's folio (payment
allocations) or when a POS sale is tendered at the counter. The folio posting
ledger is derived from those same receivable lines, so:

    the sum of every folio balance  ==  the guest-receivable account balance

A credit balance therefore means some folios carry more credit applied than
charges posted — normally an advance payment whose stay never consumed the
nights it paid for: a cancelled booking, a no-show, an early check-out, or a
rate/discount change applied after the payment was allocated. Nothing about
that is a display fault, and nothing may be edited after posting, so the only
supported ways back to zero are:

  1. the existing refund workflow (payment refund request -> approval), which
     posts an immutable debit against guest receivables, or
  2. a balanced **reclassification** journal -- Debit ASSET.GUEST_RECEIVABLE /
     Credit LIABILITY.GUEST_DEPOSITS -- posted through Accounting ->
     "Post journal entry", moving the credit to the liability account that
     actually describes money the hotel is holding for a guest.

The report also splits the account by transaction currency. The statements
present one functional currency, so receivable lines posted in another currency
would inflate the same figure; the command says so explicitly instead of
letting a mixed-currency total pass as a naira balance.

This command only reports. It never writes: no postings, no refunds, no
corrections.

Examples
--------
    python manage.py guest_receivable_check
    python manage.py guest_receivable_check --as-of 2026-10-16 --json
    python manage.py guest_receivable_check --folio FOL-J1-20260901-ABC123
    python manage.py guest_receivable_check --fail-on-credit
"""
import json
from datetime import date as _date
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Sum

from apps.core.utils import hotel_today
from apps.finance.models import FinancialLine, FinancialTransaction, Folio, FolioPosting
from apps.finance.services import accounting

ZERO = Decimal("0.00")
CENT = Decimal("0.01")
MAX_LIMIT = 500


def _dec(value) -> Decimal:
    return Decimal(value or 0).quantize(CENT)


class Command(BaseCommand):
    help = "Report the guest-receivable balance and the folios behind it (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--as-of", metavar="YYYY-MM-DD", dest="as_of",
                            help="Business date to report through (default: today, hotel time).")
        parser.add_argument("--folio", metavar="REFERENCE",
                            help="Report one folio's postings in full detail.")
        parser.add_argument("--limit", type=int, default=50,
                            help=f"Maximum number of folios to list (1-{MAX_LIMIT}, default 50).")
        parser.add_argument("--json", action="store_true", dest="as_json",
                            help="Emit machine-readable JSON.")
        parser.add_argument("--fail-on-credit", action="store_true",
                            help="Exit non-zero when the receivable is credit-normal.")

    # ---------------------------------------------------------------- helpers
    def _through(self, value):
        if not value:
            return hotel_today()
        try:
            return _date.fromisoformat(str(value))
        except ValueError as exc:
            raise CommandError(f"--as-of must be YYYY-MM-DD: {value}") from exc

    def _posted_lines(self, through):
        return FinancialLine.objects.filter(
            transaction__status=FinancialTransaction.Status.POSTED,
            transaction__business_date__lte=through,
            account_code=accounting.ACCOUNTS_RECEIVABLE,
        )

    def _account_totals(self, through):
        debits = credits = ZERO
        for row in self._posted_lines(through).values("direction").annotate(total=Sum("amount")):
            amount = _dec(row["total"])
            if row["direction"] == FinancialLine.Direction.DEBIT:
                debits += amount
            else:
                credits += amount
        return debits, credits

    def _by_transaction_type(self, through):
        buckets = []
        rows = (
            self._posted_lines(through)
            .values("transaction__type", "direction")
            .annotate(total=Sum("amount"))
            .order_by("transaction__type", "direction")
        )
        for row in rows:
            buckets.append({
                "type": row["transaction__type"],
                "direction": row["direction"],
                "amount": str(_dec(row["total"])),
            })
        return buckets

    def _currency_split(self, through):
        """Receivable movement per transaction currency.

        The balance sheet reports one functional currency. Anything posted in
        another currency is reported here rather than silently added to it.
        """
        rows = (
            self._posted_lines(through)
            .values("transaction__currency", "direction")
            .annotate(total=Sum("amount"))
        )
        split: dict[str, dict] = {}
        for row in rows:
            currency = str(row["transaction__currency"] or "NGN").upper()
            entry = split.setdefault(currency, {"currency": currency, "debits": ZERO, "credits": ZERO})
            amount = _dec(row["total"])
            if row["direction"] == FinancialLine.Direction.DEBIT:
                entry["debits"] += amount
            else:
                entry["credits"] += amount
        result = []
        for entry in split.values():
            result.append({
                "currency": entry["currency"],
                "debits": str(entry["debits"]),
                "credits": str(entry["credits"]),
                "balance": str(entry["debits"] - entry["credits"]),
            })
        result.sort(key=lambda row: row["currency"])
        return result

    def _folio_rows(self, through, folio_reference=None, limit=MAX_LIMIT):
        postings = FolioPosting.objects.select_related("folio", "folio__booking", "folio__guest")
        if folio_reference:
            postings = postings.filter(folio__reference=folio_reference)
        else:
            postings = postings.filter(business_date__lte=through)
        entries: dict[int, dict] = {}
        for posting in postings.order_by("folio_id", "business_date", "pk"):
            entry = entries.setdefault(posting.folio_id, {
                "reference": posting.folio.reference,
                "status": posting.folio.status,
                "booking_reference": getattr(posting.folio.booking, "booking_reference", ""),
                "guest": getattr(posting.folio.guest, "full_name", "") or "",
                "charges": ZERO,
                "credits": ZERO,
                "postings": [],
            })
            amount = _dec(posting.amount)
            if posting.effect == FolioPosting.Effect.DEBIT:
                entry["charges"] += amount
            else:
                entry["credits"] += amount
            entry["postings"].append({
                "business_date": posting.business_date.isoformat(),
                "kind": posting.kind,
                "effect": posting.effect,
                "amount": str(amount),
                "source_reference": posting.source_reference,
                "description": posting.description,
            })
        rows = []
        for entry in entries.values():
            balance = entry["charges"] - entry["credits"]
            entry["balance"] = str(balance)
            entry["in_credit"] = balance < ZERO
            # Serialise the money as fixed-point strings: the JSON report is read
            # by bookkeeping tools, so it never carries a float.
            entry["charges"] = str(entry["charges"])
            entry["credits"] = str(entry["credits"])
            rows.append(entry)
        rows.sort(key=lambda row: Decimal(row["balance"]))
        credit_rows = [row for row in rows if row["in_credit"]]
        return rows, credit_rows[: max(1, min(int(limit or 50), MAX_LIMIT))]

    # ------------------------------------------------------------------ handle
    def handle(self, *args, **options):
        through = self._through(options.get("as_of"))
        limit = max(1, min(int(options.get("limit") or 50), MAX_LIMIT))
        folio_reference = options.get("folio")
        if folio_reference and not Folio.objects.filter(reference=folio_reference).exists():
            raise CommandError(f"Unknown folio reference: {folio_reference}")

        debits, credits = self._account_totals(through)
        balance = debits - credits
        rows, credit_rows = self._folio_rows(through, folio_reference=folio_reference, limit=limit)
        currencies = self._currency_split(through)
        folio_total = sum((Decimal(row["balance"]) for row in rows), ZERO)
        listed = rows if folio_reference else credit_rows

        payload = {
            "as_of": through.isoformat(),
            "account_code": accounting.ACCOUNTS_RECEIVABLE,
            "debits": str(debits),
            "credits": str(credits),
            "balance": str(balance),
            "credit_normal": balance < ZERO,
            "folio_balance_total": str(folio_total),
            "folio_count": len(rows),
            "folios_in_credit": len([row for row in rows if row["in_credit"]]),
            "folios": listed,
            "movements_by_transaction_type": self._by_transaction_type(through),
            "currencies": currencies,
            "mixed_currency": len(currencies) > 1,
            "warnings": (
                ["Receivable lines exist in more than one currency ("
                 + ", ".join(row["currency"] for row in currencies)
                 + "); the statements report a single functional currency, so the balance above mixes them. "
                 "Do not correct this with a journal — report it to the accountant for a currency "
                 "revaluation, and check the booking/payment currency at the desk."]
                if len(currencies) > 1 else []
            ),
            "correction_routes": [
                "Refund the guest balance through the existing refund request/approval workflow "
                "(posts an immutable debit against guest receivables).",
                "Or post a balanced reclassification journal through Accounting -> Post journal entry: "
                f"Debit {accounting.ACCOUNTS_RECEIVABLE}, Credit {accounting.GUEST_DEPOSITS} "
                "(moves money held for a guest to the account that describes it).",
                "Posted transactions are never edited or deleted; both routes add evidence.",
            ],
        }

        if options.get("as_json"):
            self.stdout.write(json.dumps(payload, indent=2))
        else:
            self._write_report(payload, folio_reference)

        if options.get("fail_on_credit") and payload["credit_normal"]:
            raise CommandError(
                f"Guest receivables are credit-normal by {balance}; "
                f"{payload['folios_in_credit']} folio(s) carry a credit balance."
            )

    def _write_report(self, payload, folio_reference):
        style = self.style
        self.stdout.write(style.MIGRATE_HEADING(
            f"Guest receivables ({payload['account_code']}) through {payload['as_of']}"))
        self.stdout.write(f"  debits   guest charges posted      {payload['debits']}")
        self.stdout.write(f"  credits  applied to guest folios   {payload['credits']}")
        self.stdout.write(f"  balance                           {payload['balance']}"
                          + ("   <- CREDIT-NORMAL" if payload["credit_normal"] else ""))
        self.stdout.write(f"  folio balances total              {payload['folio_balance_total']}"
                          f"  ({payload['folio_count']} folio(s), "
                          f"{payload['folios_in_credit']} in credit)")

        self.stdout.write("")
        self.stdout.write(style.MIGRATE_HEADING("Receivable movement by currency"))
        for row in payload["currencies"]:
            self.stdout.write(f"  {row['currency']}  debits {row['debits']:>14}  credits "
                              f"{row['credits']:>14}  balance {row['balance']:>14}")

        if payload["warnings"]:
            self.stdout.write("")
            for warning in payload["warnings"]:
                self.stdout.write(style.ERROR(f"WARNING: {warning}"))

        self.stdout.write("")
        self.stdout.write(style.MIGRATE_HEADING("Receivable movements by transaction type"))
        for row in payload["movements_by_transaction_type"]:
            self.stdout.write(f"  {row['type']:<22} {row['direction']:<7} {row['amount']}")

        self.stdout.write("")
        heading = "Selected folio" if folio_reference else "Folios carrying a credit balance"
        self.stdout.write(style.MIGRATE_HEADING(heading))
        if not payload["folios"]:
            self.stdout.write("  None. Every folio's charges are covered by money applied to it.")
        for row in payload["folios"]:
            self.stdout.write(
                f"  {row['reference']}  {row['guest'][:26]:<26} {row['status']:<8} "
                f"charges {row['charges']:>12}  credits {row['credits']:>12}  balance {row['balance']:>12}"
            )
            if folio_reference:
                for posting in row["postings"]:
                    self.stdout.write(
                        f"      {posting['business_date']}  {posting['kind']:<11} "
                        f"{posting['effect']:<6} {posting['amount']:>12}  {posting['source_reference']}"
                    )

        self.stdout.write("")
        self.stdout.write(style.MIGRATE_HEADING("Correction routes (this command changed nothing)"))
        for route in payload["correction_routes"]:
            self.stdout.write(f"  - {route}")
