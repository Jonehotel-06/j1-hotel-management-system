# workstation_agent/tests/test_agent_unit.py
"""Logic tests for the J-ONE Reader Agent. No hardware, no SDK, no network.

These pin the parts of the chain that are not the sensor: how a dashboard
request is turned into an agent request, how a second launch hands over to the
running agent, how a crashed loop comes back, and the invariants that must hold
for the storage and transport rules (no loopback server, no image on disk,
HTTPS only).

Run with:  python -m unittest discover -s workstation_agent/tests -v
"""
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

import jone_reader_agent as agent  # noqa: E402
from agent_store import AgentPaths, AgentStore  # noqa: E402


class UriContractTests(unittest.TestCase):
    """What the browser hands to Windows, and what the agent does with it."""

    def test_a_capture_uri_carries_only_a_session_and_the_api_base(self):
        request = agent.request_from_uri(
            "jone-reader:capture?session=abc123&api=https://api.jonehotels.com&action=CLOCK_IN"
        )
        self.assertEqual(request["kind"], "capture")
        self.assertEqual(request["session"], "abc123")
        self.assertEqual(request["api"], "https://api.jonehotels.com")
        self.assertEqual(request["action"], "CLOCK_IN")
        # No biometric data, no identity, no file path.
        self.assertEqual(set(request), {"kind", "session", "api", "action", "requested_at"})

    def test_windows_style_quoting_is_tolerated(self):
        request = agent.request_from_uri('"jone-reader:start?api=https://api.jonehotels.com"')
        self.assertEqual(request["kind"], "status")
        self.assertEqual(request["api"], "https://api.jonehotels.com")

    def test_an_enrol_uri_names_the_staff_member_and_the_finger(self):
        request = agent.request_from_uri(
            "jone-reader:enroll?session=s1&api=https://api.jonehotels.com&staff_id=7&finger_position=LEFT_THUMB"
        )
        self.assertEqual(request["kind"], "enroll")
        self.assertEqual(request["staff_id"], "7")
        self.assertEqual(request["finger_position"], "LEFT_THUMB")

    def test_an_unknown_verb_is_refused_instead_of_guessed(self):
        with self.assertRaises(SystemExit):
            agent.request_from_uri("jone-reader:delete-everything")
        with self.assertRaises(SystemExit):
            agent.request_from_uri("http://example.com/")

    def test_a_provision_payload_is_decoded_or_refused(self):
        import base64

        payload = base64.urlsafe_b64encode(json.dumps({
            "reference": "rag-1", "key": "k" * 64, "api": "https://api.jonehotels.com",
            "workstation_reference": "wks-1",
        }).encode()).decode().rstrip("=")
        decoded = agent._decode_provision(payload)
        self.assertEqual(decoded["reference"], "rag-1")
        self.assertEqual(decoded["workstation_reference"], "wks-1")
        incomplete = base64.urlsafe_b64encode(json.dumps({"reference": "rag-1"}).encode()).decode()
        with self.assertRaises(SystemExit):
            agent._decode_provision(incomplete)


