"""Additive, capability-gated ledger registry and report preview endpoints."""
from __future__ import annotations

import csv
import io
import re
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.http import HttpResponse
from django.utils.dateparse import parse_date
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.views import APIView

from apps.audit.services import log_action
from apps.core.approvals import override_event_details, require_independent_approver
from apps.core.permissions import HasCapability
from apps.core.responses import success_response
from apps.core.utils import hotel_today

from .models import ChartAccount, FinancialEvent, FinancialLine, FinancialTransaction
from .services import ledger_reports
from .services.ledger_service import create_posted_transaction


class PrivateAccountingAPIView(APIView):
    """Prevent caches from storing sensitive finance responses and errors."""
    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response


class AccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChartAccount
        fields = ("code", "label", "kind", "purpose", "is_active", "is_system")
        read_only_fields = ("is_system",)
        extra_kwargs = {"purpose": {"required": False, "allow_null": True, "allow_blank": True}}

    def validate_code(self, value):
        value = value.strip().upper()
        if not re.fullmatch(r"(?:ASSET|LIABILITY|EQUITY|REVENUE|CONTRA_REVENUE|EXPENSE)\.[A-Z0-9_.-]{1,47}", value):
            raise serializers.ValidationError("Use a category prefix and uppercase letters, numbers, dots, dashes or underscores.")
        return value

    def validate(self, attrs):
        code = attrs.get("code", getattr(self.instance, "code", ""))
        kind = attrs.get("kind", getattr(self.instance, "kind", ""))
        if code.split(".", 1)[0] != kind:
            raise serializers.ValidationError({"kind": "The account kind must match its code prefix."})
        if attrs.get("purpose") == "":
            attrs["purpose"] = None
        return attrs


class AccountsView(PrivateAccountingAPIView):
    permission_classes = [HasCapability]
    required_capability = "reports.financial.view"

    def get(self, request):
        return success_response({"accounts": AccountSerializer(ChartAccount.objects.all(), many=True).data})


class AccountCreateView(PrivateAccountingAPIView):
    permission_classes = [HasCapability]
    required_capability = "finance.controls.manage"

    @transaction.atomic
    def post(self, request):
        serializer = AccountSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account = serializer.save(created_by=request.user, is_system=False)
        log_action(actor=request.user, action="CHART_ACCOUNT_CREATED", instance=account, request=request,
                   metadata={"code": account.code, "kind": account.kind})
        return success_response(AccountSerializer(account).data, status=status.HTTP_201_CREATED)


