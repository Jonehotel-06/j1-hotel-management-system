"""Excel and PDF renderings of the occupancy report.

Consumes the exact dict ``occupancy_report`` returns (daily occupied rooms,
occupancy %, available/occupied room-nights) — the exporters never recompute
occupancy, so a spreadsheet, a PDF and the on-screen report always agree.
"""
from __future__ import annotations


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
    sheet.append([])
    sheet.append(["Date", "Occupied rooms", "Available rooms", "Occupancy %"])
    for row in report.get("by_day", []):
        sheet.append([row.get("date"), row.get("occupied_rooms"), report.get("active_rooms"), row.get("occupancy_rate_percent")])
    for column, width in (("A", 16), ("B", 18), ("C", 18), ("D", 14)):
        sheet.column_dimensions[column].width = width
    output = BytesIO()
    book.save(output)
    return output.getvalue()


def occupancy_pdf_bytes(report: dict) -> bytes:
    from io import BytesIO

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from reportlab.lib import colors

    output = BytesIO()
    document = SimpleDocTemplate(output, pagesize=A4, title="J-ONE occupancy report")
    styles = getSampleStyleSheet()
    elements = [
        Paragraph("J-ONE HOTEL &amp; LODGE — Occupancy report", styles["Title"]),
        Paragraph(
            f"Period {report.get('start_date')} to {report.get('end_date')} · "
            f"{report.get('active_rooms')} active rooms · "
            f"{report.get('occupied_room_nights')} occupied of {report.get('available_room_nights')} available room-nights · "
            f"average occupancy {report.get('average_occupancy_percent')}%",
            styles["Normal"],
        ),
        Spacer(1, 12),
    ]
    rows = [["Date", "Occupied rooms", "Available rooms", "Occupancy %"]]
    for row in report.get("by_day", []):
        rows.append([
            str(row.get("date") or ""), str(row.get("occupied_rooms") or 0),
            str(report.get("active_rooms") or 0), str(row.get("occupancy_rate_percent") or 0),
        ])
    table = Table(rows, colWidths=[120, 130, 130, 110])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#373435")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#B8B2AC")),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    elements.append(table)
    document.build(elements)
    return output.getvalue()
