# apps/bookings/services/receipt_pdf.py
"""Server-side receipt PDF generation.

The database is the source of truth for the receipt attachment: the PDF is
regenerated from the booking and its successful payments at send time, through
the same ``ReceiptSerializer`` the emailed HTML uses, so the two can never
disagree. Nothing the browser produced is trusted. Generation happens entirely
in memory (``BytesIO``), so there are no temporary files to clean up.

The layout deliberately mirrors, pixel-for-pixel, the "Access-style" receipt the
frontend renders in ``frontend/js/receipt.js`` (the ``rc-access`` design). That
renderer draws the receipt onto a 720px-wide canvas and, for its PDF export,
wraps the canvas as an image inside a PDF page whose size is the canvas size
scaled by 0.75 (px -> pt). We reproduce the *same* 720px coordinate system and
the *same* 0.75 scale here with reportlab vector drawing, so the emailed PDF and
the on-page image/PDF are visually identical on every device — same header
lockup, centred title, gold labels / blue values two-column rows, colour-coded
status, footer and watermark.

reportlab is a pure-Python dependency (no system libraries), and the fonts and
logo are bundled next to this module, so generation is byte-for-byte reproducible
on both the web service and the worker regardless of the host's system fonts.
"""
from __future__ import annotations

import io
import logging
import os
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.utils import timezone

logger = logging.getLogger("apps")

# ---- Canvas geometry (identical to receipt.js) ----------------------------
RECEIPT_WIDTH = 720
PAD_X = 60
CONTENT_WIDTH = RECEIPT_WIDTH - PAD_X * 2      # 600
LABEL_WIDTH = 225
GAP = 18
VALUE_WIDTH = CONTENT_WIDTH - LABEL_WIDTH - GAP  # 357
VALUE_X = PAD_X + LABEL_WIDTH + GAP              # 303
TABLE_TOP = 232
PX_TO_PT = 0.75                                  # matches jpegPdfBlob()

# ---- Colours (identical to EXPORT_CSS / drawReceiptCanvas) -----------------
C_WORDMARK = "#143d69"
C_WORDMARK_SUB = "#6c6e70"
C_TITLE = "#053e75"
C_GENERATED = "#7b828a"
C_LABEL = "#a66d10"
C_VALUE = "#123f70"
C_VALUE_MUTED = "#65707a"
C_STATUS_OK = "#0b6b3a"
C_STATUS_WARN = "#8a5a0a"
C_STATUS_DANGER = "#a72f28"
C_DIVIDER = "#e2e6ea"
C_FOOT = "#707780"
C_CHANNELS = "#80868d"

_ASSETS = os.path.join(os.path.dirname(__file__), "assets")
_FONT_REG = "JoneSans"
_FONT_BOLD = "JoneSans-Bold"

FALLBACK_HOTEL = {
    "name": "J-ONE HOTEL & LODGE",
    "phone": "+234 803 211 2874",
    "email": "jonathanonu76@gmail.com",
    "address": "Plot 566 Mgbowo Street, off Ezike Street, Enugu State.",
}

_fonts_registered = False


