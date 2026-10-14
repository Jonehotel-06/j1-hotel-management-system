#!/usr/bin/env python3
"""Negative control for the tightened secret scan in tests-pwa/test_regression.py.

A test that can never fail is worthless, so this proves the new scan still
catches real credentials, and that it no longer flags the legitimate endpoint
path it used to flag. Uses the SAME constants as the suite.
"""
import re
import sys

from jone_verify import PWA_BASE
import urllib.request

PROVIDER_MARKERS = ("sk_live", "sk_test", "pk_live", "paystack_secret", "aws_secret",
                    "secret_key", "smtp_password", "smtp_pass")
ASSIGNED = re.compile(
    r"""(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|smtp|access[_-]?key)\b\s*[:=]\s*["'][^"']{1,}["']""")


def scan(txt):
    bad = [m for m in PROVIDER_MARKERS if m in txt.lower()]
    bad += [m.group(0)[:48] for m in ASSIGNED.finditer(txt)]
    return bad


# (label, text, must_be_flagged) — either signal (provider marker or an
# assigned credential word) is enough to fail the suite, which is what matters.
cases = [
    ("real js/config.js as served", urllib.request.urlopen(
        PWA_BASE + "/js/config.js").read().decode(), False),
    ("planted provider key", 'var key = "sk_live_51H8xQ2eZvKYlo2C";', True),
    ("planted password assignment", 'const payload = { password: "hunter2hunter2" };', True),
    ("planted smtp credential", "SMTP_PASS = 'sup3r-s3cret'", True),
    ("planted api key", 'API_KEY: "AKIAIOSFODNN7EXAMPLE"', True),
    ("legitimate endpoint path", 'passwordChange:"/api/auth/password/change/",', False),
    ("legitimate prose about passwords", "// change the signed-in account's password", False),
]

fails = 0
for label, text, must_flag in cases:
    found = scan(text)
    ok = bool(found) if must_flag else not found
    print(("PASS" if ok else "FAIL"), f"{label}: flagged={found}")
    fails += 0 if ok else 1
print(f"\n{len(cases) - fails}/{len(cases)} negative-control cases behaved correctly")
sys.exit(1 if fails else 0)