class AccountUpdateView(PrivateAccountingAPIView):
    permission_classes = [HasCapability]
    required_capability = "finance.controls.manage"

    @transaction.atomic
    def patch(self, request, code):
        account = ChartAccount.objects.select_for_update().filter(code=code).first()
        if not account:
            raise NotFound("Account not found.")
        # Identity fields are never mutable, even before the first posting.
        if set(request.data) - {"label", "is_active"}:
            raise ValidationError("Only the label and active state may be changed.")
        serializer = AccountSerializer(account, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            account = serializer.save()
        except DjangoValidationError as exc:
            raise ValidationError(exc.messages) from exc
        log_action(actor=request.user, action="CHART_ACCOUNT_UPDATED", instance=account, request=request,
                   metadata={"code": account.code, "is_active": account.is_active})
        return success_response(AccountSerializer(account).data)


def _parameters(request):
    today = hotel_today()
    start_raw = request.query_params.get("start")
    end_raw = request.query_params.get("end")
    try:
        start = parse_date(start_raw) if start_raw else today.replace(day=1)
        end = parse_date(end_raw) if end_raw else today
    except ValueError as exc:
        raise ValidationError("Dates must be valid calendar dates in YYYY-MM-DD form.") from exc
    if not start or not end:
        raise ValidationError("Dates must use YYYY-MM-DD.")
    if end < start:
        raise ValidationError("End date must be on or after start date.")
    currency = str(request.query_params.get("currency") or "NGN").upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValidationError("Currency must be a three-letter code.")
    return {"start": start, "end": end, "currency": currency}


class OverviewView(PrivateAccountingAPIView):
    permission_classes = [HasCapability]
    required_capability = "reports.financial.view"

    def get(self, request):
        return success_response(ledger_reports.overview(**_parameters(request)))


def _cell(value):
    value = "" if value is None else str(value)
    # Spreadsheet formula injection must not execute a description/reference.
    if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


_MONEY_COLUMNS = {"amount", "opening", "closing", "debit", "credit", "increases", "decreases",
                  "opening_debit", "opening_credit", "period_debit", "period_credit", "closing_debit", "closing_credit",
                  "running_debit_balance"}


def _record_cell(column, value):
    # Only known server-generated numeric columns are allowed as numbers;
    # all free text remains formula-escaped for spreadsheets.
    text = "" if value is None else str(value)
    if column in _MONEY_COLUMNS and re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return Decimal(text)
    return _cell(value)


def _export(report, extension):
    records = report.get("records", [])
    columns = list(dict.fromkeys(key for row in records for key in row.keys()))
    if not columns:
        columns = ["message"]
        records = [{"message": "No records for the selected period"}]
    filename = re.sub(r"[^a-z0-9-]", "-", report.get("report", "accounting"))
    summary = [("Status", "PREVIEW ONLY: historical reconciliation is not certified"),
               ("Report", report.get("report", "accounting")), ("Currency", report.get("currency", "NGN")),
               ("Start", report.get("start", "")), ("End/as of", report.get("end", report.get("as_of", "")))]
    if report.get("account"):
        summary.append(("Account", report["account"]))
    summary.extend((key.replace("_", " ").title(), value) for key, value in report.get("totals", {}).items())
    for key in ("opening_debit_balance", "period_debit", "period_credit", "closing_debit_balance"):
        if key in report:
            summary.append((key.replace("_", " ").title(), report[key]))
    summary.extend(("Warning", warning) for warning in report.get("warnings", []))
    if extension == "csv":
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(["Report details", "Value"])
        for label, value in summary:
            writer.writerow([_cell(label), _cell(value)])
        writer.writerow([])
        writer.writerow(columns)
        for record in records:
            writer.writerow([_record_cell(c, record.get(c, "")) for c in columns])
        response = HttpResponse("\ufeff" + stream.getvalue(), content_type="text/csv; charset=utf-8")
    elif extension == "xlsx":
        from openpyxl import Workbook
        from openpyxl.styles import Font
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Ledger report"
        sheet.append(columns)
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for record in records:
            sheet.append([_record_cell(c, record.get(c, "")) for c in columns])
        sheet.freeze_panes = "A2"
        summary_sheet = workbook.create_sheet("Report details")
        summary_sheet.append(["Report details", "Value"])
        for label, value in summary:
            summary_sheet.append([_cell(label), _cell(value)])
        summary_sheet.column_dimensions["A"].width = 32
        summary_sheet.column_dimensions["B"].width = 100
        stream = io.BytesIO()
        workbook.save(stream)
        response = HttpResponse(stream.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    elif extension == "pdf":
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle
        from xml.sax.saxutils import escape
        stream = io.BytesIO()
        document = SimpleDocTemplate(stream, pagesize=landscape(A4), leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24)
        style = ParagraphStyle("table", fontSize=7, leading=9, alignment=TA_LEFT)
        title = Paragraph(escape(f"{report.get('report', 'Accounting')} — {report.get('currency', 'NGN')} — PREVIEW; reconciliation pending"), style)
        data = [[Paragraph(escape(str(c)), style) for c in columns]]
        data.extend([[Paragraph(escape(str(record.get(c, ""))), style) for c in columns] for record in records])
        table = Table(data, repeatRows=1)
        table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("GRID", (0, 0), (-1, -1), .25, colors.lightgrey)]))
        description = [Paragraph(escape(f"{label}: {value}"), style) for label, value in summary]
        document.build([title, *description, table])
        response = HttpResponse(stream.getvalue(), content_type="application/pdf")
    else:
        raise ValidationError("Export format must be csv, xlsx or pdf.")
    response["Content-Disposition"] = f'attachment; filename="{filename}.{extension}"'
    response["Cache-Control"] = "private, no-store"
    return response


