"""The build must refuse to ship changed assets under an unchanged version.

WHY THIS EXISTS
===============
Production 2026-11-20, after the first repair was deployed: the operator still
saw the old fingerprint error. The server was verified byte-for-byte correct —
``/js/biometrics.js`` on production was identical to the repository — but every
page still asked for ``biometrics.js?v=1.1.29`` because ``version.json`` had
never been bumped. The ``?v=`` stamp IS the cache key for Cloudflare, the
browser cache and the staff service worker, so a release that changes code
without changing the stamp cannot reach a browser that has loaded the site
before. Nothing in the build noticed.

These tests pin the guard that now notices, and the release command that clears
it. They run the real ``build.py`` in a temporary copy of the frontend, so the
working tree is never modified.

    python -m pytest frontend/tests/test_release_guard.py -v
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[1]


@pytest.fixture()
def workspace():
    """A throwaway copy of the frontend, so build.py can run for real."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "frontend"
        shutil.copytree(FRONTEND, target, ignore=shutil.ignore_patterns("tests", "tests-js", ".git"))
        yield target


def build(where, *args):
    return subprocess.run([sys.executable, "build.py", *args],
                          cwd=str(where), capture_output=True, text=True, timeout=300)


def test_first_build_records_a_baseline(workspace):
    result = build(workspace)
    assert result.returncode == 0, result.stderr
    state = json.loads((workspace / ".build-assets.json").read_text())
    assert state["version"] == json.loads((workspace / "version.json").read_text())["version"]
    assert state["assets"], "the baseline must cover the shipped assets"
    assert "js/biometrics.js" in state["assets"]


def test_an_unchanged_rebuild_is_allowed(workspace):
    assert build(workspace).returncode == 0
    second = build(workspace)
    assert second.returncode == 0, "rebuilding identical assets must not be blocked"
    assert "BUILD STOPPED" not in second.stdout + second.stderr


def test_changed_code_without_a_bump_is_refused(workspace):
    """The exact production mistake: edit JS, deploy, do not bump."""
    assert build(workspace).returncode == 0
    biometrics = workspace / "js" / "biometrics.js"
    biometrics.write_text(biometrics.read_text() + "\n// repair\n")

    result = build(workspace)
    assert result.returncode != 0, "a silent un-bumped release must be impossible"
    message = result.stdout + result.stderr
    assert "BUILD STOPPED" in message
    assert "js/biometrics.js" in message, "the guard must name the file"
    assert "--bump patch" in message, "the guard must name the command that fixes it"


def test_the_guard_does_not_block_an_actual_release(workspace):
    assert build(workspace).returncode == 0
    biometrics = workspace / "js" / "biometrics.js"
    biometrics.write_text(biometrics.read_text() + "\n// repair\n")
    before = json.loads((workspace / "version.json").read_text())["version"]

    released = build(workspace, "--bump", "patch")
    assert released.returncode == 0, released.stderr
    assert "BUILD STOPPED" not in released.stdout + released.stderr

    after = json.loads((workspace / "version.json").read_text())["version"]
    assert tuple(map(int, after.split("."))) > tuple(map(int, before.split("."))), (before, after)
    # version.json, the worker cache namespace and every page stamp move together.
    assert re.search(r'CACHE_VERSION = "jone-v%s"' % re.escape(after), (workspace / "sw.js").read_text())
    # The reception page loads js/reader-agent.js (the fingerprint panel) and
    # every asset it references must carry the released stamp — a single stale
    # ?v= on this page is how production kept running the old panel.
    page = (workspace / "dashboard" / "workforce.html").read_text()
    assert re.search(r'reader-agent\.js\?v=%s' % re.escape(after), page), (
        "the workforce page must load the reader-agent panel at the released version"
    )
    stamps = set(re.findall(r'\?v=([0-9.]+)', page))
    assert stamps == {after}, f"every asset stamp on the page must be {after}, found {sorted(stamps)}"


def test_the_escape_hatch_is_explicit(workspace):
    assert build(workspace).returncode == 0
    biometrics = workspace / "js" / "biometrics.js"
    biometrics.write_text(biometrics.read_text() + "\n// hotfix\n")

    result = build(workspace, "--allow-unstamped")
    assert result.returncode == 0, "an operator may override deliberately"
    assert "BUILD STOPPED" not in result.stdout + result.stderr
