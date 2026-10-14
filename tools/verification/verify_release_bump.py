#!/usr/bin/env python3
"""Deployment pre-flight: prove the release step and the update prompt still work.

`PRODUCTION_AUDIT_REPAIR_REPORT.md` §F step 5 is `python frontend/build.py --bump`.
This script exercises it the way an operator would, on a **copy** of the frontend
(nothing in the working tree is touched):

1. `build.py` with no bump must be **idempotent** — if re-stamping the current
   version rewrites pages, the deployment would carry spurious diffs, and a
   hand-edited page could be clobbered.
2. `build.py --bump patch` must move the three version sources **in lockstep**
   (`version.json`, `js/version.js`, `sw.js` `CACHE_VERSION`) and re-stamp every
   asset reference on every page — no page left pointing at the old version.
3. The validator must still pass on the built output.
4. A browser **on the old release** must be offered the new one. The tab is served
   the un-bumped tree from one port and `version.json` is answered from the bumped
   build on another, which is exactly the state of the world during a rollout;
   item 9's machinery must then raise the "A new version is available" prompt with
   both actions. Two throwaway servers are started and stopped for this.

    python tools/verification/verify_release_bump.py

No credentials are needed — this checks the frontend release, not the API. The
temporary copy and its server are removed on exit.
"""
from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
PYTHON = sys.executable
ROWS = []


def record(name, ok, detail=""):
    ROWS.append((name, ok, detail))
    print(("PASS" if ok else "FAIL"), name, "-", detail)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def version_sources(tree: Path) -> dict:
    version_json = json.loads((tree / "version.json").read_text())["version"]
    version_js = re.search(r'JONE_VERSION\s*=\s*"([^"]+)"', (tree / "js" / "version.js").read_text()).group(1)
    cache_version = re.search(r'CACHE_VERSION\s*=\s*"([^"]+)"', (tree / "sw.js").read_text()).group(1)
    return {"version.json": version_json, "js/version.js": version_js, "sw.js": cache_version,
            "all_equal": version_json == version_js == cache_version.lstrip("jone-v")}


def diff_trees(left: Path, right: Path) -> list[str]:
    """Paths that differ between two frontend trees (ignoring run artefacts)."""
    out = subprocess.run(
        ["diff", "-rq", str(left), str(right),
         "--exclude=__pycache__", "--exclude=artifacts", "--exclude=.gitignore"],
        capture_output=True, text=True)
    return [line for line in out.stdout.splitlines() if line.strip()]


