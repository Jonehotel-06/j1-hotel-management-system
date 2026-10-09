"""Request-scoped metadata shared by audited service layers.

Only authenticated server-resolved identities are copied here. A workstation
reference is deliberately treated as optional attribution, never as a login or
authorization factor.
"""
from contextvars import ContextVar

_request_context = ContextVar("jone_request_context", default={})


def bind_request_context(**values):
    current = dict(_request_context.get() or {})
    current.update(values)
    _request_context.set(current)
    return current


def get_request_context():
    return dict(_request_context.get() or {})


def set_request_context(value):
    return _request_context.set(dict(value or {}))


def reset_request_context(token):
    _request_context.reset(token)