class SpoolHandoverTests(unittest.TestCase):
    """A second launch (the scheme) must not fight the running agent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jone-agent-test-")
        self.addCleanup(self.tmp.cleanup)
        self.paths = AgentPaths(Path(self.tmp.name)).ensure()
        self.store = AgentStore(self.paths)
        self.logger = logging.getLogger("jone.reader.agent.test")
        self.logger.addHandler(logging.NullHandler())

    def test_a_request_is_stored_and_claimed_exactly_once(self):
        self.store.push_request({"kind": "capture", "session": "s1", "api": "https://api.jonehotels.com"})
        pending = self.store.pending_requests()
        self.assertEqual(len(pending), 1)
        claimed = self.store.claim_request(pending[0])
        self.assertEqual(claimed["session"], "s1")
        self.assertEqual(self.store.pending_requests(), [])

    def test_requests_are_processed_in_the_order_they_arrived(self):
        for index in range(3):
            self.store.push_request({"kind": "capture", "session": f"s{index}"})
        sessions = [self.store.claim_request(path)["session"] for path in self.store.pending_requests()]
        self.assertEqual(sessions, ["s0", "s1", "s2"])

    def test_no_image_or_template_is_written_next_to_the_log(self):
        os.environ["JONE_AGENT_ALLOW_PLAIN_STATE"] = "1"   # DPAPI is Windows-only
        self.addCleanup(os.environ.pop, "JONE_AGENT_ALLOW_PLAIN_STATE", None)
        self.store.push_request({"kind": "capture", "session": "s1"})
        self.store.save_config({"reference": "rag-1", "signing_key": "k", "api_base": "https://x"})
        names = sorted(p.name for p in self.paths.root.rglob("*") if p.is_file() and p.suffix != ".log")
        for name in names:
            self.assertNotIn(name.lower(), {"finger.bmp", "capture.png", "probe.fmd"})


class SupervisorTests(unittest.TestCase):
    """A crashed run loop must come back on its own (no logon required)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jone-agent-supervisor-")
        self.addCleanup(self.tmp.cleanup)
        self.paths = AgentPaths(Path(self.tmp.name)).ensure()
        self.store = AgentStore(self.paths)
        self.logger = logging.getLogger("jone.reader.agent.supervisor")
        self.logger.addHandler(logging.NullHandler())

    def test_the_supervisor_restarts_a_crashed_loop(self):
        calls = {"n": 0}

        def flaky(store, logger, *, once=False):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("simulated crash")
            return 0

        original_run, original_sleep = agent.run, agent.time.sleep
        original_core = agent.make_core
        agent.run = flaky
        agent.time.sleep = lambda seconds: None
        agent.make_core = lambda store, logger: type("C", (), {"heartbeat": lambda self: None})()
        try:
            self.assertEqual(agent.run_supervised(self.store, self.logger), 0)
        finally:
            agent.run, agent.time.sleep, agent.make_core = original_run, original_sleep, original_core
        self.assertEqual(calls["n"], 3)

    def test_the_backoff_grows_and_is_capped(self):
        self.assertLess(agent.RESTART_BACKOFF_START_SECONDS, agent.RESTART_BACKOFF_MAX_SECONDS)
        self.assertLessEqual(agent.RESTART_BACKOFF_MAX_SECONDS, 300)


class TransportAndStorageInvariantTests(unittest.TestCase):
    """The rules the whole design rests on, checked against the source."""

    def read(self, name):
        return (AGENT_DIR / name).read_text(encoding="utf-8")

    def test_the_agent_opens_no_local_server_of_any_kind(self):
        for name in ("jone_reader_agent.py", "agent_core.py", "agent_store.py", "dp_sdk.py"):
            source = self.read(name)
            # A hostname lookup is fine; a listener is not.
            for banned in ("http.server", "socketserver", "BaseHTTPRequestHandler",
                           "socket.socket", ".listen(", ".bind(("):
                self.assertNotIn(banned, source, f"{name} must not contain {banned}")

    def test_the_api_base_must_be_https(self):
        from agent_core import AgentError, HttpClient

        with self.assertRaises(AgentError) as caught:
            HttpClient("http://api.jonehotels.com")
        self.assertEqual(caught.exception.code, "API_BASE_INSECURE")
        HttpClient("https://api.jonehotels.com")   # allowed

    def test_the_agent_never_writes_a_raw_fingerprint_image_to_disk(self):
        source = self.read("agent_core.py")
        for banned in (r"\.save(", "write_bytes", "open(\"wb\")", "open('wb')"):
            self.assertNotIn(banned, source, f"raw capture writes are forbidden ({banned})")
        # Only the FMD (template) travels, and only as JSON inside the signed body.
        self.assertIn("dpfj_create_fmd_from_raw", self.read("dp_sdk.py"))

    def test_the_signed_request_shape_is_the_one_the_api_verifies(self):
        from agent_core import HttpClient  # noqa: F401
        source = self.read("agent_core.py")
        for header in ("X-JONE-Agent-Reference", "X-JONE-Agent-Nonce",
                       "X-JONE-Agent-Timestamp", "X-JONE-Agent-Kind", "X-JONE-Agent-Signature"):
            self.assertIn(header, source)
        self.assertIn("jone-reader-agent-v1", source)