def _ensure_fonts() -> None:
    global _fonts_registered
    if _fonts_registered:
        return
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    pdfmetrics.registerFont(TTFont(_FONT_REG, os.path.join(_ASSETS, "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont(_FONT_BOLD, os.path.join(_ASSETS, "DejaVuSans-Bold.ttf")))
    _fonts_registered = True


# ---------------------------------------------------------------------------
# Value helpers — mirror the normalize()/detailRows() logic in receipt.js so
# the same fields, ordering and wording appear in the same rows.
# ---------------------------------------------------------------------------
def _clean(value) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s in ("—", "--") else s


def _first(*values) -> str:
    for v in values:
        if v is not None and _clean(v) != "":
            return v
    return ""


def _as_number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _money(value) -> str:
    """₦ + thousands-separated integer amount — same as JONE.formatNaira."""
    try:
        n = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        n = Decimal(0)
    neg = n < 0
    whole = int(n.copy_abs().to_integral_value(rounding="ROUND_HALF_UP"))
    return ("-" if neg else "") + "\u20a6" + f"{whole:,}"


def _labelize(value) -> str:
    return re.sub(r"\s+", " ", _clean(value).replace("_", " ")).strip()


def _title_case(value) -> str:
    return _labelize(value).lower().title()


def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    s = str(value).strip()
    for parser in (datetime.fromisoformat,):
        try:
            return parser(s.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _localize(dt: datetime) -> datetime:
    if timezone.is_aware(dt):
        return timezone.localtime(dt)
    return dt


def _access_datetime(value) -> str:
    dt = _parse_dt(value) or timezone.localtime()
    dt = _localize(dt)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _generated_datetime(value) -> str:
    dt = _parse_dt(value) or timezone.localtime()
    dt = _localize(dt)
    return dt.strftime("%d/%m/%y %H:%M:%S")


def _date_only(value) -> str:
    dt = _parse_dt(value)
    if not dt:
        return _clean(value)
    # en-GB "mid": e.g. "5 Sep 2026"
    return f"{dt.day} {dt.strftime('%b')} {dt.year}"


def _format_nights(n) -> str:
    if n is None or _clean(n) == "":
        return ""
    try:
        count = int(n)
    except (TypeError, ValueError):
        return _clean(n)
    return f"{count} night" + ("" if count == 1 else "s")


def _compact(parts, sep=", ") -> str:
    return sep.join([p for p in (_clean(x) for x in parts) if p])


def _room_numbers(data) -> str:
    nums = data.get("room_numbers")
    if isinstance(nums, (list, tuple)) and nums:
        return ", ".join(str(x) for x in nums)
    assignments = data.get("room_assignments")
    if isinstance(assignments, (list, tuple)) and assignments:
        got = [a.get("room_number") for a in assignments if isinstance(a, dict) and a.get("room_number")]
        if got:
            return ", ".join(str(x) for x in got)
    count = data.get("rooms") or data.get("number_of_rooms")
    if count:
        return f"{count} room" + ("" if str(count) == "1" else "s")
    return ""


def _latest_payment(data):
    if data.get("latest_payment"):
        return data["latest_payment"]
    payments = data.get("payments")
    if isinstance(payments, (list, tuple)) and payments:
        return payments[-1]
    return None


def _hotel_from(data) -> dict:
    h = data.get("hotel") or {}
    return {
        "name": _first(h.get("name"), h.get("hotel_name"), h.get("display_name"), FALLBACK_HOTEL["name"]),
        "phone": _first(h.get("phone"), h.get("phone_number"), h.get("telephone"), FALLBACK_HOTEL["phone"]),
        "email": _first(h.get("email"), h.get("contact_email"), FALLBACK_HOTEL["email"]),
        "address": _compact([
            _first(h.get("address"), h.get("street_address"), FALLBACK_HOTEL["address"]),
            h.get("city"), h.get("state"), h.get("country"),
        ]),
    }


def _guest_from(data) -> dict:
    g = data.get("guest") or {}
    return {
        "name": _first(g.get("name"), g.get("full_name"), _compact([g.get("first_name"), g.get("last_name")], " "), "Guest"),
        "phone": _first(g.get("phone"), g.get("phone_number")),
        "email": _first(g.get("email")),
        "address": _compact([g.get("address"), g.get("city"), g.get("state"), g.get("country")]),
    }


def _status_text(data, latest) -> str:
    raw = str(_first(data.get("payment_status"), latest and latest.get("status"), data.get("status"))).upper()
    return {
        "PAID": "Successful", "SUCCESS": "Successful",
        "PARTIALLY_PAID": "Partially paid",
        "UNPAID": "Awaiting payment", "PENDING": "Awaiting payment",
        "PARTIALLY_REFUNDED": "Partially refunded",
        "REFUNDED": "Refunded", "FAILED": "Failed", "CANCELLED": "Cancelled",
    }.get(raw, _title_case(raw) if raw else "Processed")


def _status_color(label) -> str:
    l = str(label or "").lower()
    if "failed" in l or "cancelled" in l:
        return C_STATUS_DANGER
    if "awaiting" in l or "partial" in l or "pending" in l:
        return C_STATUS_WARN
    return C_STATUS_OK


def _is_paystack(latest) -> bool:
    if not latest:
        return False
    return "PAYSTACK" in str(_first(latest.get("provider"), latest.get("provider_label"))).upper()


def _payment_type(latest) -> str:
    if not latest:
        return "BOOKING CONFIRMATION"
    provider = str(_first(latest.get("provider_label"), latest.get("provider"))).upper().replace("_", " ")
    channel = str(_first(latest.get("channel"))).upper().replace("_", " ")
    if _is_paystack(latest):
        return "ONLINE PAYMENT" + (f" / {channel}" if channel else "")
    # Offline records store the provider as the channel too (cash → "cash");
    # drop the channel when the provider already contains it ("CASH / CASH",
    # "POS TERMINAL / POS") so nothing is displayed twice.
    def _norm(s):
        return "".join(ch for ch in s if ch.isalnum())
    if channel and _norm(channel) and _norm(channel) in _norm(provider):
        channel = ""
    return provider + (f" / {channel}" if channel else "")


def _normalize(data: dict, opts: dict) -> dict:
    latest = _latest_payment(data)
    hotel = _hotel_from(data)
    guest = _guest_from(data)
    total = _first(data.get("total"), data.get("total_amount"))
    paid = _first(data.get("amount_paid"))
    due = _first(data.get("amount_due"))
    if latest and latest.get("amount") is not None:
        amount = latest.get("amount")
    else:
        amount = _first(
            paid if _as_number(paid) > 0 else "",
            due if _as_number(due) > 0 else "",
            total,
        )
    booking_ref = _first(data.get("booking_reference"), data.get("reference"))
    payment_ref = _first(latest and latest.get("reference"), data.get("receipt_reference"),
                         data.get("payment_reference"), booking_ref)
    # Gateway session/transaction id: only a real provider value may appear.
    # Offline (cash/POS/transfer) payments have no gateway session — never
    # substitute the payment reference and never invent one. (Mirrors
    # normalize() in frontend/js/receipt.js.)
    if _is_paystack(latest):
        tx_id = _first(latest and latest.get("transaction_id"), data.get("transaction_id"),
                       latest and latest.get("provider_reference"), payment_ref)
    else:
        tx_id = _first(latest and latest.get("transaction_id"))
    room_type = _first(data.get("room_type"), data.get("room_type_name"), opts.get("room_type"))
    rooms = _room_numbers(data)
    stay_line = _compact([room_type, f"Room {rooms}" if rooms else ""], " \u00b7 ")
    stay_dates = _compact([
        f"Check-in {_date_only(data.get('check_in'))}" if data.get("check_in") else "",
        f"Check-out {_date_only(data.get('check_out'))}" if data.get("check_out") else "",
        _format_nights(data.get("nights")),
    ], " \u00b7 ")
    guest_count = _first(data.get("number_of_guests"),
                         (_as_number(data.get("adults")) + _as_number(data.get("children"))) or "")
    stay_guests = ""
    if guest_count:
        try:
            gc = int(float(guest_count))
            stay_guests = f"{gc} guest" + ("" if gc == 1 else "s")
        except (TypeError, ValueError):
            stay_guests = ""
    status = _status_text(data, latest)
    remark_line = "Hotel booking payment" + (f" for {booking_ref}" if booking_ref else "")
    booking_details = _compact([stay_line, stay_dates, stay_guests], " \u00b7 ")
    # Offline (front-desk) payments carry the staff member who recorded them;
    # Paystack rows carry the payer, which is not "recorded by" information.
    recorded_by = _first(latest.get("recorded_by")) if (latest and not _is_paystack(latest)) else ""
    notes = _first(latest.get("notes")) if latest else ""

    summary = [s for s in [
        f"Booking total: {_money(total)}" if total != "" else "",
        f"Amount paid: {_money(paid)}" if paid != "" else "",
        f"Outstanding balance: {_money(due)}" if due != "" else "",
    ] if s]

    return {
        "hotel": hotel,
        "guest": guest,
        "source_label": _first(opts.get("sourceLabel"), opts.get("generatedFrom"), "J-ONE"),
        "document_title": _first(opts.get("documentTitle"), "Transaction Receipt"),
        "amount": amount,
        "transaction_type": _payment_type(latest),
        "transaction_date": _access_datetime(_first(
            latest and latest.get("paid_at"), data.get("issued_at"),
            data.get("updated_at"), data.get("created_at"))),
        "generated_at": _generated_datetime(_first(
            data.get("issued_at"), latest and latest.get("paid_at"),
            data.get("updated_at"), data.get("created_at"))),
        "sender_lines": [x for x in [guest["name"], guest["phone"], guest["email"]] if x],
        "beneficiary_lines": [x for x in [hotel["name"], hotel["address"], hotel["phone"], hotel["email"]] if x],
        "remark_line": remark_line,
        "booking_details": [booking_details] if booking_details else [],
        "booking_reference": booking_ref,
        "payment_reference": payment_ref,
        "session_id": tx_id,
        "recorded_by": recorded_by,
        "notes": notes,
        "status": status,
        "status_color": _status_color(status),
        "summary_lines": summary,
    }


def _detail_rows(d: dict):
    """(label, value_lines, kind, muted_rest) — mirrors detailRows()."""
    rows = [
        ("Transaction Amount", [_money(d["amount"])], "amount", False),
        ("Transaction Type", [d["transaction_type"]], "value", False),
        ("Transaction Date", [d["transaction_date"]], "value", False),
        ("Sender", d["sender_lines"], "value", True),
        ("Beneficiary", d["beneficiary_lines"], "value", True),
        ("Booking Details", d["booking_details"], "value", True),
        ("Remark", [d["remark_line"] or "Hotel booking payment"], "value", False),
        ("Booking Reference", [d["booking_reference"]], "value", False),
        ("Transaction Reference", [d["payment_reference"]], "value", False),
    ]
    # Same conditional rows as detailRows() in receipt.js: no fabricated
    # gateway session for offline payments; staff attribution when present.
    if d["session_id"]:
        rows.append(("Session Id", [d["session_id"]], "value", False))
    if d.get("recorded_by"):
        rows.append(("Recorded By", [d["recorded_by"]], "value", False))
    if d.get("notes"):
        rows.append(("Notes", [d["notes"]], "value", False))
    rows.append(("Transaction Status", [d["status"]], "status", False))
    if d["summary_lines"]:
        rows.append(("Payment Summary", d["summary_lines"], "value", True))
    return rows


# ---------------------------------------------------------------------------
# Text wrapping — same greedy word-wrap (with long-word breaking) as
# wrapCanvasText(); measured with reportlab so line breaks fall in the same
# places relative to the column width.
# ---------------------------------------------------------------------------
def _text_width(text, font, size_px) -> float:
    from reportlab.pdfbase.pdfmetrics import stringWidth

    _ensure_fonts()  # wrapping may run before render_receipt_pdf registers fonts
    return stringWidth(text, font, size_px)


def _wrap(text, max_width, font, size_px):
    text = _clean(text) or "\u2014"
    words = re.split(r"\s+", text)
    lines = []
    line = ""

    def push_long_word(word, current_line):
        chunk = ""
        for ch in word:
            nxt = chunk + ch
            if chunk and _text_width(nxt, font, size_px) > max_width:
                lines.append(chunk)
                chunk = ch
            else:
                chunk = nxt
        return chunk

    for word in words:
        if not word:
            continue
        test = (line + " " + word) if line else word
        if _text_width(test, font, size_px) <= max_width:
            line = test
        elif line:
            lines.append(line)
            if _text_width(word, font, size_px) > max_width:
                line = push_long_word(word, line)
            else:
                line = word
        elif _text_width(word, font, size_px) > max_width:
            line = push_long_word(word, line)
        else:
            line = word
    if line:
        lines.append(line)
    return lines if lines else ["\u2014"]


def _prepared_rows(rows):
    """Wrap each row and compute its height exactly like preparedRows()."""
    prepared = []
    for label, values, kind, muted_rest in rows:
        label_lines = _wrap(label, LABEL_WIDTH, _FONT_BOLD, 15)
        size = 16 if kind == "amount" else 15
        value_lines = []
        src_values = [v for v in (_clean(x) for x in values) if v] or ["\u2014"]
        for src_idx, raw in enumerate(src_values):
            for wrapped in _wrap(raw, VALUE_WIDTH, _FONT_BOLD, size):
                value_lines.append((wrapped, muted_rest and src_idx > 0))
        h = max(53, max(len(label_lines) * 18, len(value_lines) * 20) + 22)
        prepared.append({
            "label_lines": label_lines,
            "value_lines": value_lines,
            "kind": kind,
            "height": h,
        })
    return prepared


def _faded_logo_reader(alpha: float):
    """Return an ImageReader of the logo with its alpha scaled (for watermark)."""
    from PIL import Image
    from reportlab.lib.utils import ImageReader

    img = Image.open(os.path.join(_ASSETS, "logo-official.png")).convert("RGBA")
    r, g, b, a = img.split()
    a = a.point(lambda v: int(v * alpha))
    img = Image.merge("RGBA", (r, g, b, a))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return ImageReader(buf)


def render_receipt_pdf(receipt: dict, opts: dict | None = None) -> bytes:
    """Render a ``ReceiptSerializer`` payload into PDF bytes.

    Reproduces the ``rc-access`` receipt design from receipt.js. Returns the raw
    PDF bytes. Raises on failure so the caller (the email task) can decide
    whether to send without the attachment or fail — it must NOT silently
    pretend a receipt was attached.
    """
    from reportlab.lib.colors import HexColor
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    _ensure_fonts()
    opts = opts or {}
    d = _normalize(receipt or {}, opts)
    rows = _prepared_rows(_detail_rows(d))

    # --- Height: identical arithmetic to drawReceiptCanvas() ---------------
    table_height = sum(r["height"] for r in rows)
    footer_top = TABLE_TOP + table_height + 22
    contact_text = (
        "If you have any questions or would like more information, please call "
        + (d["hotel"]["phone"] or FALLBACK_HOTEL["phone"])
        + (f" or send an email to {d['hotel']['email']}" if d["hotel"]["email"] else "")
        + "."
    )
    footer_lines = (
        _wrap(contact_text, CONTENT_WIDTH, _FONT_REG, 12)
        + _wrap(f"Thank you for choosing {d['hotel']['name']}.", CONTENT_WIDTH, _FONT_REG, 12)
    )
    footer_height = len(footer_lines) * 16 + 44
    height = int(footer_top + footer_height + 36 + 0.999)  # ceil

    page_w = RECEIPT_WIDTH * PX_TO_PT
    page_h = height * PX_TO_PT

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(page_w, page_h))

    def X(px):
        return px * PX_TO_PT

    def Y(px_top):
        """Convert a top-down canvas y (baseline) to reportlab bottom-up pt."""
        return page_h - px_top * PX_TO_PT

    def sz(px):
        return px * PX_TO_PT

    def text(x_px, y_px, s, font, size_px, color, *, center=False, char_space_px=0.0):
        c.setFont(font, sz(size_px))
        c.setFillColor(HexColor(color))
        if char_space_px:
            # Canvas has no setCharSpace; letter-spacing is applied via a text
            # object (used only for the "HOTEL & LODGE" wordmark subtitle).
            # saveState/restoreState keeps the char-space out of the shared
            # graphics state so it does not leak into later drawString() calls.
            c.saveState()
            to = c.beginText(X(x_px), Y(y_px))
            to.setFont(font, sz(size_px))
            to.setFillColor(HexColor(color))
            to.setCharSpace(sz(char_space_px))
            to.textOut(s)
            c.drawText(to)
            c.restoreState()
        elif center:
            c.drawCentredString(X(x_px), Y(y_px), s)
        else:
            c.drawString(X(x_px), Y(y_px), s)

    # --- Background --------------------------------------------------------
    c.setFillColor(HexColor("#ffffff"))
    c.rect(0, 0, page_w, page_h, fill=1, stroke=0)

    # --- Watermark (5.5% opacity centred logo) -----------------------------
    try:
        wm = _faded_logo_reader(0.055)
        wm_w, wm_h = 210, 390
        wm_x = RECEIPT_WIDTH / 2 - 105
        wm_y_top = height / 2 - 195
        c.drawImage(wm, X(wm_x), Y(wm_y_top + wm_h), width=X(wm_w), height=sz(wm_h),
                    mask="auto", preserveAspectRatio=False)
    except Exception:  # noqa: BLE001 — watermark is decorative; never fail on it
        logger.debug("receipt watermark skipped", exc_info=True)

    # --- Header lockup -----------------------------------------------------
    try:
        logo = ImageReader(os.path.join(_ASSETS, "logo-official.png"))
        c.drawImage(logo, X(PAD_X), Y(52 + 52), width=X(28), height=sz(52),
                    mask="auto", preserveAspectRatio=False)
    except Exception:  # noqa: BLE001
        text(PAD_X, 84, "J-ONE", _FONT_BOLD, 22, C_WORDMARK)
    text(PAD_X + 42, 80, "J-ONE", _FONT_BOLD, 30, C_WORDMARK)
    text(PAD_X + 42, 98, "HOTEL & LODGE", _FONT_BOLD, 9, C_WORDMARK_SUB, char_space_px=2)

    # --- Title + generated line (centred) ----------------------------------
    text(RECEIPT_WIDTH / 2, 168, d["document_title"], _FONT_BOLD, 30, C_TITLE, center=True)
    text(RECEIPT_WIDTH / 2, 206,
         f"Generated from {d['source_label']} on {d['generated_at']}",
         _FONT_REG, 13, C_GENERATED, center=True)

    # --- Detail table ------------------------------------------------------
    y = TABLE_TOP
    for r in rows:
        label_lines = r["label_lines"]
        value_lines = r["value_lines"]
        h = r["height"]
        kind = r["kind"]
        label_y = y + max(16, (h - len(label_lines) * 18) / 2 + 13)
        value_y = y + max(16, (h - len(value_lines) * 20) / 2 + 14)

        for idx, line in enumerate(label_lines):
            text(PAD_X + 6, label_y + idx * 18, line, _FONT_BOLD, 15, C_LABEL)

        for idx, (line, muted) in enumerate(value_lines):
            if kind == "amount":
                font, size, color = _FONT_BOLD, 16, C_VALUE
            elif kind == "status":
                font, size, color = _FONT_BOLD, 15, d["status_color"]
            else:
                font, size = _FONT_BOLD, 15
                color = C_VALUE_MUTED if muted else C_VALUE
            text(VALUE_X, value_y + idx * 20, line, font, size, color)

        # Row divider
        c.setStrokeColor(HexColor(C_DIVIDER))
        c.setLineWidth(sz(1))
        c.line(X(PAD_X), Y(y + h), X(RECEIPT_WIDTH - PAD_X), Y(y + h))
        y += h

    # --- Footer ------------------------------------------------------------
    cursor = footer_top + 12
    for line in footer_lines:
        text(PAD_X, cursor, line, _FONT_REG, 12, C_FOOT)
        cursor += 16
    text(PAD_X, height - 32,
         "J-ONE HOTEL & LODGE: Rooms | Bookings | Online Payment | Contact centre",
         _FONT_REG, 12, C_CHANNELS)

    c.showPage()
    c.save()
    return buffer.getvalue()