def stamp_leaks(tree: Path, version: str) -> int:
    """Pages still advertising an older version in their asset references."""
    leaks = 0
    for page in tree.rglob("*.html"):
        text = page.read_text(errors="ignore")
        for match in set(re.findall(r"\?v=([0-9][0-9.]*)", text)):
            if match != version:
                leaks += 1
                break
    return leaks


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="jone-release-check-"))
    copy = workdir / "frontend"
    servers: list[subprocess.Popen] = []
    try:
        shutil.copytree(FRONTEND, copy, ignore=shutil.ignore_patterns("__pycache__", "artifacts"))

        # ---------------------------------------------------- 1. idempotence
        before = version_sources(copy)
        build = subprocess.run([PYTHON, "build.py"], cwd=copy, capture_output=True, text=True, timeout=300)
        after = version_sources(copy)
        drift = diff_trees(FRONTEND, copy)
        record("build.py with no bump is idempotent (no page is rewritten)",
               build.returncode == 0 and not drift and before == after,
               f"rc={build.returncode}, {len(drift)} differing path(s)"
               + (f": {drift[:3]}" if drift else ""))

        # ---------------------------------------------------- 2. the release
        bump = subprocess.run([PYTHON, "build.py", "--bump", "patch"], cwd=copy,
                              capture_output=True, text=True, timeout=300)
        released = version_sources(copy)
        expected = ".".join(str(int(part) + (1 if i == 2 else 0))
                            for i, part in enumerate(before["version.json"].split(".")))
        record("build.py --bump patch moves all three version sources together",
               bump.returncode == 0 and released["all_equal"] and released["version.json"] == expected,
               f"rc={bump.returncode} {before['version.json']} -> {released['version.json']} "
               f"(version.json={released['version.json']}, version.js={released['js/version.js']}, "
               f"sw cache={released['sw.js']})")

        leaks = stamp_leaks(copy, released["version.json"])
        record("every page's asset references are re-stamped on release", leaks == 0,
               f"{leaks} page(s) still advertising an older stamp")

        # ---------------------------------------------------- 3. the output still validates
        validate = subprocess.run([PYTHON, "validate.py"], cwd=copy, capture_output=True, text=True, timeout=300)
        record("the built output passes frontend/validate.py", validate.returncode == 0,
               validate.stdout.strip().splitlines()[-1] if validate.stdout.strip() else f"rc={validate.returncode}")

        # ------------------------------------------- 4. an old tab is offered the new release
        # Two throwaway servers: the rollout state is "old build in the browser,
        # new build on the server". Serving the un-bumped tree read-only is safe —
        # dev_server.py never writes.
        old_port, new_port = free_port(), free_port()
        for tree, port in ((FRONTEND, old_port), (copy, new_port)):
            servers.append(subprocess.Popen([PYTHON, "dev_server.py", str(port), "http://127.0.0.1:8000"],
                                            cwd=tree, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

        def wait_for_version(port):
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/version.json", timeout=2) as response:
                        return json.loads(response.read().decode())
                except Exception:
                    time.sleep(0.3)
            return {}

        served = wait_for_version(new_port)
        old_served = wait_for_version(old_port)
        record("the two throwaway servers serve the old and the released version",
               old_served.get("version") == before["version.json"] and served.get("version") == released["version.json"],
               f"old={old_served.get('version')} new={served.get('version')}")
        record("the bumped build serves its released version", served.get("version") == released["version.json"],
               f"served {served or 'nothing'}")

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
            page.add_init_script("""
                localStorage.setItem('jone.auth', JSON.stringify({email:'admin@jonehotel.local',
                    first_name:'Ada', last_name:'Admin', role:'ADMIN'}));
                sessionStorage.setItem('jone.session', JSON.stringify({access:'x', refresh:'r'}));
            """)
            # The tab keeps running the ORIGINAL release; /version.json is answered
            # with what the bumped deployment really returns. The trailing * matters:
            # the checker cache-busts with ?t=<ms>.
            page.route("**/version.json*", lambda route: route.fulfill(
                status=200, content_type="application/json", body=json.dumps(served),
                headers={"cache-control": "no-store"}))
            page.route("**/api/**", lambda route: route.fulfill(
                status=200, content_type="application/json",
                body='{"success": true, "data": {"results": []}, "pagination": null}'))
            page.goto(f"http://127.0.0.1:{old_port}/dashboard/index.html", wait_until="load")
            page.wait_for_timeout(1200)
            running = page.evaluate("() => window.JONE_VERSION")
            page.evaluate("() => JONE.updateChecker.check(true)")
            prompted = True
            try:
                page.wait_for_selector(".modal", timeout=20000)
            except Exception:
                prompted = False
            actions = []
            heading = ""
            if prompted:
                detail = page.evaluate("""() => {
                    const panel = document.querySelector('.modal-panel[role=dialog]');
                    return { heading: (panel.querySelector('h4') || {}).textContent || '',
                             buttons: Array.from(panel.querySelectorAll('button'))
                                              .map(b => b.textContent.trim()).filter(Boolean) };
                }""")
                heading, actions = detail["heading"], detail["buttons"]
            browser.close()

        record("a tab on the old release is offered the new one (item 9's machinery)",
               prompted and "new version" in heading.lower()
               and "Refresh now" in actions and "Not now" in actions,
               f"tab ran {running}, deployment {served.get('version')} -> "
               f"prompt={prompted} heading={heading!r} actions={actions}")
    finally:
        for server in servers:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
        shutil.rmtree(workdir, ignore_errors=True)

    failed = [row for row in ROWS if not row[1]]
    print(f"\n{len(ROWS) - len(failed)}/{len(ROWS)} release checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
