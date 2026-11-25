"""Device state machine and signed protocol client for the J-ONE Reader Agent.

This module contains every decision the agent makes, with the vendor SDK and the
HTTP transport injected, so it can be exercised end to end on a machine with no
reader attached (see tests/agent/). ``jone_reader_agent.py`` is only the
Windows shell around it: CLI parsing, the request spool, and the run loop.

DEVICE STATES (mirror what the dashboard shows)
===============================================
``not_provisioned``     agent has no signing key yet -- run one-click setup
``sdk_unavailable``     dpfpdd.dll / dpfj.dll missing (driver installed is not enough)
``driver_mode_mismatch`` reader is on USB but bound to the WBF (Windows Hello)
                        driver, which the SDK cannot use
``not_connected``       no reader on the USB bus
``initializing``        dpfpdd_init/open in flight
``ready``               reader open, waiting for a finger
``busy``                capturing / another program holds the reader
``error``               last operation failed; the detail explains why

Every state is derived from something actually observed. There is no code path
that reports ``ready`` because a previous test said ``true``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone as dt_timezone
from typing import Any, Callable, Protocol

STATE_NOT_PROVISIONED = "not_provisioned"
STATE_SDK_UNAVAILABLE = "sdk_unavailable"
STATE_DRIVER_MODE_MISMATCH = "driver_mode_mismatch"
STATE_NOT_CONNECTED = "not_connected"
STATE_INITIALIZING = "initializing"
STATE_READY = "ready"
STATE_BUSY = "busy"
STATE_ERROR = "error"

CAPTURE_BUDGET_SECONDS = 20
ENROLL_IMPRESSIONS = 3


class AgentError(Exception):
    """A failure the receptionist can act on."""

    def __init__(self, code: str, message: str, *, status: str = STATE_ERROR, retry: bool = True):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry = retry


class NetworkError(AgentError):
    def __init__(self, message: str):
        super().__init__("NETWORK_UNAVAILABLE", message, status="offline")


class SdkLike(Protocol):  # pragma: no cover - structural typing only
    report: Any

    def load(self): ...
    def initialize(self): ...
    def open(self, device): ...
    def close(self): ...
    def query_devices(self): ...
    def capture(self, timeout_ms: int = ...): ...
    def fmd_from_capture(self, capture, finger_position: int = ...): ...
    def identify(self, probe_fmd, gallery, *, threshold: int, max_candidates: int = ...): ...
    def enrollment_fmd(self, fmds): ...


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class HttpClient:
    """The ONLY network path the agent has: HTTPS JSON to the J-ONE API.

    No localhost server, no inbound port, no browser involvement.
    """

    def __init__(self, api_base: str, *, timeout: int = 20, logger: logging.Logger | None = None):
        self.api_base = str(api_base or "").rstrip("/")
        self.timeout = timeout
        self.logger = logger or logging.getLogger("jone.reader.agent")
        if not self.api_base.startswith("https://") and not self.api_base.startswith("http://localhost"):
            raise AgentError(
                "API_BASE_INSECURE",
                "The J-ONE API address must be HTTPS; refusing to send signed reader requests to "
                f"{self.api_base or '(empty)'}.",
                retry=False,
            )

    def post_json(self, path: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict]:
        url = self.api_base + path
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", "JONE-ReaderAgent/1.0")
        for key, value in headers.items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return response.status, _json(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read() if hasattr(exc, "read") else b""
            return exc.code, _json(raw)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NetworkError(
                "The J-ONE server could not be reached from this computer. Check the internet connection "
                f"and try again ({getattr(exc, 'reason', exc)})."
            )


def _json(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {"data": data}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"message": "The server response could not be read."}


# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------
class ReaderAgentCore:
    def __init__(
        self,
        *,
        store,
        sdk_factory: Callable[[], SdkLike],
        http_factory: Callable[[str], HttpClient] | None = None,
        logger: logging.Logger | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self._sdk_factory = sdk_factory
        self._http_factory = http_factory or (lambda base: HttpClient(base, logger=self.logger))
        self.logger = logger or logging.getLogger("jone.reader.agent")
        self.clock = clock
        self._sdk: SdkLike | None = None
        self._state = STATE_NOT_CONNECTED
        self._detail = ""
        self._device = None
        self._cached_gallery: dict | None = None

    # -- configuration ------------------------------------------------------
    @property
    def config(self) -> dict:
        return self.store.load_config()

    def is_provisioned(self) -> bool:
        return self.store.is_provisioned()

    def provision(self, *, reference: str, signing_key: str, api_base: str, workstation_reference: str = "") -> dict:
        config = dict(self.store.load_config())
        config.update({
            "reference": str(reference),
            "signing_key": str(signing_key),
            "api_base": str(api_base).rstrip("/"),
            "workstation_reference": str(workstation_reference or ""),
        })
        self.store.save_config(config)
        self.logger.info("Provisioned as %s for %s", config["reference"], config["api_base"])
        return config

    # -- device -------------------------------------------------------------
    def _sdk_or_none(self) -> SdkLike | None:
        if self._sdk is None:
            try:
                self._sdk = self._sdk_factory()
                self._sdk.load()
            except Exception as exc:
                self._sdk = None
                self._set_state(_state_from_sdk_error(exc), getattr(exc, "message", str(exc)))
                self.logger.warning("SDK load failed: %s", getattr(exc, "message", exc))
                return None
        return self._sdk

    def _set_state(self, state: str, detail: str) -> None:
        self._state = state
        self._detail = detail

    def device_state(self) -> dict:
        """Describe the sensor without capturing anything."""
        if not self.is_provisioned():
            self._set_state(STATE_NOT_PROVISIONED, "This computer is not set up for fingerprint capture yet.")
        else:
            sdk = self._sdk_or_none()
            if sdk is not None:
                report = getattr(sdk, "report", None)
                devices = getattr(report, "devices", []) if report is not None else []
                if devices:
                    if self._device is None:
                        try:
                            self._device = devices[0]
                        except Exception:  # pragma: no cover
                            self._device = None
                    self._set_state(STATE_READY, getattr(report, "detail", "") or "Reader ready.")
                else:
                    mode = getattr(report, "driver_mode", "UNKNOWN") if report is not None else "UNKNOWN"
                    if mode == "WBF" or getattr(report, "usb_present", False):
                        self._set_state(
                            STATE_DRIVER_MODE_MISMATCH,
                            getattr(report, "detail", "") or "The reader is bound to a driver the SDK cannot use.",
                        )
                    else:
                        self._set_state(STATE_NOT_CONNECTED, getattr(report, "detail", "") or "Reader not connected.")
        report = getattr(getattr(self._sdk, "report", None), "as_dict", None)
        sdk_report = report() if callable(report) else {}
        return {
            "device_status": self._state,
            "detail": self._detail,
            "device_serial": getattr(self._device, "serial", "") if self._device is not None else "",
            "model": getattr(self._device, "product", "") if self._device is not None else "",
            "firmware": getattr(self._device, "firmware", "") if self._device is not None else "",
            "driver_mode": sdk_report.get("driver_mode", "UNKNOWN"),
            "sdk_available": bool(sdk_report.get("sdk_available")),
            "sdk_version": sdk_report.get("sdk_version", ""),
            "busy": self._state == STATE_BUSY,
        }

    def ensure_ready(self) -> SdkLike:
        """Load, initialise and open the reader; auto-recovers from a replug."""
        sdk = self._sdk_or_none()
        if sdk is None:
            raise self._last_sdk_error()

        for attempt in (1, 2):
            try:
                devices = sdk.query_devices()
                if not devices:
                    self._set_state(STATE_INITIALIZING, "Looking for the fingerprint reader…")
                    devices = sdk.initialize()
                if not devices:
                    report = getattr(sdk, "report", None)
                    if getattr(report, "usb_present", False):
                        self._set_state(STATE_DRIVER_MODE_MISMATCH, getattr(report, "detail", ""))
                        raise AgentError(
                            "DRIVER_MODE_MISMATCH",
                            getattr(report, "detail", "")
                            or "The reader is plugged in but bound to a driver the DigitalPersona SDK cannot use.",
                            status=STATE_DRIVER_MODE_MISMATCH,
                            retry=False,
                        )
                    self._set_state(STATE_NOT_CONNECTED, "No fingerprint reader is connected to this computer.")
                    raise AgentError(
                        "READER_NOT_CONNECTED",
                        "No fingerprint reader is connected to this computer. Plug the DigitalPersona 4500 in.",
                        status=STATE_NOT_CONNECTED,
                    )
                if self._device is None or getattr(self._device, "name", None) != getattr(devices[0], "name", None):
                    sdk.open(devices[0])
                    self._device = devices[0]
                self._set_state(STATE_READY, "Fingerprint reader ready.")
                return sdk
            except AgentError:
                raise
            except Exception as exc:
                code = getattr(exc, "code", "DEVICE_ERROR")
                if attempt == 1 and code in {"READER_GONE", "DEVICE_OPEN_FAILED", "SERVICE_UNAVAILABLE"}:
                    self.logger.warning("Reader unavailable (%s); re-initialising once", code)
                    try:
                        sdk.close()
                    except Exception:
                        pass
                    self._device = None
                    self._sdk = None
                    time.sleep(1.0)
                    sdk = self._sdk_or_none() or sdk
                    continue
                raise AgentError(
                    getattr(exc, "code", "DEVICE_ERROR"),
                    getattr(exc, "message", str(exc)),
                    status=getattr(exc, "status", STATE_ERROR),
                    retry=getattr(exc, "retry", True),
                )
        raise AgentError("READER_NOT_CONNECTED", "The fingerprint reader is not available.", status=STATE_NOT_CONNECTED)

    def _last_sdk_error(self) -> AgentError:
        return AgentError(self._state.upper(), self._detail or "The fingerprint reader is not available.",
                          status=self._state, retry=False)

    # -- signed protocol ----------------------------------------------------
    def _sign_headers(self, kind: str, body: bytes, config: dict) -> tuple[dict, str]:
        nonce = secrets.token_hex(16)
        timestamp = str(int(self.clock()))
        message = "\n".join([
            "jone-reader-agent-v1",
            config["reference"],
            nonce,
            timestamp,
            kind,
            hashlib.sha256(body or b"").hexdigest(),
        ]).encode("utf-8")
        signature = hmac.new(bytes.fromhex(config["signing_key"]), message, hashlib.sha256).hexdigest()
        headers = {
            "X-JONE-Agent-Reference": config["reference"],
            "X-JONE-Agent-Nonce": nonce,
            "X-JONE-Agent-Timestamp": timestamp,
            "X-JONE-Agent-Kind": kind,
            "X-JONE-Agent-Signature": signature,
        }
        return headers, nonce

    def call(self, kind: str, path: str, payload: dict) -> dict:
        config = self.config
        if not (config.get("reference") and config.get("signing_key")):
            raise AgentError(
                "AGENT_NOT_PROVISIONED",
                "This computer is not set up for fingerprint capture yet. Open the J-ONE dashboard and "
                "press 'Set up fingerprint reader on this computer'.",
                status=STATE_NOT_PROVISIONED,
                retry=False,
            )
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers, nonce = self._sign_headers(kind, body, config)
        client = self._http_factory(config["api_base"])
        status, data = client.post_json(path, body, headers)
        if 200 <= status < 300 and data.get("success", True):
            self.logger.info("%s ok (nonce %s)", kind, nonce[:8])
            return data.get("data", data)
        code = str(data.get("code") or f"HTTP_{status}")
        message = str(data.get("message") or "The J-ONE server refused the reader request.")
        self.logger.warning("%s refused: %s %s", kind, code, message)
        raise AgentError(code, message, status="error", retry=code not in {
            "AGENT_UNKNOWN", "AGENT_SIGNATURE_INVALID", "AGENT_NOT_PROVISIONED", "AGENT_TIMESTAMP_STALE",
        })

    # -- heartbeat / gallery ------------------------------------------------
    def heartbeat(self) -> dict:
        state = self.device_state()
        state.update({
            "agent_version": "1.0.0",
            "python": _python_version(),
            "hostname": _hostname(),
        })
        return self.call("HEARTBEAT", "/api/admin/staff-operations/reader-agent/agent/heartbeat/", {"state": state})

    def sync_gallery(self, *, force: bool = False) -> dict:
        cached = self._cached_gallery or self.store.load_gallery()
        if cached.get("templates") and not force:
            self._cached_gallery = cached
            return cached
        data = self.call("GALLERY", "/api/admin/staff-operations/reader-agent/agent/gallery/", {})
        gallery = {"etag": data.get("etag", ""), "templates": data.get("templates", [])}
        self.store.save_gallery(gallery)
        self._cached_gallery = gallery
        self.logger.info("Gallery synced: %d enrolled template(s)", len(gallery["templates"]))
        return gallery

    # -- capture ------------------------------------------------------------
    def _capture_fmd(self, sdk: SdkLike, *, finger_position: int = 2, budget: int = CAPTURE_BUDGET_SECONDS):
        deadline = self.clock() + budget
        last_error: Exception | None = None
        self._set_state(STATE_BUSY, "Reading fingerprint…")
        while self.clock() < deadline:
            try:
                capture = sdk.capture(timeout_ms=int(min(8000, max(2000, (deadline - self.clock()) * 1000))))
                return sdk.fmd_from_capture(capture, finger_position)
            except Exception as exc:
                last_error = exc
                if getattr(exc, "status", "") in {"capture_quality", "busy"}:
                    continue
                if getattr(exc, "status", "") == STATE_NOT_CONNECTED:
                    self._device = None
                    self._sdk = None
                    raise AgentError(
                        "READER_DISCONNECTED",
                        "The fingerprint reader was disconnected during the read.",
                        status=STATE_NOT_CONNECTED,
                    )
                raise
        raise AgentError(
            "CAPTURE_TIMEOUT",
            getattr(last_error, "message", "") or "No usable fingerprint was read in time.",
            status="capture_quality",
        )

    def identify(self, *, session_token: str, action: str) -> dict:
        """Capture a finger, match it locally with dpfj_identify, tell the API."""
        sdk = self.ensure_ready()
        gallery = self.sync_gallery()
        templates = gallery.get("templates", [])
        if not templates:
            raise AgentError(
                "NO_ENROLLED_STAFF",
                "No staff member has an approved fingerprint on this system yet, so there is nothing to match.",
                retry=False,
            )
        config = self.config
        threshold = int(config.get("match_threshold") or 2_147_483)

        probe = self._capture_fmd(sdk)
        self._set_state(STATE_BUSY, "Comparing fingerprint…")
        rows = [(index, _b64decode(row.get("fmd", ""))) for index, row in enumerate(templates)]
        rows = [(index, fmd) for index, fmd in rows if fmd]
        matches = sdk.identify(probe, rows, threshold=threshold, max_candidates=5) if rows else []
        candidates = [
            {
                "staff_id": templates[index].get("staff_id"),
                "template_id": templates[index].get("template_id"),
                "score": int(score),
            }
            for index, score in matches
        ]
        self.logger.info("Identify produced %d candidate(s)", len(candidates))
        payload = {
            "session_token": session_token,
            "candidates": candidates,
            "captured_at": datetime.now(dt_timezone.utc).isoformat(),
            "device_state": self.device_state(),
            "nonce": "",  # filled by the server from the header
        }
        try:
            result = self.call("IDENTIFY", "/api/admin/staff-operations/reader-agent/agent/identify/", payload)
            self._set_state(STATE_READY, "Fingerprint verified.")
            return result
        except AgentError:
            self._set_state(STATE_READY, "Fingerprint not accepted.")
            raise

    def enroll(self, *, session_token: str, staff_id: int, finger_position: str = "RIGHT_INDEX",
               impressions: int = ENROLL_IMPRESSIONS) -> dict:
        sdk = self.ensure_ready()
        position = {"RIGHT_INDEX": 2, "RIGHT_THUMB": 1, "LEFT_INDEX": 7, "LEFT_THUMB": 6}.get(
            str(finger_position or "").upper(), 2
        )
        fmds = []
        for index in range(impressions):
            self.logger.info("Enrolment impression %d/%d", index + 1, impressions)
            fmds.append(self._capture_fmd(sdk, finger_position=position))
        self._set_state(STATE_BUSY, "Building enrolment template…")
        enrollment = sdk.enrollment_fmd(fmds)
        payload = {
            "session_token": session_token,
            "staff_id": staff_id,
            "finger_position": str(finger_position or "RIGHT_INDEX").upper(),
            "fmd": _b64encode(enrollment),
            "views": len(fmds),
        }
        result = self.call("ENROLL", "/api/admin/staff-operations/reader-agent/agent/enroll/", payload)
        self._set_state(STATE_READY, "Fingerprint captured for enrolment.")
        return result

    def close(self) -> None:
        if self._sdk is not None:
            try:
                self._sdk.close()
            except Exception:
                pass


def _state_from_sdk_error(exc: Exception) -> str:
    status = getattr(exc, "status", "") or ""
    if status in {STATE_SDK_UNAVAILABLE, STATE_NOT_CONNECTED, STATE_DRIVER_MODE_MISMATCH,
                  STATE_READY, STATE_BUSY, STATE_ERROR}:
        return status
    code = getattr(exc, "code", "")
    if code in {"SDK_UNAVAILABLE", "SERVICE_UNAVAILABLE"}:
        return STATE_SDK_UNAVAILABLE
    return STATE_ERROR


def _b64decode(value: str) -> bytes:
    import base64

    try:
        return base64.b64decode(value, validate=True)
    except Exception:
        return b""


def _b64encode(value: bytes) -> str:
    import base64

    return base64.b64encode(value).decode("ascii")


def _python_version() -> str:
    import platform

    return platform.python_version()


def _hostname() -> str:
    import socket

    try:
        return socket.gethostname()[:80]
    except Exception:  # pragma: no cover
        return ""
