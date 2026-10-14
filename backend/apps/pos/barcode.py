"""Barcode normalisation and format validation for POS menu items.

A barcode is an identifier, not a price and not proof of stock. Values are
stored exactly as printed/scanned except for surrounding whitespace: leading
zeros are meaningful in EAN/UPC symbols and are therefore never stripped.

Supported assignment formats
----------------------------
* GTIN-family numeric symbols (EAN-8, UPC-A, EAN-13 and GTIN-14): when the
  value is all digits with one of those lengths, its GS1 check digit is
  validated.
* Code 128 (subset B on the printed J-ONE labels): printable ASCII values
  that do not fall into the GTIN shape above.

Uniqueness is enforced by the database on ``MenuItem.barcode`` (NULLs, used
by unlabelled items, never collide). SKU remains a separate legacy
identifier; the lookup service also resolves SKUs so labels printed from
existing SKUs keep working.
"""
from __future__ import annotations

MAX_BARCODE_LENGTH = 64
MIN_BARCODE_LENGTH = 3
GTIN_LENGTHS = (8, 12, 13, 14)


def normalize_barcode(value) -> str | None:
    """Trim surrounding whitespace only; preserve leading zeros and case."""
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def gtin_check_digit_is_valid(value: str) -> bool:
    """Validate a GS1 check digit (weights alternate 3/1 from the right)."""
    if not value.isdigit() or len(value) not in GTIN_LENGTHS:
        return False
    total = 0
    weight = 3
    for char in reversed(value[:-1]):
        total += int(char) * weight
        weight = 1 if weight == 3 else 3
    check = (10 - (total % 10)) % 10
    return check == int(value[-1])


def validate_barcode(value):
    """Return the normalized barcode or raise ``ValueError`` with a message.

    ``None``/blank is allowed (item simply has no dedicated barcode yet).
    """
    normalized = normalize_barcode(value)
    if normalized is None:
        return None
    if len(normalized) < MIN_BARCODE_LENGTH:
        raise ValueError("Barcode must be at least 3 characters long.")
    if len(normalized) > MAX_BARCODE_LENGTH:
        raise ValueError("Barcode must be at most 64 characters long.")
    if any(char.isspace() for char in normalized):
        raise ValueError("Barcode must not contain spaces.")
    if normalized.isdigit() and len(normalized) in GTIN_LENGTHS:
        if not gtin_check_digit_is_valid(normalized):
            raise ValueError("Barcode check digit is invalid for its EAN/UPC format.")
        return normalized
    if not all(32 <= ord(char) <= 126 for char in normalized):
        raise ValueError("Barcode must use printable Code 128 characters.")
    return normalized
