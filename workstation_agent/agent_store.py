"""Local state, DPAPI protection and on-disk plumbing for the J-ONE Reader Agent.

Everything the agent persists lives under
``%LOCALAPPDATA%\\JONE\\ReaderAgent`` (per user, no administrator rights
required):

``config.bin``    DPAPI-encrypted JSON: agent reference, signing key, API base
``gallery.bin``   DPAPI-encrypted JSON: the enrolled FMD templates for 1:N match
``requests\\``     hand-off folder between a browser URI launch and the running agent
``logs\\agent.log`` rotating text log (no biometric data, ever)

DPAPI (``CryptProtectData``) ties the encrypted blobs to the Windows user
account, so copying the folder to another machine -- or reading it as another
user -- yields nothing usable. On non-Windows machines (continuous integration)
protection is refused unless ``JONE_AGENT_ALLOW_PLAIN_STATE=1`` is set, so a
test can never silently produce an unprotected production state file.
"""
from __future__ import annotations

import ctypes
import hashlib
import itertools
import json
import logging
import os
import platform
import secrets
import sys
import tempfile
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler

_SPOOL_SEQ = itertools.count()
from pathlib import Path

IS_WINDOWS = platform.system() == "Windows"
APP_DIR_NAME = "JONE"
AGENT_DIR_NAME = "ReaderAgent"


def default_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if not base:
        base = str(Path.home() / ".local" / "share")
    return Path(base) / APP_DIR_NAME / AGENT_DIR_NAME


@dataclass
class AgentPaths:
    root: Path = field(default_factory=default_root)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.config = self.root / "config.bin"
        self.gallery = self.root / "gallery.bin"
        self.requests = self.root / "requests"
        self.logs = self.root / "logs"
        self.log_file = self.logs / "agent.log"
        # Use Local\ so a standard non-elevated Windows 11 account can create
        # the mutex without SeCreateGlobalPrivilege (Global\ fails with
        # ERROR_ACCESS_DENIED on standard user logins and caused duplicate runs).
        try:
            is_default = self.root.resolve() == default_root().resolve()
        except OSError:
            is_default = False
        if is_default:
            self.mutex_name = "Local\\JONE-Reader-Agent"
        else:
            suffix = hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()[:8]
            self.mutex_name = f"Local\\JONE-Reader-Agent-{suffix}"

    def ensure(self) -> "AgentPaths":
        self.root.mkdir(parents=True, exist_ok=True)
        self.requests.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        return self


# --------------------------------------------------------------------------
# DPAPI
# --------------------------------------------------------------------------
class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def _blob_bytes(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def protect(data: bytes, *, scope: str = "user") -> bytes:
    """CryptProtectData with the current user (or machine) scope."""
    if not IS_WINDOWS:
        if os.environ.get("JONE_AGENT_ALLOW_PLAIN_STATE") == "1":
            return b"PLAIN:" + data
        raise RuntimeError(
            "DPAPI protection is only available on Windows. "
            "Set JONE_AGENT_ALLOW_PLAIN_STATE=1 only in automated tests."
        )
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    in_blob, _keep_buf = _blob(data)
    out_blob = _DataBlob()
    flags = 0x1 if scope == "machine" else 0x0  # CRYPTPROTECT_LOCAL_MACHINE
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob), "JONE Reader Agent", None, None, None, flags, ctypes.byref(out_blob)
    )
    if not ok:
        raise OSError(f"CryptProtectData failed (error {ctypes.get_last_error()})")
    try:
        return _blob_bytes(out_blob)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def unprotect(data: bytes) -> bytes:
    if data.startswith(b"PLAIN:"):
        return data[len(b"PLAIN:"):]
    if not IS_WINDOWS:
        raise RuntimeError("This agent state was protected on Windows and cannot be read here.")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    in_blob, _keep_buf = _blob(data)
    out_blob = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    )
    if not ok:
        raise OSError(
            "CryptUnprotectData failed: this agent state belongs to another Windows user "
            f"(error {ctypes.get_last_error()})"
        )
    try:
        return _blob_bytes(out_blob)
    finally:
        kernel32.LocalFree(out_blob.pbData)


# --------------------------------------------------------------------------
# Config + gallery stores
# --------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "reference": "",
    "signing_key": "",
    "api_base": "",
    "workstation_reference": "",
    "match_threshold": 2_147_483,
    "agent_version": "1.0.0",
}


