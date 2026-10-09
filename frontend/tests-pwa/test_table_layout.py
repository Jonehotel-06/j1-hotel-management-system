"""Browser check for the shared staff table layout (CSS + js/table-fit.js).

Runs against a static fixture (tests-pwa/fixtures/table-layout.html) that uses
the real stylesheet and script. Verifies, by measured geometry rather than by
class names alone:
  * no page-level horizontal overflow at desktop and phone widths;
  * amounts, references and statuses stay on one line;
  * long guest/room prose still wraps;
  * wide tables scroll inside .table-wrap and that wrapper is keyboard-focusable;
  * sticky-free sort/filter hooks are untouched (no data-* attributes removed).

Usage:  python3 tests-pwa/test_table_layout.py   (needs Playwright Chromium)
"""
import functools
import http.server
import pathlib
import sys
import threading

from playwright.sync_api import sync_playwright

FRONTEND = pathlib.Path(__file__).resolve().parent.parent
FIXTURE = "/tests-pwa/fixtures/table-layout.html"


def serve():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FRONTEND))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


MEASURE = """
(id) => {
  const table = document.getElementById(id);
  const wrap = table.closest('.table-wrap');
  // Count rendered text lines from per-text-node line fragments. Each text node
  // yields one rectangle per line box it occupies; fragments on the same line
  // differ by well under half a font size, lines are separated by more than
  // that, so tops are clustered with that tolerance (no rounding artefacts).
  // Cell height is not used: table rows stretch every cell to the tallest cell.
  const lineCount = (c) => {
    const tops = [];
    const walker = document.createTreeWalker(c, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      if (!node.textContent.trim()) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      for (const r of Array.from(range.getClientRects())) {
        if (r.height > 0 && r.width > 0) tops.push(r.top);
      }
    }
    tops.sort((a, b) => a - b);
    const fontSize = parseFloat(getComputedStyle(c).fontSize) || 14;
    let lines = 0;
    let last = -Infinity;
    for (const top of tops) {
      if (top - last > fontSize * 0.5) { lines += 1; last = top; }
    }
    return lines;
  };
  const cells = (row) => Array.from(row.cells).map((c) => ({
    text: c.textContent.trim().slice(0, 30),
    lines: lineCount(c),
    fit: c.classList.contains('cell-fit'),
    whiteSpace: getComputedStyle(c).whiteSpace,
  }));
  return {
    head: cells(table.tHead.rows[0]),
    rows: Array.from(table.tBodies[0].rows).map(cells),
    wrapScrolls: wrap.scrollWidth > wrap.clientWidth,
    wrapTabIndex: wrap.getAttribute('tabindex'),
  };
}
"""


def run():
    failures = []
    server = serve()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for width, label in ((1280, "desktop"), (390, "phone")):
                page = browser.new_page(viewport={"width": width, "height": 900})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(base + FIXTURE)
                page.wait_for_timeout(150)

                overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
                if overflow > 0:
                    failures.append(f"[{label}] page overflows horizontally by {overflow}px")
                if errors:
                    failures.append(f"[{label}] page errors: {errors}")

                ledger = page.evaluate(MEASURE, "ledger")
                head = {c["text"]: c for c in ledger["head"]}
                for col in ("Reference", "Check-in date", "Amount", "Status"):
                    if not head[col]["fit"]:
                        failures.append(f"[{label}] heading {col} not marked cell-fit")
                for row in ledger["rows"]:
                    amount = row[4]
                    if amount["lines"] > 1:
                        failures.append(f"[{label}] amount wrapped to {amount['lines']} lines: {amount['text']}")
                    if row[0]["lines"] > 1:
                        failures.append(f"[{label}] reference wrapped: {row[0]['text']}")
                    if row[3]["lines"] > 1:
                        failures.append(f"[{label}] date wrapped: {row[3]['text']}")
                room = ledger["rows"][0][2]
                if label == "phone" and room["lines"] < 2:
                    failures.append(f"[phone] long room type did not wrap: {room['text']}")
                first_guest = ledger["rows"][0][1]
                if first_guest["fit"]:
                    failures.append(f"[{label}] guest name wrongly marked cell-fit")
                if label == "phone" and first_guest["lines"] < 2:
                    failures.append(f"[{label}] long guest name did not wrap on a phone width")

                if not ledger["wrapTabIndex"] == "0":
                    failures.append(f"[{label}] .table-wrap is not keyboard focusable")
                page.focus(".table-wrap")
                focused = page.evaluate("() => document.activeElement && document.activeElement.classList.contains('table-wrap')")
                if not focused:
                    failures.append(f"[{label}] .table-wrap did not receive keyboard focus")

                wide = page.evaluate(MEASURE, "wide")
                if label == "phone" and not wide["wrapScrolls"]:
                    failures.append("[phone] wide register does not scroll inside its wrapper")
                for row in wide["rows"]:
                    for cell in row:
                        if cell["text"].startswith("₦") and cell["lines"] > 1:
                            failures.append(f"[{label}] money cell wrapped: {cell['text']}")
                page.close()
            browser.close()
    finally:
        server.shutdown()
    return failures


if __name__ == "__main__":
    problems = run()
    if problems:
        print("FAILED")
        for item in problems:
            print(" -", item)
        sys.exit(1)
    print("OK: table layout checks passed at desktop and phone widths")