class AgentCoreReliabilityTests(unittest.TestCase):
    """Verify live hardware refresh, gallery sync, session-fail reporting, and cold-start supervision."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jone-agent-core-")
        self.addCleanup(self.tmp.cleanup)
        os.environ["JONE_AGENT_ALLOW_PLAIN_STATE"] = "1"
        self.addCleanup(os.environ.pop, "JONE_AGENT_ALLOW_PLAIN_STATE", None)
        self.paths = AgentPaths(Path(self.tmp.name)).ensure()
        self.store = AgentStore(self.paths)
        self.logger = logging.getLogger("jone.reader.agent.core-test")
        self.logger.addHandler(logging.NullHandler())

    def test_mutex_uses_local_namespace_for_standard_windows_users(self):
        self.assertTrue(self.paths.mutex_name.startswith("Local\\"))

    def test_device_state_refreshes_live_on_usb_unplug_and_replug(self):
        from agent_core import ReaderAgentCore, STATE_NOT_CONNECTED, STATE_READY

        class FakeDevice:
            name = "dev-0"
            product = "U.are.U 4500"
            serial = "SER-4500"
            firmware = "1.0.0"

        class FakeReport:
            def __init__(self, connected):
                self.sdk_available = True
                self.sdk_version = "3.0"
                self.usb_present = connected
                self.devices = [FakeDevice()] if connected else []
                self.driver_mode = "LEGACY_SDK" if connected else "UNKNOWN"
                self.service_running = True
                self.detail = "Ready" if connected else "Unplugged"

            def as_dict(self):
                return {"sdk_available": True, "sdk_version": self.sdk_version, "driver_mode": self.driver_mode}

        class FakeSdk:
            def __init__(self):
                self.connected = True
                self.closed = False
                self.report = FakeReport(True)

            def load(self):
                return self.report

            def refresh_status(self):
                self.report = FakeReport(self.connected)
                return self.report

            def close(self):
                self.closed = True

        sdk = FakeSdk()
        core = ReaderAgentCore(store=self.store, sdk_factory=lambda: sdk, logger=self.logger)
        core.provision(reference="RAG-1", signing_key="ab" * 32, api_base="https://api.test")

        first = core.device_state()
        self.assertEqual(first["device_status"], STATE_READY)
        self.assertEqual(first["device_serial"], "SER-4500")

        sdk.connected = False
        second = core.device_state()
        self.assertEqual(second["device_status"], STATE_NOT_CONNECTED)
        self.assertTrue(sdk.closed)

        sdk.connected = True
        third = core.device_state()
        self.assertEqual(third["device_status"], STATE_READY)

    def test_gallery_refreshes_when_ttl_expires_or_etag_changes(self):
        from agent_core import ReaderAgentCore

        now = [1000.0]
        http_calls = []

        class FakeHttp:
            def __init__(self, base):
                self.base = base

            def post_json(self, path, body, headers):
                http_calls.append((path, json.loads(body.decode("utf-8"))))
                if path.endswith("/heartbeat/"):
                    return 200, {"success": True, "data": {"gallery_etag": "etag-v2", "match_threshold": 2147483}}
                if len([c for c in http_calls if c[0].endswith("/gallery/")]) == 1:
                    return 200, {"success": True, "data": {
                        "etag": "etag-v1",
                        "templates": [{"staff_id": 1, "template_id": 10, "fmd": "AQID"}],
                    }}
                return 200, {"success": True, "data": {
                    "etag": "etag-v2",
                    "templates": [
                        {"staff_id": 1, "template_id": 10, "fmd": "AQID"},
                        {"staff_id": 2, "template_id": 11, "fmd": "BAUG"},
                    ],
                }}

        core = ReaderAgentCore(
            store=self.store,
            sdk_factory=lambda: None,
            http_factory=FakeHttp,
            logger=self.logger,
            clock=lambda: now[0],
        )
        core.provision(reference="RAG-1", signing_key="ab" * 32, api_base="https://api.test")

        g1 = core.sync_gallery()
        self.assertEqual(len(g1["templates"]), 1)

        # Within TTL with matching etag -> served from cache without HTTP
        now[0] += 5.0
        g1_cached = core.sync_gallery()
        self.assertEqual(len(g1_cached["templates"]), 1)
        self.assertEqual(len([c for c in http_calls if c[0].endswith("/gallery/")]), 1)

        # Heartbeat reveals new gallery_etag -> sync_gallery fetches updated templates immediately
        core.heartbeat()
        g2 = core.sync_gallery()
        self.assertEqual(len(g2["templates"]), 2)
        self.assertEqual(g2["etag"], "etag-v2")

    def test_failed_capture_reports_session_fail_to_backend(self):
        from agent_core import AgentError, ReaderAgentCore

        calls = []

        class FakeHttp:
            def __init__(self, base):
                pass

            def post_json(self, path, body, headers):
                calls.append((path, headers.get("X-JONE-Agent-Kind"), json.loads(body.decode("utf-8"))))
                return 200, {"success": True, "data": {"status": "FAILED"}}

        core = ReaderAgentCore(store=self.store, sdk_factory=lambda: None, http_factory=FakeHttp, logger=self.logger)
        core.provision(reference="RAG-1", signing_key="ab" * 32, api_base="https://api.test")

        agent._report_request_failure(
            core,
            {"kind": "capture", "session": "TOK-1"},
            AgentError("NO_ENROLLED_STAFF", "No staff member has an approved fingerprint yet."),
            self.logger,
        )
        fail_call = next((c for c in calls if c[1] == "SESSION_FAIL"), None)
        self.assertIsNotNone(fail_call)
        self.assertEqual(fail_call[2]["session_token"], "TOK-1")
        self.assertEqual(fail_call[2]["error_code"], "NO_ENROLLED_STAFF")

    def test_incremental_enrollment_stops_when_sdk_reports_ready(self):
        from agent_core import ReaderAgentCore

        class FakeDevice:
            name = "dev-0"
            product = "U.are.U 4500"
            serial = "SER-1"
            firmware = "1.0"

        class FakeSdk:
            def __init__(self):
                self.captures = 0
                self.added = 0
                self.finished = False
                self.report = type("R", (), {
                    "devices": [FakeDevice()], "sdk_available": True, "sdk_version": "3",
                    "driver_mode": "LEGACY_SDK", "usb_present": True, "detail": "ok",
                    "as_dict": lambda s: {"sdk_available": True, "driver_mode": "LEGACY_SDK"},
                })()

            def load(self): return self.report
            def refresh_status(self): return self.report
            def query_devices(self): return [FakeDevice()]
            def open(self, d): pass
            def close(self): pass
            def status(self): return {"finger_detected": False}
            def capture(self, timeout_ms=15000):
                self.captures += 1
                return b"raw"
            def fmd_from_capture(self, cap, finger_position=2):
                return b"fmd-" + bytes([self.captures])
            def start_enrollment(self): pass
            def add_to_enrollment(self, fmd):
                self.added += 1
                return "ready" if self.added >= 4 else "more_data"
            def create_enrollment_fmd(self):
                return b"E" * 200
            def finish_enrollment(self):
                self.finished = True

        sent = []

        class FakeHttp:
            def __init__(self, base): pass
            def post_json(self, path, body, headers):
                sent.append((path, json.loads(body.decode("utf-8"))))
                return 200, {"success": True, "data": {"template_id": 9, "status": "PENDING"}}

        sdk = FakeSdk()
        core = ReaderAgentCore(store=self.store, sdk_factory=lambda: sdk, http_factory=FakeHttp, logger=self.logger)
        core.provision(reference="RAG-1", signing_key="ab" * 32, api_base="https://api.test")
        res = core.enroll(session_token="S-1", staff_id=42, finger_position="LEFT_RING")
        self.assertEqual(res["template_id"], 9)
        self.assertEqual(sdk.added, 4)
        self.assertTrue(sdk.finished)
        self.assertEqual(sent[0][1]["views"], 4)
        self.assertEqual(sent[0][1]["finger_position"], "LEFT_RING")

    def test_cold_start_uri_enters_supervised_run_loop_unless_once(self):
        entered = {"supervised": 0, "executed": 0}
        orig_make_core = agent.make_core
        orig_exec = agent.execute_request
        orig_sup = agent.run_supervised
        agent.make_core = lambda store, logger: object()
        agent.execute_request = lambda core, req, logger: entered.__setitem__("executed", entered["executed"] + 1) or {}
        agent.run_supervised = lambda store, logger, *, once=False: entered.__setitem__("supervised", entered["supervised"] + 1) or 0
        try:
            rc = agent.main(["--state-dir", str(self.paths.root), "--uri", "jone-reader:start?api=https://api.test"])
            self.assertEqual(rc, 0)
            self.assertEqual(entered["executed"], 1)
            self.assertEqual(entered["supervised"], 1)

            rc_once = agent.main(["--state-dir", str(self.paths.root), "--uri", "jone-reader:start?api=https://api.test", "--once"])
            self.assertEqual(rc_once, 0)
            self.assertEqual(entered["executed"], 2)
            self.assertEqual(entered["supervised"], 1)
        finally:
            agent.make_core = orig_make_core
            agent.execute_request = orig_exec
            agent.run_supervised = orig_sup


if __name__ == "__main__":
    unittest.main()