class AgentStore:
    def __init__(self, paths: AgentPaths | None = None):
        self.paths = (paths or AgentPaths()).ensure()

    # -- config ------------------------------------------------------------
    def load_config(self) -> dict:
        config = dict(DEFAULT_CONFIG)
        if self.paths.config.is_file():
            try:
                config.update(json.loads(unprotect(self.paths.config.read_bytes()).decode("utf-8")))
            except Exception as exc:  # corrupted or foreign state file
                raise RuntimeError(
                    f"The reader agent configuration at {self.paths.config} could not be read ({exc}). "
                    "Re-run the one-click agent setup from the J-ONE dashboard."
                ) from exc
        return config

    def save_config(self, config: dict) -> None:
        payload = json.dumps(config, indent=2, sort_keys=True).encode("utf-8")
        self._atomic_write(self.paths.config, protect(payload))

    def is_provisioned(self) -> bool:
        try:
            config = self.load_config()
        except RuntimeError:
            return False
        return bool(config.get("reference") and config.get("signing_key") and config.get("api_base"))

    # -- gallery -----------------------------------------------------------
    def load_gallery(self) -> dict:
        if not self.paths.gallery.is_file():
            return {"etag": "", "templates": []}
        try:
            return json.loads(unprotect(self.paths.gallery.read_bytes()).decode("utf-8"))
        except Exception:
            return {"etag": "", "templates": []}

    def save_gallery(self, gallery: dict) -> None:
        payload = json.dumps(gallery, separators=(",", ":")).encode("utf-8")
        self._atomic_write(self.paths.gallery, protect(payload))

    # -- spool -------------------------------------------------------------
    def push_request(self, request: dict) -> Path:
        # Nanosecond stamp + pid + in-process sequence + random suffix. Two
        # clicks arriving in the same clock tick must queue two requests in
        # arrival order, never overwrite one another -- and the names still
        # sort in arrival order even when the filesystem's mtime resolution is
        # coarser than a nanosecond.
        seq = next(_SPOOL_SEQ)
        name = (
            f"{time.time_ns():020d}-{os.getpid():06d}-{seq:08d}-"
            f"{secrets.token_hex(4)}.json"
        )
        path = self.paths.requests / name
        self._atomic_write(path, json.dumps(request).encode("utf-8"))
        return path

    def pending_requests(self) -> list[Path]:
        """Queued requests, oldest first (mtime, then name for stability)."""
        try:
            paths = list(self.paths.requests.glob("*.json"))
        except OSError:
            return []
        def order(path: Path):
            try:
                return (path.stat().st_mtime_ns, path.name)
            except OSError:  # pragma: no cover - vanished between list and stat
                return (0, path.name)
        return sorted(paths, key=order)

    def claim_request(self, path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text("utf-8"))
        except Exception:
            data = None
        try:
            path.unlink()
        except OSError:
            pass
        return data

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        except Exception:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise


# --------------------------------------------------------------------------
# Logging + single instance
# --------------------------------------------------------------------------
def configure_logging(paths: AgentPaths, *, verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("jone.reader.agent")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    if logger.handlers:
        return logger
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    try:
        handler = RotatingFileHandler(paths.log_file, maxBytes=512 * 1024, backupCount=3, encoding="utf-8")
    except OSError:  # pragma: no cover - unwritable log directory
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    if verbose or not IS_WINDOWS:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        logger.addHandler(stream)
    logger.propagate = False
    return logger


class SingleInstance:
    """Windows named mutex with lock-file fallback; flock elsewhere."""

    def __init__(self, name: str):
        self.name = name
        self._handle = None
        self._file = None

    def _lock_path(self) -> Path:
        safe = hashlib.sha256(str(self.name).encode("utf-8")).hexdigest()[:12]
        return Path(tempfile.gettempdir()) / f"jone-reader-agent-{safe}.lock"

    def acquire(self) -> bool:
        if IS_WINDOWS:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            handle = kernel32.CreateMutexW(None, False, self.name)
            if handle:
                self._handle = handle
                ERROR_ALREADY_EXISTS = 183
                return ctypes.get_last_error() != ERROR_ALREADY_EXISTS
            # Fallback if mutex creation returned NULL (e.g. policy restriction)
            try:
                import msvcrt

                self._file = open(self._lock_path(), "w")
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        lock_path = self._lock_path()
        try:
            self._file = open(lock_path, "w")
            import fcntl

            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def release(self) -> None:
        try:
            if self._handle:
                ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(self._handle))
                self._handle = None
        except Exception:  # pragma: no cover
            pass
        try:
            if self._file:
                self._file.close()
                self._file = None
        except Exception:  # pragma: no cover
            pass
