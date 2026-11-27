"""J-ONE Reader Agent -- the native Windows component for the DigitalPersona 4500.

WHAT IT IS, AND WHAT IT DELIBERATELY IS NOT
===========================================
A single small process that owns the fingerprint reader on the reception PC.
It is started automatically (logon entry) and, if it is not running, Windows
starts it when the dashboard asks for a fingerprint through the ``jone-reader:``
URL scheme. There is:

  * no HTTP server, no port, no ``127.0.0.1:8787``,
  * no bridge service to start by hand, no PowerShell to run,
  * no browser access to the sensor, and no biometric data in the browser.

The browser only ever receives a *result* (which staff member, which action),
never an image or a template; the agent talks HTTPS to the J-ONE API.

USAGE (all of it is automatic; the flags exist for installation and support)
===========================================================================
    jone_reader_agent.py --run                     # started at logon
    jone_reader_agent.py --uri "jone-reader:capture?session=...&api=..."
    jone_reader_agent.py --provision <payload>     # one-click setup from the dashboard
    jone_reader_agent.py --status                  # JSON: is the reader usable?
    jone_reader_agent.py --diagnose                # JSON: SDK/driver/USB detail
    jone_reader_agent.py --self-test-capture       # technician: read one finger, send nothing
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_core import AgentError, ReaderAgentCore, STATE_NOT_PROVISIONED  # noqa: E402
from agent_store import AgentPaths, AgentStore, SingleInstance, configure_logging  # noqa: E402

AGENT_VERSION = "1.0.0"
HEARTBEAT_INTERVAL_SECONDS = 30
SPOOL_POLL_SECONDS = 0.5


def make_core(store: AgentStore, logger) -> ReaderAgentCore:
    from dp_sdk import DpSdk

    return ReaderAgentCore(store=store, sdk_factory=DpSdk, logger=logger)


# --------------------------------------------------------------------------
# URI handling
# --------------------------------------------------------------------------
def parse_uri(raw: str) -> tuple[str, dict]:
    raw = str(raw or "").strip().strip('"')
    if not raw:
        raise SystemExit("The agent was started without a request.")
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme != "jone-reader":
        raise SystemExit(f"Unsupported request: {raw}")
    verb = (parsed.netloc or parsed.path or "").strip("/").lower() or "start"
    query = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items()}
    return verb, query


def request_from_uri(raw: str) -> dict:
    verb, query = parse_uri(raw)
    if verb == "provision":
        payload = _decode_provision(query.get("payload", ""))
        return {"kind": "provision", **payload}
    if verb in {"capture", "identify"}:
        return {
            "kind": "capture",
            "session": query.get("session", ""),
            "api": query.get("api", ""),
            "action": (query.get("action") or "CLOCK_IN").upper(),
            "requested_at": time.time(),
        }
    if verb == "enroll":
        return {
            "kind": "enroll",
            "session": query.get("session", ""),
            "api": query.get("api", ""),
            "staff_id": query.get("staff_id", ""),
            "finger_position": query.get("finger_position") or "RIGHT_INDEX",
            "requested_at": time.time(),
        }
    if verb in {"start", "status", "repair"}:
        return {"kind": "status", "api": query.get("api", ""), "requested_at": time.time()}
    raise SystemExit(f"Unsupported reader request: {verb}")


def _decode_provision(payload: str) -> dict:
    padded = payload + "=" * (-len(payload) % 4)
    data = json.loads(base64.urlsafe_b64decode(padded.encode()).decode("utf-8"))
    if not data.get("reference") or not data.get("key") or not data.get("api"):
        raise SystemExit("The setup code is incomplete; generate it again from the J-ONE dashboard.")
    return {"reference": data["reference"], "signing_key": data["key"], "api_base": data["api"],
            "workstation_reference": data.get("workstation_reference", "")}


# --------------------------------------------------------------------------
# Request execution
# --------------------------------------------------------------------------
def execute_request(core: ReaderAgentCore, request: dict, logger) -> dict:
    kind = request.get("kind")
    if kind == "provision":
        core.provision(
            reference=request["reference"], signing_key=request["signing_key"],
            api_base=request["api_base"], workstation_reference=request.get("workstation_reference", ""),
        )
        try:
            core.heartbeat()
        except AgentError as exc:
            logger.warning("Provisioned, but the first heartbeat failed: %s", exc.message)
        return {"status": "provisioned", "reference": request["reference"]}

    if not core.is_provisioned():
        raise AgentError(
            "AGENT_NOT_PROVISIONED",
            "This computer is not set up for fingerprint capture yet. In the J-ONE dashboard press "
            "'Set up fingerprint reader on this computer' once.",
            status=STATE_NOT_PROVISIONED, retry=False,
        )
    _adopt_api_base(core, request.get("api"))

    if kind == "status":
        state = core.device_state()
        try:
            core.heartbeat()
        except AgentError as exc:
            logger.warning("Heartbeat during status failed: %s", exc.message)
        return state

    if kind == "capture":
        session = str(request.get("session") or "")
        if not session:
            raise AgentError("SESSION_MISSING", "The fingerprint request has no session token.", retry=False)
        return core.identify(session_token=session, action=str(request.get("action") or "CLOCK_IN"))

    if kind == "enroll":
        session = str(request.get("session") or "")
        staff_id = request.get("staff_id")
        if not session or not staff_id:
            raise AgentError("SESSION_MISSING", "The enrolment request is incomplete.", retry=False)
        return core.enroll(
            session_token=session, staff_id=int(staff_id),
            finger_position=str(request.get("finger_position") or "RIGHT_INDEX"),
        )

    raise AgentError("REQUEST_UNKNOWN", f"Unknown reader request: {kind}", retry=False)


def _adopt_api_base(core: ReaderAgentCore, api: str | None) -> None:
    """Keep the provisioned API base; only fill it in if it is missing."""
    if not api:
        return
    config = core.config
    if config.get("api_base"):
        return
    config["api_base"] = str(api).rstrip("/")
    core.store.save_config(config)


# --------------------------------------------------------------------------
# Run loop
# --------------------------------------------------------------------------
#: Restart backoff for the supervised run loop. A crashed agent must come back
#: on its own: the receptionist cannot be asked to start a program, and a dead
#: agent is otherwise invisible until the next logon (repair 2026-11-26).
RESTART_BACKOFF_START_SECONDS = 2.0
RESTART_BACKOFF_MAX_SECONDS = 60.0


def run_supervised(store: AgentStore, logger, *, once: bool = False) -> int:
    """Run the agent forever, restarting the run loop if it ever crashes.

    The installer starts the agent at logon and the ``jone-reader:`` scheme
    starts it on demand, but neither of those recovers a process that dies while
    the shift is running (an unexpected exception, a Windows session event, an
    antivirus kill). Without this the desk silently loses fingerprint capture
    until somebody logs in again — exactly the "it works, then it stops working,
    nobody knows why" pattern this repair is about.

    The single-instance mutex is deliberately held by the caller across
    restarts, so a restarted loop cannot race a second copy of itself.
    """
    delay = RESTART_BACKOFF_START_SECONDS
    while True:
        try:
            return run(store, logger, once=once)
        except KeyboardInterrupt:  # pragma: no cover - interactive only
            logger.info("J-ONE Reader Agent stopped by the operator.")
            return 0
        except Exception:  # noqa: BLE001 - the whole point is to survive anything
            logger.exception(
                "J-ONE Reader Agent crashed; restarting in %.0f s", delay,
            )
            try:
                # Tell the API the agent is alive again as soon as it can, so the
                # dashboard stops reporting "agent not running" after a restart.
                make_core(store, logger).heartbeat()
            except Exception:  # noqa: BLE001 - offline restarts are expected
                pass
            time.sleep(delay)
            delay = min(delay * 2, RESTART_BACKOFF_MAX_SECONDS)


def run(store: AgentStore, logger, *, once: bool = False) -> int:
    core = make_core(store, logger)
    last_heartbeat = 0.0
    logger.info("J-ONE Reader Agent %s starting (state dir %s)", AGENT_VERSION, store.paths.root)
    while True:
        handled = False
        for path in store.pending_requests():
            request = store.claim_request(path)
            if not request:
                continue
            handled = True
            logger.info("Handling %s request", request.get("kind"))
            try:
                result = execute_request(core, request, logger)
                logger.info("Request finished: %s", json.dumps(result, default=str)[:400])
            except AgentError as exc:
                logger.warning("Request failed: %s %s", exc.code, exc.message)
                # Report the local reason (reader unplugged, driver mode, SDK
                # missing) so the dashboard panel can show the real cause
                # instead of only "no fingerprint was read in time".
                try:
                    core.heartbeat()
                except Exception:
                    pass
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("Unexpected agent failure: %s", exc)
        now = time.time()
        if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
            last_heartbeat = now
            try:
                core.heartbeat()
            except AgentError as exc:
                logger.debug("Heartbeat skipped: %s", exc.message)
            except Exception as exc:  # pragma: no cover
                logger.debug("Heartbeat error: %s", exc)
        if once and not handled:
            return 0
        time.sleep(SPOOL_POLL_SECONDS)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="J-ONE Reader Agent (DigitalPersona 4500)")
    parser.add_argument("--uri", help="jone-reader: URI passed by Windows")
    parser.add_argument("--provision", help="one-click setup payload from the dashboard")
    parser.add_argument("--run", action="store_true", help="run the background agent")
    parser.add_argument("--once", action="store_true", help="process pending requests and exit")
    parser.add_argument("--status", action="store_true", help="print the reader state as JSON")
    parser.add_argument("--diagnose", action="store_true", help="print SDK/driver/USB diagnostics as JSON")
    parser.add_argument("--self-test-capture", action="store_true", help="read one finger without sending anything")
    parser.add_argument("--state-dir", help="override the state directory (support/diagnostics)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    paths = AgentPaths(Path(args.state_dir)) if args.state_dir else AgentPaths()
    store = AgentStore(paths)
    logger = configure_logging(paths, verbose=args.verbose)

    if args.diagnose:
        from dp_sdk import DpSdk, usb_reader_present

        report = {"usb_present": usb_reader_present()}
        try:
            sdk = DpSdk()
            report["sdk"] = sdk.load().as_dict()
        except Exception as exc:
            report["sdk_error"] = {"code": getattr(exc, "code", "ERROR"),
                                   "message": getattr(exc, "message", str(exc))}
        report["state_dir"] = str(paths.root)
        report["agent_version"] = AGENT_VERSION
        print(json.dumps(report, indent=2, default=str))
        return 0

    if args.self_test_capture:
        core = make_core(store, logger)
        try:
            sdk = core.ensure_ready()
            capture = sdk.capture(timeout_ms=15000)
            fmd = sdk.fmd_from_capture(capture)
            print(json.dumps({"ok": True, "image": f"{capture.width}x{capture.height}@{capture.dpi}",
                              "fmd_bytes": len(fmd), "note": "Nothing was sent anywhere."}, indent=2))
            return 0
        except Exception as exc:
            print(json.dumps({"ok": False, "code": getattr(exc, "code", "ERROR"),
                              "message": getattr(exc, "message", str(exc))}, indent=2))
            return 2

    if args.status:
        core = make_core(store, logger)
        print(json.dumps(core.device_state(), indent=2, default=str))
        return 0

    if args.provision:
        core = make_core(store, logger)
        result = execute_request(core, {"kind": "provision", **_decode_provision(args.provision)}, logger)
        print(json.dumps(result, indent=2, default=str))
        # A provisioned agent should also start looking after the reader now.
        return run(store, logger) if args.run else 0

    if args.uri:
        request = request_from_uri(args.uri)
        instance = SingleInstance(paths.mutex_name)
        if not instance.acquire():
            store.push_request(request)
            logger.info("Agent already running; request handed over")
            return 0
        try:
            core = make_core(store, logger)
            try:
                execute_request(core, request, logger)
            except AgentError as exc:
                logger.warning("Request failed: %s %s", exc.code, exc.message)
            if request.get("kind") == "provision" or args.run:
                return run(store, logger)
            return 0
        finally:
            instance.release()

    if args.run or args.once:
        instance = SingleInstance(paths.mutex_name)
        if not instance.acquire():
            logger.info("J-ONE Reader Agent is already running.")
            return 0
        try:
            return run_supervised(store, logger, once=args.once)
        finally:
            instance.release()

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
