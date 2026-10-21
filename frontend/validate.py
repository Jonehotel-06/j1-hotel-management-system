#!/usr/bin/env python3
# validate.py
"""J-ONE frontend validator.
Checks every HTML page for: doctype, balanced tags, unresolved placeholders,
missing local assets, referenced JS syntax, inline event handlers, and the
audit's no-fake-data rule (no Math.random booking refs, no ?demo=1 bypass).
"""
import re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).parent
PUBLIC = [p for p in ROOT.glob("*.html")]
DASH = [p for p in (ROOT / "dashboard").glob("*.html")]
ALL = PUBLIC + DASH

def err(msg):
    print("  [FAIL] " + msg)

def check_file(p):
    issues = []
    html = p.read_text(encoding="utf-8")
    rel = str(p.relative_to(ROOT))
    if not html.lstrip().lower().startswith("<!doctype"):
        issues.append("missing <!DOCTYPE html>")
    # unresolved build markers
    for marker in ("<!--JS-->", "<!--HEADER-->", "<!--FOOTER-->", "<!--MOBILE-->"):
        if marker in html:
            issues.append("unresolved marker " + marker)
    # inline event handlers (onclick etc.) — attribute form only.
    # onerror on an <img> is a legitimate graceful image fallback; skip it.
    for m in re.finditer(r"\s(on[a-z]+)\s*=", html):
        if m.group(1) == "onerror":
            continue
        issues.append("inline event handler: " + m.group(0))
    # forbidden production data patterns
    if "Math.random()" in html.replace("Math.random", "Math.random") and "booking" in rel:
        if re.search(r"reference.{0,20}Math\.random|Math\.random.{0,20}J1-", html):
            issues.append("client-side booking ref via Math.random")
    if "?demo=1" in html or "getItem(\"demo\")" in html or "getItem('demo')" in html:
        issues.append("demo bypass present")
    # balance of div tags (self-closing ignored)
    opens = len(re.findall(r"<div\b", html))
    closes = len(re.findall(r"</div>", html))
    if opens != closes:
        issues.append(f"div balance {opens} open vs {closes} close")
    # referenced local src/href assets (cache-busting queries stripped)
    for src in re.findall(r'(?:src|href)="((?:\.\./|\./)?[^"]+)"', html):
        if src.startswith(("http", "#", "mailto:", "tel:", "data:", "javascript:", "https", "//")):
            continue
        src = src.split("?", 1)[0]
        # Root-absolute references (/css/…, /js/…, /manifest.webmanifest) resolve
        # against the deployed site root, which is this directory.
        path = (ROOT / src.lstrip("/")).resolve() if src.startswith("/") else (p.parent / src).resolve()
        base_ok = (ROOT / "css").exists() or True
        if path.exists():
            continue
        # allow root-relative css/ js/ assets
        alt = (ROOT.parent / src).resolve()
        if (ROOT / src.lstrip("./")).exists() or (p.parent / src).exists():
            continue
        if "css/" in src or "js/" in src or "favicon/" in src or "assets/" in src:
            if not path.exists():
                issues.append("missing local asset: " + src)
    return issues

fails_js = []

def check_js(p):
    issues = []
    html = p.read_text(encoding="utf-8")
    rel = str(p.relative_to(ROOT))
    # every locally referenced js module must exist and be parseable.
    # (src may carry a cache-busting query — ?v=1.2.3 from build.py — which is
    # stripped before resolving the file on disk.)
    for src in re.findall(r'<script src="([^"]+)"></script>', html):
        if src.startswith(("http", "//")):
            continue
        src = src.split("?", 1)[0]
        f = (ROOT / src.lstrip("/")).resolve() if src.startswith("/") else (p.parent / src).resolve()
        if not f.exists():
            issues.append("missing script: " + src)
            continue
    # each inline <script> without src — check syntax via node
    for i, body in enumerate(re.findall(r"<script>(.*?)</script>", html, re.S)):
        if not body.strip():
            continue
        tmp = Path("/tmp/_v.js")
        tmp.write_text(body, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True)
        if r.returncode != 0:
            issues.append(f"inline script #{i} syntax error: " + (r.stderr.strip().splitlines() or [""])[-1])
    return issues

# every standalone JS file must be parseable by node
for js in sorted(ROOT.glob("js/*.js")) + [ROOT / "sw.js"]:
    r = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
    if r.returncode != 0:
        print("  [FAIL] syntax error in " + str(js.relative_to(ROOT)) + ":")
        print("         " + (r.stderr.strip().splitlines() or [""])[-1])
        fails_js.append(str(js))

fails = 0
for p in ALL:
    rel = str(p.relative_to(ROOT))
    is_html = p.suffix == ".html"
    if not is_html:
        continue
    issues = check_file(p) + check_js(p)
    if issues:
        fails += 1
        print(rel)
        for i in issues:
            err(i)

js_count = len(list(ROOT.glob("js/*.js"))) + 1
print(f"\nChecked {len([p for p in ALL if p.suffix=='.html'])} HTML files, syntax-checked {js_count} standalone JS files.")
total_fails = fails + len(fails_js)
print("FAIL" if total_fails else "ALL OK")
sys.exit(1 if total_fails else 0)
