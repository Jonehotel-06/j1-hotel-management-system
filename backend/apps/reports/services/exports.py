"""Excel and PDF renderings of the operational reports.

Each exporter consumes the exact dict its report service returns — occupancy,
revenue or bookings — and never recomputes anything. A spreadsheet, a PDF and
the on-screen report therefore always agree, which is the whole point of
exporting from the server instead of scraping the rendered table.

BATCH 01 (task 8). Two defects are fixed here and apply to every exporter:

* Money was written as a bare number with no currency anywhere on the sheet,
  so an exported file could not be read without knowing what produced it.
  Amounts now carry the report's own currency — as a real Excel number format
  so the cell still sums, and as an ``NGN 1,234.56`` string in the PDF, where
  the built-in Helvetica font has no naira glyph and would print a blank box.
* An empty period produced a table with a header row and nothing under it,
  which reads as "no data was exported" rather than "nothing happened in this
  period". Every table now states that explicitly.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

# Matches the on-screen dashboard: a thousands separator and exactly two
# decimals, prefixed with the ISO code rather than a symbol.
EXCEL_MONEY_FORMAT = '"NGN" #,##0.00'
EMPTY_NOTE = "No activity was recorded in this period."


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else "0"))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _money_text(value, currency: str = "NGN") -> str:
    """`NGN 1,234.56` — spelt out, because Helvetica has no ₦ glyph."""
    return f"{currency} {_decimal(value):,.2f}"


def _excel_money(sheet, row_index: int, columns, currency: str = "NGN") -> None:
    """Make the given columns of a written row read as money, not as digits."""
    fmt = f'"{currency}" #,##0.00'
    for column in columns:
        sheet.cell(row=row_index, column=column).number_format = fmt


def _empty_row(width: int):
    return [EMPTY_NOTE] + [""] * (width - 1)


# ---------------------------------------------------------------------------
# Shared PDF furniture
# ---------------------------------------------------------------------------
def _pdf_table(rows, col_widths, right_align_from: int | None = None):
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle

    table = Table(rows, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#373435")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#B8B2AC")),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]
    if right_align_from is not None:
        style.append(("ALIGN", (right_align_from, 1), (-1, -1), "RIGHT"))
    if len(rows) == 2 and rows[1] and rows[1][0] == EMPTY_NOTE:
        # The note spans the table rather than pretending to be a data row.
        style.append(("SPAN", (0, 1), (-1, 1)))
        style.append(("TEXTCOLOR", (0, 1), (-1, 1), colors.HexColor("#6B6560")))
    table.setStyle(TableStyle(style))
    return table


def _pdf_document(output, title: str):
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate

    return SimpleDocTemplate(output, pagesize=A4, title=title)


# ---------------------------------------------------------------------------
# Occupancy
# ---------------------------------------------------------------------------
def occupancy_xlsx_bytes(report: dict) -> bytes:
    from io import BytesIO

    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "Occupancy"
    sheet.append(["J-ONE HOTEL & LODGE — Occupancy report"])
    sheet.append(["Period", report.get("start_date"), report.get("end_date")])
    sheet.append(["Active rooms", report.get("active_rooms")])
    sheet.append(["Occupied room-nights", report.get("occupied_room_nights")])
    sheet.append(["Available room-nights", report.get("available_room_nights")])
    sheet.append(["Average occupancy %", report.get("average_occupancy_percent")])
    # Rooms SOLD vs rooms a guest actually took possession of. The difference is
    # exactly the no-shows the hotel still paid for, so both are exported.
    sheet.append(["Arrived room-nights", report.get("arrived_room_nights")])
    sheet.append(["Rooms not arrived (no-show)", report.get("unarrived_room_nights")])
    sheet.append([])
    sheet.append(["Date", "Occupied rooms", "Arrived rooms", "Not arrived", "Available rooms", "Occupancy %"])
    days = report.get("by_day") or []
    for row in days:
        sheet.append([
            row.get("date"), row.get("occupied_rooms"), row.get("arrived_rooms"),
            row.get("unarrived_rooms"), report.get("active_rooms"), row.get("occupancy_rate_percent"),
        ])
    if not days:
        sheet.append(_empty_row(6))
    for column, width in (("A", 30), ("B", 18), ("C", 18), ("D", 14), ("E", 18), ("F", 14)):
        sheet.column_dimensions[column].width = width
    output = BytesIO()
    book.save(output)
    return output.getvalue()


def occupancy_pdf_bytes(report: dict) -> bytes:
    from io import BytesIO

    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, Spacer

    output = BytesIO()
    document = _pdf_document(output, "J-ONE occupancy report")
    styles = getSampleStyleSheet()
    elements = [
        Paragraph("J-ONE HOTEL &amp; LODGE — Occupancy report", styles["Title"]),
        Paragraph(
            f"Period {report.get('start_date')} to {report.get('end_date')} · "
            f"{report.get('active_rooms')} active rooms · "
            f"{report.get('occupied_room_nights')} sold of {report.get('available_room_nights')} available room-nights · "
            f"average occupancy {report.get('average_occupancy_percent')}% · "
            f"{report.get('arrived_room_nights')} room-nights with a guest checked in "
            f"({report.get('unarrived_room_nights')} never arrived)",
            styles["Normal"],
        ),
        Spacer(1, 12),
    ]
    rows = [["Date", "Sold", "Arrived", "Not arrived", "Occupancy %"]]
    for row in report.get("by_day") or []:
        rows.append([
            str(row.get("date") or ""), str(row.get("occupied_rooms") or 0),
            str(row.get("arrived_rooms") or 0), str(row.get("unarrived_rooms") or 0),
            str(row.get("occupancy_rate_percent") or 0),
        ])
    if len(rows) == 1:
        rows.append(_empty_row(5))
    elements.append(_pdf_table(rows, [120, 80, 80, 90, 110], right_align_from=1))
    document.build(elements)
    return output.getvalue()


# ---------------------------------------------------------------------------
# Revenue  (BATCH 01 task 8 — the screen offered an export the server never
# implemented, so the button produced a JSON body with a .xlsx name.)
# ---------------------------------------------------------------------------
def revenue_xlsx_bytes(report: dict) -> bytes:
    from io import BytesIO

    from openpyxl import Workbook

    currency = report.get("currency") or "NGN"
    book = Workbook()
    sheet = book.active
    sheet.title = "Revenue"
    sheet.append(["J-ONE HOTEL & LODGE — Payments received"])
    sheet.append(["Period", report.get("start_date"), report.get("end_date")])
    sheet.append(["Currency", currency])
    sheet.append([
        "Note",
        "Money received in the period. This is a collections report, not an "
        "income statement: a payment for a future stay is counted here and a "
        "stay billed but not yet paid is not.",
    ])
    sheet.append([])

    sheet.append(["Total received", _decimal(report.get("grand_total"))])
    _excel_money(sheet, sheet.max_row, [2], currency)
    sheet.append(["Payments", report.get("transactions") or 0])
    refunds = report.get("refunds") or {}
    sheet.append(["Refunds paid out", _decimal(refunds.get("total"))])
    _excel_money(sheet, sheet.max_row, [2], currency)
    sheet.append(["Refund count", refunds.get("count") or 0])
    sheet.append([
        "Net received", _decimal(report.get("grand_total")) - _decimal(refunds.get("total"))
    ])
    _excel_money(sheet, sheet.max_row, [2], currency)
    sheet.append([])

    sheet.append(["Date", "Received", "Payments"])
    days = report.get("by_day") or []
    for row in days:
        sheet.append([row.get("date"), _decimal(row.get("total")), row.get("transactions") or 0])
        _excel_money(sheet, sheet.max_row, [2], currency)
    if not days:
        sheet.append(_empty_row(3))
    sheet.append([])

    sheet.append(["Payment method", "Received", "Payments"])
    providers = report.get("by_provider") or []
    for row in providers:
        sheet.append([row.get("provider"), _decimal(row.get("total")), row.get("transactions") or 0])
        _excel_money(sheet, sheet.max_row, [2], currency)
    if not providers:
        sheet.append(_empty_row(3))

    for column, width in (("A", 34), ("B", 20), ("C", 16)):
        sheet.column_dimensions[column].width = width
    output = BytesIO()
    book.save(output)
    return output.getvalue()


def revenue_pdf_bytes(report: dict) -> bytes:
    from io import BytesIO

    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, Spacer

    currency = report.get("currency") or "NGN"
    output = BytesIO()
    document = _pdf_document(output, "J-ONE payments received")
    styles = getSampleStyleSheet()
    refunds = report.get("refunds") or {}
    net = _decimal(report.get("grand_total")) - _decimal(refunds.get("total"))
    elements = [
        Paragraph("J-ONE HOTEL &amp; LODGE — Payments received", styles["Title"]),
        Paragraph(
            f"Period {report.get('start_date')} to {report.get('end_date')} · "
            f"{_money_text(report.get('grand_total'), currency)} received over "
            f"{report.get('transactions') or 0} payments · "
            f"{_money_text(refunds.get('total'), currency)} refunded over "
            f"{refunds.get('count') or 0} refunds · net {_money_text(net, currency)}",
            styles["Normal"],
        ),
        Paragraph(
            "Money received in the period. This is a collections report, not an "
            "income statement: a payment for a future stay is counted here and a "
            "stay billed but not yet paid is not.",
            styles["Italic"],
        ),
        Spacer(1, 12),
        Paragraph("Received by day", styles["Heading3"]),
    ]
    rows = [["Date", "Received", "Payments"]]
    for row in report.get("by_day") or []:
        rows.append([
            str(row.get("date") or ""), _money_text(row.get("total"), currency),
            str(row.get("transactions") or 0),
        ])
    if len(rows) == 1:
        rows.append(_empty_row(3))
    elements.append(_pdf_table(rows, [160, 180, 110], right_align_from=1))
    elements.append(Spacer(1, 14))
    elements.append(Paragraph("Received by payment method", styles["Heading3"]))

    rows = [["Payment method", "Received", "Payments"]]
    for row in report.get("by_provider") or []:
        rows.append([
            str(row.get("provider") or "—"), _money_text(row.get("total"), currency),
            str(row.get("transactions") or 0),
        ])
    if len(rows) == 1:
        rows.append(_empty_row(3))
    elements.append(_pdf_table(rows, [160, 180, 110], right_align_from=1))
    document.build(elements)
    return output.getvalue()


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------
def bookings_xlsx_bytes(report: dict) -> bytes:
    from io import BytesIO

    from openpyxl import Workbook

    currency = "NGN"
    book = Workbook()
    sheet = book.active
    sheet.title = "Bookings"
    sheet.append(["J-ONE HOTEL & LODGE — Bookings report"])
    sheet.append(["Period", report.get("start_date"), report.get("end_date")])
    sheet.append(["Total bookings", report.get("total_bookings") or 0])
    sheet.append(["Cancelled", report.get("cancelled") or 0])
    sheet.append(["No-shows", report.get("no_shows") or 0])
    sheet.append(["Cancellation rate %", report.get("cancellation_rate_percent") or 0])
    sheet.append([])

    sheet.append(["Status", "Bookings"])
    statuses = report.get("by_status") or []
    for row in statuses:
        sheet.append([row.get("status"), row.get("count") or 0])
    if not statuses:
        sheet.append(_empty_row(2))
    sheet.append([])

    sheet.append(["Payment status", "Bookings"])
    payment_statuses = report.get("by_payment_status") or []
    for row in payment_statuses:
        sheet.append([row.get("payment_status"), row.get("count") or 0])
    if not payment_statuses:
        sheet.append(_empty_row(2))
    sheet.append([])

    # Contract value of the bookings taken, which is NOT money received.
    sheet.append(["Room type", "Bookings", "Contract value"])
    room_types = report.get("by_room_type") or []
    for row in room_types:
        sheet.append([row.get("room_type"), row.get("bookings") or 0, _decimal(row.get("revenue"))])
        _excel_money(sheet, sheet.max_row, [3], currency)
    if not room_types:
        sheet.append(_empty_row(3))

    for column, width in (("A", 30), ("B", 16), ("C", 20)):
        sheet.column_dimensions[column].width = width
    output = BytesIO()
    book.save(output)
    return output.getvalue()


def bookings_pdf_bytes(report: dict) -> bytes:
    from io import BytesIO

    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, Spacer

    output = BytesIO()
    document = _pdf_document(output, "J-ONE bookings report")
    styles = getSampleStyleSheet()
    elements = [
        Paragraph("J-ONE HOTEL &amp; LODGE — Bookings report", styles["Title"]),
        Paragraph(
            f"Period {report.get('start_date')} to {report.get('end_date')} · "
            f"{report.get('total_bookings') or 0} bookings · "
            f"{report.get('cancelled') or 0} cancelled · "
            f"{report.get('no_shows') or 0} no-shows · "
            f"cancellation rate {report.get('cancellation_rate_percent') or 0}%",
            styles["Normal"],
        ),
        Spacer(1, 12),
        Paragraph("By status", styles["Heading3"]),
    ]
    rows = [["Status", "Bookings"]]
    for row in report.get("by_status") or []:
        rows.append([str(row.get("status") or "—"), str(row.get("count") or 0)])
    if len(rows) == 1:
        rows.append(_empty_row(2))
    elements.append(_pdf_table(rows, [260, 120], right_align_from=1))
    elements.append(Spacer(1, 14))

    elements.append(Paragraph("By payment status", styles["Heading3"]))
    rows = [["Payment status", "Bookings"]]
    for row in report.get("by_payment_status") or []:
        rows.append([str(row.get("payment_status") or "—"), str(row.get("count") or 0)])
    if len(rows) == 1:
        rows.append(_empty_row(2))
    elements.append(_pdf_table(rows, [260, 120], right_align_from=1))
    elements.append(Spacer(1, 14))

    elements.append(Paragraph("By room type", styles["Heading3"]))
    elements.append(Paragraph(
        "Contract value is what the bookings are worth, not what has been "
        "collected. The payments report carries money received.",
        styles["Italic"],
    ))
    rows = [["Room type", "Bookings", "Contract value"]]
    for row in report.get("by_room_type") or []:
        rows.append([
            str(row.get("room_type") or "—"), str(row.get("bookings") or 0),
            _money_text(row.get("revenue")),
        ])
    if len(rows) == 1:
        rows.append(_empty_row(3))
    elements.append(_pdf_table(rows, [200, 100, 150], right_align_from=1))
    document.build(elements)
    return output.getvalue()