class LedgerReportView(PrivateAccountingAPIView):
    permission_classes = [HasCapability]
    required_capability = "reports.financial.view"

    def get(self, request, report):
        params = _parameters(request)
        functions = {"trial-balance": ledger_reports.trial_balance,
                     "profit-loss": ledger_reports.income_statement,
                     "balance-sheet": ledger_reports.balance_sheet,
                     "cash-bank": ledger_reports.cash_bank}
        if report == "general-ledger":
            code = str(request.query_params.get("account") or "").strip()
            if not code:
                raise ValidationError({"account": "An account code is required."})
            try:
                page = int(request.query_params.get("page", "1"))
                size = int(request.query_params.get("page_size", "50"))
            except ValueError as exc:
                raise ValidationError("Page and page_size must be whole numbers.") from exc
            export = request.query_params.get("export")
            if page < 1 or size < 1 or size > (5000 if export else 100):
                raise ValidationError("Invalid page or page_size.")
            result = ledger_reports.general_ledger(code=code, page=page, page_size=size, **params)
            if export and (page != 1 or result["count"] > size):
                raise ValidationError("Export must start at page 1 and include every row. Narrow the date range or increase page_size (maximum 5000).")
        elif report in functions:
            result = functions[report](**params)
        else:
            raise NotFound("Unknown ledger report.")
        export = request.query_params.get("export")
        return _export(result, export.lower()) if export else success_response(result)


class ReverseManualJournalView(PrivateAccountingAPIView):
    """Only manual journals are reversed here; operational reversals need their own subledger workflow."""
    permission_classes = [HasCapability]
    required_capability = "expense.approve"

    @transaction.atomic
    def post(self, request, reference):
        original = FinancialTransaction.objects.select_for_update().filter(reference=reference).first()
        if not original or original.status != FinancialTransaction.Status.POSTED:
            raise NotFound("Posted journal not found.")
        if (original.metadata or {}).get("entry_kind") != "MANUAL_JOURNAL" or not original.initiated_by_id:
            raise ValidationError("Only attributed manual journals can use this reversal endpoint; use the source workflow for operational entries.")
        reason = str(request.data.get("reason") or "").strip()
        if len(reason) < 5:
            raise ValidationError({"reason": "Provide a reversal reason of at least five characters."})
        existing = FinancialTransaction.objects.filter(reversal_of=original, status=FinancialTransaction.Status.POSTED).first()
        if existing:
            return success_response({"reference": existing.reference, "created": False})
        evidence = require_independent_approver(requester=original.initiated_by, actor=request.user, workflow="journal reversal")
        lines = list(original.lines.order_by("pk"))
        if any(line.folio_id for line in lines):
            raise ValidationError("A folio-linked journal requires its own subledger reversal workflow.")
        key = f"manual-reversal:{original.reference}"
        reversed_tx, created = create_posted_transaction(
            transaction_type=FinancialTransaction.Type.VOID, source_key=key, idempotency_key=key,
            actor=request.user, source_reference=original.reference, narrative=f"Reversal: {reason}"[:500],
            currency=original.currency, business_date=hotel_today(), reversal_of=original,
            metadata=override_event_details({"entry_kind": "MANUAL_JOURNAL_REVERSAL", "reason": reason}, evidence),
            lines=[{"account_code": line.account_code,
                    "direction": FinancialLine.Direction.CREDIT if line.direction == FinancialLine.Direction.DEBIT else FinancialLine.Direction.DEBIT,
                    "amount": line.amount, "description": f"Reversal of {original.reference}"} for line in lines],
        )
        if created:
            FinancialEvent.objects.create(transaction=original, type=FinancialEvent.Type.REVERSED, actor=request.user,
                                          details={"reversal_reference": reversed_tx.reference, "reason": reason, "override": evidence})
            log_action(actor=request.user, action="MANUAL_JOURNAL_REVERSED", instance=original, request=request,
                       metadata={"reversal_reference": reversed_tx.reference, "reason": reason})
        return success_response({"reference": reversed_tx.reference, "created": created},
                                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
