"""Logging filter that removes credentials from messages and tracebacks.

Full tracebacks stay available for diagnosis, but provider payloads, bearer
tokens, passwords and card fields must never be written to the console/log
sink. This filter is attached to the console handler in settings so it covers
Django request errors as well as the application's ``apps`` logger.
"""
from __future__ import annotations

import logging
import re
from typing import Any

_REPLACEMENTS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE), "Bearer [REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[REDACTED_JWT]"),
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|token|access_token|refresh_token|secret|api[_-]?key|"
            r"authorization|client_secret|private_key|card_number|card[_-]?number|cvv|pin|otp)\b"
            r"(\s*[:=]\s*|\"?\s*:\s*\")([^,\s}\]\"']+)"
        ),
        r"\1\2[REDACTED]",
    ),
    (
        re.compile(
            r'(?i)"(password|passwd|pwd|token|access|refresh|secret|api_key|authorization|'
            r'card_number|cvv|pin|otp)"\s*:\s*"[^"]*"'
        ),
        r'"\1":"[REDACTED]"',
    ),
    (re.compile(r"(?i)(https?://[^/\s:@]+:)[^@/\s]+@"), r"\1[REDACTED]@"),
)


def redact(value: Any) -> Any:
    """Return ``value`` with known credential shapes replaced."""
    if isinstance(value, str):
        text = value
        for pattern, replacement in _REPLACEMENTS:
            text = pattern.sub(replacement, text)
        return text
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact(item) for item in value)
    return value


class SecretRedactingFilter(logging.Filter):
    """Redact log messages, args, stack info and cached exception text."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = redact(record.args)
            else:
                record.args = tuple(redact(arg) for arg in record.args)
        if record.exc_info:
            formatter = logging.Formatter()
            record.exc_text = redact(formatter.formatException(record.exc_info))
        elif getattr(record, "exc_text", None):
            record.exc_text = redact(record.exc_text)
        if getattr(record, "stack_info", None):
            record.stack_info = redact(record.stack_info)
        return True
