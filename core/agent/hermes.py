"""Fail-closed adapter for the optional, pinned Hermes TUI gateway.

Jarvs owns routing, policy and durable audit.  This module is deliberately a
small process/protocol boundary: it starts a fresh Hermes gateway only after
Jarvs has approved a specialist invocation, forwards normalized events, and
removes the per-run Hermes home when the invocation finishes.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import math
import os
import queue
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import unicodedata
import uuid
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO
from urllib.parse import urlparse

if os.name == "nt":  # pragma: no cover - declarations are platform-specific
    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


HERMES_VERSION = "0.20.0"
HERMES_TAG = "v2026.8.3"
HERMES_REVISION = "3c27eb6234bf91b8ceee9e9071591b31e9b148cb"
HERMES_DESKTOP_CONTRACT = 5

_MAX_PROTOCOL_LINE_BYTES = 12 * 1024 * 1024
_MAX_REQUEST_BYTES = 2 * 1024 * 1024
# ShortTermTranscript accepts 128 KiB total. Reserve enough space for the
# enclosing Jarvs message and neighbouring turns so the streamed response is
# not accepted by the UI and subsequently discarded from history.
_MAX_RESPONSE_BYTES = 92 * 1024
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_TOTAL_IMAGE_BYTES = 25 * 1024 * 1024
_MAX_IMAGES = 8
_MAX_MESSAGES = 24
_MAX_STDERR_BYTES = 256 * 1024
_MAX_QUEUED_MESSAGES = 512
_MAX_DEFERRED_MESSAGES = 512
_MAX_QUEUED_BYTES = 16 * 1024 * 1024
_MAX_DEFERRED_BYTES = 8 * 1024 * 1024
_MAX_EVENTS = 10_000
_MAX_FRAMES = 12_000
_MAX_NORMALIZED_EVENT_BYTES = 16 * 1024
_MAX_FINAL_RESULT_BYTES = 96 * 1024
_MAX_DISPLAY_COMMAND_CHARS = 4_000
_MAX_ATTESTATION_BYTES = 8 * 1024 * 1024
_MAX_ATTESTED_FILES = 50_000
_ATTESTATION_NAME = ".jarvs-hermes-attestation.json"
_RUN_PREFIX = "jarvs-run-"
_RUN_MARKER = ".jarvs-hermes-run"
_FIXTURE_MARKER = ".jarvs-hermes-fixture"

_CAPABILITY_TOOLSETS = {
    "research": "web",
    "vision": "vision",
    "coding_fixture": "file,terminal",
    "browser_fixture": "browser",
    "delegation_fixture": "delegation",
}
_EFFECTFUL_CAPABILITIES = frozenset(
    {"coding_fixture", "browser_fixture", "delegation_fixture"}
)
_PASSTHROUGH_ENV = (
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "PATH",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)


class HermesAdapterError(RuntimeError):
    """Base class whose messages are stable public reason codes."""


class HermesUnavailableError(HermesAdapterError):
    pass


class HermesProtocolError(HermesAdapterError):
    pass


class HermesInteractionUnsupported(HermesAdapterError):
    pass


class _HermesCancelled(Exception):
    pass


@dataclass(frozen=True)
class HermesConfig:
    runtime_root: Path
    python_path: Path | None
    home_root: Path
    startup_timeout: float = 30.0
    rpc_timeout: float = 30.0
    turn_timeout: float = 15 * 60.0
    shutdown_timeout: float = 3.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "runtime_root", Path(self.runtime_root))
        if self.python_path is not None:
            object.__setattr__(self, "python_path", Path(self.python_path))
        object.__setattr__(self, "home_root", Path(self.home_root))
        for name in (
            "startup_timeout",
            "rpc_timeout",
            "turn_timeout",
            "shutdown_timeout",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True)
class HermesRequest:
    messages: Sequence[Mapping[str, Any]]
    workspace_root: str | os.PathLike[str]
    capability: str
    model: str
    provider: str
    base_url: str
    api_key: str
    images: Sequence[Mapping[str, Any]] = ()


@dataclass(frozen=True)
class HermesStatus:
    state: str
    ready: bool
    pid: int | None
    version: str | None
    error: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "ready": self.ready,
            "pid": self.pid,
            "version": self.version,
            "error": self.error,
        }


@dataclass(frozen=True)
class _Layout:
    runtime_root: Path
    python_path: Path


@dataclass(frozen=True)
class _ReaderFault:
    reason: str


@dataclass(frozen=True)
class _QueuedMessage:
    value: Mapping[str, Any]
    size: int


@dataclass(frozen=True)
class _CombinedCancellation:
    first: Any
    second: Any

    def is_set(self) -> bool:
        return _is_cancelled(self.first) or _is_cancelled(self.second)


class HermesGatewayAdapter:
    """One-run-at-a-time client for Hermes' newline-delimited JSON-RPC API."""

    def __init__(self, config: HermesConfig) -> None:
        self.config = config
        self._state_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._messages: queue.Queue[object] = queue.Queue(
            maxsize=_MAX_QUEUED_MESSAGES
        )
        self._queued_bytes = 0
        self._deferred: deque[_QueuedMessage] = deque()
        self._deferred_bytes = 0
        self._reader_fault: str | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_tail = bytearray()
        self._request_number = 0
        self._state = "stopped"
        self._ready = False
        self._version: str | None = None
        self._error: str | None = None
        self._run_home: Path | None = None
        self._run_token: str | None = None
        self._closed = False
        self._lifecycle_cancel = threading.Event()
        self._job_handle: Any = None
        self._git_path = _resolve_trusted_git()

    def status(self) -> HermesStatus:
        with self._state_lock:
            process = self._process
            pid = process.pid if process is not None and process.poll() is None else None
            return HermesStatus(
                state=self._state,
                ready=self._ready and pid is not None,
                pid=pid,
                version=self._version,
                error=self._error,
            )

    def preflight(self, request: HermesRequest, cancelled=None) -> str | None:
        """Validate the complete boundary without starting Hermes."""

        cancelled = _CombinedCancellation(cancelled, self._lifecycle_cancel)
        with self._state_lock:
            closed = self._closed
        request_reason = (
            "hermes_adapter_closed" if closed else self._validate_request(request)
        )
        reason = request_reason
        if reason is None:
            try:
                reason = self._verify_runtime(cancelled=cancelled)
            except _HermesCancelled:
                return "hermes_cancelled"
        with self._state_lock:
            if reason is None:
                self._version = HERMES_VERSION
                if self._process is None and not self._closed:
                    self._state = "stopped"
                self._error = None
            elif request_reason is None:
                self._error = reason
        return reason

    def execute(
        self,
        request: HermesRequest,
        cancelled,
        *,
        event_notifier: Callable[[Mapping[str, Any]], Any] | None = None,
        interactive_handler: Callable[[Mapping[str, Any]], bool] | None = None,
    ) -> Mapping[str, Any]:
        """Run one isolated Hermes session and return its final response."""

        if not self._run_lock.acquire(blocking=False):
            raise HermesUnavailableError("hermes_adapter_busy")
        cancelled = _CombinedCancellation(cancelled, self._lifecycle_cancel)
        session_id: str | None = None
        stored_session_id: str | None = None
        run_home: Path | None = None
        successful = False
        cancelled_run = False
        cleanup_error: str | None = None
        try:
            reason = self.preflight(request, cancelled=cancelled)
            if reason is not None:
                if reason == "hermes_adapter_closed":
                    raise HermesUnavailableError(reason)
                if reason == "hermes_cancelled" or _is_cancelled(cancelled):
                    raise _HermesCancelled()
                raise HermesUnavailableError(reason)
            if _is_cancelled(cancelled):
                return self._cancelled_result(request, session_id=None)

            run_home = self._create_run_home()
            self._start(request, run_home, cancelled)
            created = self._rpc(
                "session.create",
                {
                    "cwd": str(Path(request.workspace_root).resolve()),
                    "source": "jarvs",
                    "model": _gateway_model(request.model),
                    "provider": "lmstudio",
                    "messages": [],
                    "close_on_disconnect": True,
                },
                cancelled=cancelled,
            )
            session_id, stored_session_id = self._validate_created_session(created)

            for index, image in enumerate(request.images):
                content, extension = _validated_image(image)
                self._rpc(
                    "image.attach_bytes",
                    {
                        "session_id": session_id,
                        "content_base64": content,
                        "filename": f"jarvs-image-{index + 1}.{extension}",
                    },
                    cancelled=cancelled,
                )

            prompt = _render_prompt(request.messages)
            self._rpc(
                "prompt.submit",
                {"session_id": session_id, "text": prompt},
                cancelled=cancelled,
            )
            result = self._consume_turn(
                request,
                session_id,
                cancelled,
                event_notifier=event_notifier,
                interactive_handler=interactive_handler,
            )
            successful = True
            return result
        except _HermesCancelled:
            cancelled_run = True
            if session_id is not None:
                try:
                    self._rpc(
                        "session.interrupt",
                        {"session_id": session_id},
                        timeout=min(self.config.rpc_timeout, 5.0),
                    )
                except HermesAdapterError:
                    pass
            return self._cancelled_result(request, session_id=session_id)
        except HermesAdapterError as exc:
            with self._state_lock:
                self._state = "closed" if self._closed else "error"
                self._error = str(exc)
            raise
        except Exception as exc:
            with self._state_lock:
                self._state = "error"
                self._error = "hermes_adapter_failure"
            raise HermesAdapterError("hermes_adapter_failure") from exc
        finally:
            if session_id is not None:
                try:
                    self._rpc(
                        "session.close",
                        {"session_id": session_id},
                        timeout=min(self.config.rpc_timeout, 5.0),
                    )
                except Exception:
                    pass
            if stored_session_id is not None:
                try:
                    self._rpc(
                        "session.delete",
                        {"session_id": stored_session_id},
                        timeout=min(self.config.rpc_timeout, 5.0),
                    )
                except Exception:
                    pass
            self._stop_process()
            if run_home is not None:
                try:
                    self._remove_run_home(run_home)
                except Exception:
                    cleanup_error = "hermes_ephemeral_cleanup_failed"
            with self._state_lock:
                if cleanup_error is not None:
                    self._state = "error"
                    self._error = cleanup_error
                elif (successful or cancelled_run) and not self._closed:
                    self._state = "stopped"
                    self._error = None
                self._run_home = None
                self._run_token = None
            self._run_lock.release()
            # A successful turn is not successful if its supposedly ephemeral
            # transcript could not be removed. Do not mask an earlier error;
            # that failure and status() still retain the cleanup condition.
            if cleanup_error is not None and successful:
                raise HermesAdapterError(cleanup_error)

    def close(self) -> None:
        """Terminate the whole owned process tree and remove only our run home."""

        with self._state_lock:
            self._closed = True
            self._lifecycle_cancel.set()
            run_home = self._run_home
        self._stop_process()
        owns_cleanup = self._run_lock.acquire(blocking=False)
        if owns_cleanup and run_home is not None:
            try:
                self._remove_run_home(run_home)
            except Exception:
                with self._state_lock:
                    self._state = "error"
                    self._error = "hermes_ephemeral_cleanup_failed"
                self._run_lock.release()
                return
        with self._state_lock:
            self._state = "closed"
            self._ready = False
            self._error = None
            if owns_cleanup:
                self._run_home = None
                self._run_token = None
        if owns_cleanup:
            self._run_lock.release()

    def _validate_request(self, request: HermesRequest) -> str | None:
        capability = str(request.capability or "")
        if capability not in _CAPABILITY_TOOLSETS:
            return "hermes_capability_not_allowed"
        if request.provider != "lmstudio":
            return "hermes_local_provider_required"
        if request.api_key not in {"", "lm-studio"}:
            return "hermes_local_placeholder_key_required"
        if not _is_loopback_url(str(request.base_url or "")):
            return "hermes_local_provider_required"
        if not isinstance(request.model, str) or not request.model.strip():
            return "hermes_model_required"
        if len(request.model) > 500 or any(ord(char) < 32 for char in request.model):
            return "hermes_model_invalid"

        workspace = Path(request.workspace_root)
        if not workspace.is_absolute() or not workspace.is_dir():
            return "hermes_workspace_invalid"
        try:
            workspace = workspace.resolve(strict=True)
        except OSError:
            return "hermes_workspace_invalid"
        if capability in _EFFECTFUL_CAPABILITIES:
            if not _is_regular_unsymlinked_file(workspace / _FIXTURE_MARKER):
                return "hermes_fixture_marker_missing"

        home = self.config.home_root
        if not home.is_absolute():
            return "hermes_home_invalid"
        try:
            home = home.resolve(strict=False)
        except OSError:
            return "hermes_home_invalid"
        existing_parent = home if home.exists() else home.parent
        if not existing_parent.is_dir():
            return "hermes_home_invalid"
        if home.exists() and not home.is_dir():
            return "hermes_home_invalid"

        if not isinstance(request.messages, Sequence) or isinstance(
            request.messages, (str, bytes)
        ):
            return "hermes_messages_invalid"
        if not isinstance(request.images, Sequence) or isinstance(
            request.images, (str, bytes)
        ):
            return "hermes_image_invalid"
        if len(request.messages) > _MAX_MESSAGES:
            return "hermes_too_many_messages"
        if len(request.images) > _MAX_IMAGES:
            return "hermes_too_many_images"
        if capability == "vision" and not request.images:
            return "hermes_vision_image_required"
        total_image_bytes = 0
        for image in request.images:
            try:
                content, _extension = _validated_image(image)
                total_image_bytes += len(base64.b64decode(content, validate=True))
            except (TypeError, ValueError):
                return "hermes_image_invalid"
        if total_image_bytes > _MAX_TOTAL_IMAGE_BYTES:
            return "hermes_images_too_large"

        try:
            _render_prompt(request.messages)
        except (TypeError, ValueError):
            return "hermes_messages_invalid"
        return None

    def _verify_runtime(self, cancelled=None) -> str | None:
        if not self.config.runtime_root.is_absolute():
            return "hermes_runtime_invalid"
        if (
            self.config.python_path is not None
            and not self.config.python_path.is_absolute()
        ):
            return "hermes_python_missing"
        try:
            layout = self._resolve_layout()
        except (OSError, ValueError):
            return "hermes_runtime_invalid"
        root = layout.runtime_root
        if not root.is_absolute() or not root.is_dir():
            return "hermes_runtime_invalid"
        if not layout.python_path.is_absolute() or not layout.python_path.is_file():
            return "hermes_python_missing"
        if not (root / "tui_gateway" / "entry.py").is_file():
            return "hermes_gateway_missing"
        # The gateway itself asks its checkout for a fallback project .env.
        # A configured runtime must therefore remain code-only; all sidecar
        # configuration belongs in Jarvs' fresh per-run HERMES_HOME.
        if (root / ".env").exists() or (root / ".op.env").exists():
            return "hermes_runtime_env_not_allowed"
        top_level = self._git_capture(
            root,
            ["rev-parse", "--show-toplevel"],
            timeout=5,
            cancelled=cancelled,
        )
        if top_level is None:
            return "hermes_runtime_invalid"
        try:
            if Path(top_level.strip()).resolve(strict=True) != root.resolve(strict=True):
                return "hermes_runtime_root_mismatch"
        except OSError:
            return "hermes_runtime_invalid"
        version = _read_pyproject_version(root / "pyproject.toml")
        if version != HERMES_VERSION:
            return "hermes_runtime_version_mismatch"
        revision = self._read_revision(root, cancelled=cancelled)
        if revision != HERMES_REVISION:
            return "hermes_runtime_revision_mismatch"
        if not self._runtime_code_is_clean(root, cancelled=cancelled):
            return "hermes_runtime_modified"
        attestation_error = self._verify_venv_attestation(
            layout, cancelled=cancelled
        )
        if attestation_error is not None:
            return attestation_error
        return None

    def _read_revision(self, root: Path, *, cancelled=None) -> str | None:
        output = self._git_capture(
            root,
            ["rev-parse", "HEAD^{commit}"],
            timeout=5,
            cancelled=cancelled,
        )
        if output is None:
            return None
        revision = output.strip().lower()
        return revision if re.fullmatch(r"[0-9a-f]{40}", revision) else None

    def _runtime_code_is_clean(self, root: Path, *, cancelled=None) -> bool:
        if self._git_path is None:
            return False
        with tempfile.TemporaryFile() as output:
            try:
                process = subprocess.Popen(
                    self._git_command(
                        root,
                        [
                            "status",
                            "--porcelain",
                            "--untracked-files=all",
                            "--",
                            ".",
                            f":(exclude){_ATTESTATION_NAME}",
                        ],
                    ),
                    cwd=str(root),
                    env=self._git_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                return False
            returncode = self._wait_subprocess(
                process, timeout=10, cancelled=cancelled
            )
            output.seek(0, os.SEEK_END)
            return returncode == 0 and output.tell() == 0

    def _verify_venv_attestation(
        self, layout: _Layout, *, cancelled=None
    ) -> str | None:
        manifest_path = layout.runtime_root / _ATTESTATION_NAME
        try:
            if (
                not manifest_path.is_file()
                or manifest_path.is_symlink()
                or manifest_path.stat().st_size > _MAX_ATTESTATION_BYTES
            ):
                return "hermes_attestation_missing"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return "hermes_attestation_invalid"
        if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 1:
            return "hermes_attestation_invalid"
        if set(manifest) != {
            "schema_version",
            "hermes_version",
            "hermes_tag",
            "hermes_revision",
            "python_executable",
            "python_version",
            "dependency_lock_sha256",
            "files",
        }:
            return "hermes_attestation_invalid"
        if (
            manifest.get("hermes_version") != HERMES_VERSION
            or manifest.get("hermes_tag") != HERMES_TAG
            or manifest.get("hermes_revision") != HERMES_REVISION
        ):
            return "hermes_attestation_mismatch"
        relative_python = manifest.get("python_executable")
        python_version = manifest.get("python_version")
        lock_hash = manifest.get("dependency_lock_sha256")
        files = manifest.get("files")
        if (
            not isinstance(relative_python, str)
            or not relative_python
            or not isinstance(python_version, str)
            or not re.fullmatch(r"3\.(11|12|13)(?:\.\d+)?", python_version)
            or not isinstance(lock_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", lock_hash)
            or not isinstance(files, Mapping)
            or not 1 <= len(files) <= _MAX_ATTESTED_FILES
        ):
            return "hermes_attestation_invalid"
        try:
            portable_python = PurePosixPath(relative_python)
            if (
                "\\" in relative_python
                or portable_python.is_absolute()
                or not portable_python.parts
                or any(part in {"", ".", ".."} for part in portable_python.parts)
            ):
                return "hermes_attestation_invalid"
            declared_python = (
                layout.runtime_root.joinpath(*portable_python.parts)
                .resolve(strict=True)
            )
            if declared_python != layout.python_path.resolve(strict=True):
                return "hermes_attestation_mismatch"
            declared_python.relative_to(layout.runtime_root.resolve(strict=True))
            venv_root = declared_python.parent.parent
            venv_root.relative_to(layout.runtime_root.resolve(strict=True))
        except (OSError, ValueError):
            return "hermes_attestation_mismatch"
        if venv_root == layout.runtime_root.resolve(strict=True):
            return "hermes_attestation_mismatch"
        try:
            lock_path = layout.runtime_root / "uv.lock"
            if lock_path.is_symlink():
                return "hermes_attestation_invalid"
            if _sha256_file(lock_path, cancelled) != lock_hash:
                return "hermes_dependency_lock_mismatch"
        except (OSError, _HermesCancelled):
            if _is_cancelled(cancelled):
                raise
            return "hermes_attestation_invalid"

        normalized: dict[str, str] = {}
        for raw_path, digest in files.items():
            if (
                not isinstance(raw_path, str)
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                return "hermes_attestation_invalid"
            relative = PurePosixPath(raw_path)
            if (
                "\\" in raw_path
                or relative.is_absolute()
                or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                return "hermes_attestation_invalid"
            key = relative.as_posix()
            if key in normalized or _attestation_forbidden(relative):
                return "hermes_attestation_invalid"
            normalized[key] = digest

        actual: dict[str, Path] = {}
        try:
            for path in venv_root.rglob("*"):
                if _is_cancelled(cancelled):
                    raise _HermesCancelled()
                relative = path.relative_to(venv_root)
                if _attestation_forbidden(relative):
                    return "hermes_venv_bytecode_forbidden"
                is_junction = getattr(path, "is_junction", lambda: False)()
                mode = path.lstat().st_mode
                if path.is_symlink() or is_junction or not (
                    stat.S_ISREG(mode) or stat.S_ISDIR(mode)
                ):
                    return "hermes_venv_unsupported_entry"
                if stat.S_ISREG(mode):
                    key = relative.as_posix()
                    actual[key] = path
                    if len(actual) > _MAX_ATTESTED_FILES:
                        return "hermes_attestation_invalid"
        except OSError:
            return "hermes_attestation_invalid"
        if actual.keys() != normalized.keys():
            return "hermes_venv_drift"
        try:
            for key, path in actual.items():
                if _sha256_file(path, cancelled) != normalized[key]:
                    return "hermes_venv_drift"
        except OSError:
            return "hermes_attestation_invalid"
        return None

    def _git_capture(
        self,
        root: Path,
        arguments: Sequence[str],
        *,
        timeout: float,
        cancelled=None,
    ) -> str | None:
        if self._git_path is None:
            return None
        with tempfile.TemporaryFile() as output:
            try:
                process = subprocess.Popen(
                    self._git_command(root, arguments),
                    cwd=str(root),
                    env=self._git_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                return None
            if self._wait_subprocess(
                process, timeout=timeout, cancelled=cancelled
            ) != 0:
                return None
            size = output.seek(0, os.SEEK_END)
            if size > 8_192:
                return None
            output.seek(0)
            try:
                return output.read().decode("utf-8")
            except UnicodeDecodeError:
                return None

    def _git_command(self, root: Path, arguments: Sequence[str]) -> list[str]:
        if self._git_path is None:
            raise HermesUnavailableError("hermes_git_missing")
        return [
            str(self._git_path),
            "--no-pager",
            "-c",
            f"safe.directory={root}",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "credential.helper=",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "submodule.recurse=false",
            "-c",
            "protocol.file.allow=never",
            "-c",
            "protocol.ext.allow=never",
            "-C",
            str(root),
            *arguments,
        ]

    @staticmethod
    def _git_environment() -> dict[str, str]:
        env = {
            key: value
            for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR")
            if (value := os.environ.get(key)) is not None
        }
        env.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_ASKPASS": "",
                "LC_ALL": "C",
            }
        )
        return env

    @staticmethod
    def _wait_subprocess(process, *, timeout: float, cancelled=None) -> int:
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if _is_cancelled(cancelled):
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                raise _HermesCancelled()
            if time.monotonic() >= deadline:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                return -1
            time.sleep(0.025)
        return int(process.returncode or 0)

    def _create_runtime_snapshot(
        self,
        root: Path,
        run_home: Path,
        cancelled=None,
        *,
        capability: str | None = None,
    ) -> Path:
        if self._git_path is None:
            raise HermesUnavailableError("hermes_git_missing")
        archive = run_home / ".runtime.tar"
        snapshot = run_home / "runtime"
        try:
            self._git_archive(root, archive, cancelled=cancelled)
            snapshot.mkdir(mode=0o700)
            self._extract_snapshot(archive, snapshot, cancelled=cancelled)
            if capability in {"research", "browser_fixture"}:
                self._restrict_bundled_plugins(snapshot, capability)
            self._make_snapshot_read_only(snapshot)
        except OSError as exc:
            raise HermesUnavailableError("hermes_snapshot_failed") from exc
        finally:
            try:
                archive.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        if not (snapshot / "tui_gateway" / "entry.py").is_file():
            raise HermesUnavailableError("hermes_snapshot_invalid")
        if _read_pyproject_version(snapshot / "pyproject.toml") != HERMES_VERSION:
            raise HermesUnavailableError("hermes_snapshot_invalid")
        return snapshot.resolve(strict=True)

    @staticmethod
    def _restrict_bundled_plugins(snapshot: Path, capability: str) -> None:
        """Leave only the pinned bundled backend required by this run."""

        plugins = snapshot / "plugins"
        allowed = "web" if capability == "research" else "browser"
        if not (plugins / allowed).is_dir():
            raise HermesUnavailableError("hermes_snapshot_invalid")
        try:
            for child in plugins.iterdir():
                if child.is_dir() and child.name != allowed:
                    shutil.rmtree(child)
        except OSError as exc:
            raise HermesUnavailableError("hermes_snapshot_failed") from exc

    @staticmethod
    def _make_snapshot_read_only(destination: Path) -> None:
        for path in sorted(destination.rglob("*"), reverse=True):
            try:
                path.chmod(0o555 if path.is_dir() else 0o444)
            except OSError as exc:
                raise HermesUnavailableError(
                    "hermes_snapshot_readonly_failed"
                ) from exc
        try:
            destination.chmod(0o555)
        except OSError as exc:
            raise HermesUnavailableError("hermes_snapshot_readonly_failed") from exc

    def _git_archive(self, root: Path, archive: Path, *, cancelled=None) -> None:
        try:
            process = subprocess.Popen(
                self._git_command(
                    root,
                    [
                        "archive",
                        "--format=tar",
                        f"--output={archive}",
                        HERMES_REVISION,
                    ],
                ),
                cwd=str(root),
                env=self._git_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise HermesUnavailableError("hermes_snapshot_failed") from exc
        if self._wait_subprocess(process, timeout=60, cancelled=cancelled) != 0:
            raise HermesUnavailableError("hermes_snapshot_failed")

    def _extract_snapshot(self, archive: Path, destination: Path, *, cancelled=None):
        total = 0
        count = 0
        with tarfile.open(archive, mode="r:") as bundle:
            members = bundle.getmembers()
            for member in members:
                if _is_cancelled(cancelled):
                    raise _HermesCancelled()
                count += 1
                total += max(0, member.size)
                relative = Path(member.name)
                if (
                    count > 50_000
                    or total > 512 * 1024 * 1024
                    or relative.is_absolute()
                    or ".." in relative.parts
                    or not (member.isdir() or member.isfile())
                ):
                    raise HermesUnavailableError("hermes_snapshot_invalid")
            for member in members:
                if _is_cancelled(cancelled):
                    raise _HermesCancelled()
                target = destination.joinpath(*Path(member.name).parts)
                try:
                    target.resolve(strict=False).relative_to(
                        destination.resolve(strict=True)
                    )
                except (OSError, ValueError) as exc:
                    raise HermesUnavailableError("hermes_snapshot_invalid") from exc
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    raise HermesUnavailableError("hermes_snapshot_invalid")
                with source, target.open("xb") as output:
                    remaining = member.size
                    while remaining:
                        if _is_cancelled(cancelled):
                            raise _HermesCancelled()
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise HermesUnavailableError("hermes_snapshot_invalid")
                        output.write(chunk)
                        remaining -= len(chunk)
    def _resolve_layout(self) -> _Layout:
        configured_root = self.config.runtime_root.expanduser().resolve(strict=False)
        configured_python = self.config.python_path
        if configured_python is not None:
            python_path = configured_python.expanduser().resolve(strict=False)
            runtime_root = configured_root
            if not _looks_like_runtime(runtime_root):
                for ancestor in python_path.parents:
                    if _looks_like_runtime(ancestor):
                        runtime_root = ancestor
                        break
            return _Layout(runtime_root=runtime_root, python_path=python_path)

        for candidate in (
            configured_root / "venv" / "Scripts" / "python.exe",
            configured_root / ".venv" / "Scripts" / "python.exe",
            configured_root / "venv" / "bin" / "python",
            configured_root / ".venv" / "bin" / "python",
            configured_root / "Scripts" / "python.exe",
            configured_root / "bin" / "python",
            configured_root / "python.exe",
        ):
            if candidate.is_file():
                return _Layout(runtime_root=configured_root, python_path=candidate.resolve())
        raise ValueError("no pinned interpreter")

    def _create_run_home(self) -> Path:
        home_root = self.config.home_root.expanduser().resolve(strict=False)
        home_root.mkdir(parents=True, exist_ok=True)
        runs_root = home_root / "runs"
        runs_root.mkdir(exist_ok=True)
        resolved_runs = runs_root.resolve(strict=True)
        resolved_runs.relative_to(home_root.resolve(strict=True))
        token = uuid.uuid4().hex
        run_home = resolved_runs / f"{_RUN_PREFIX}{token}"
        run_home.mkdir(mode=0o700)
        resolved = run_home.resolve(strict=True)
        if resolved.parent != resolved_runs:
            raise HermesUnavailableError("hermes_home_invalid")
        (resolved / _RUN_MARKER).write_text(token, encoding="ascii")
        (resolved / "config.yaml").write_text(
            "approvals:\n"
            "  mode: manual\n"
            "memory:\n"
            "  memory_enabled: false\n"
            "  user_profile_enabled: false\n"
            "  write_approval: true\n"
            "skills:\n"
            "  write_approval: true\n"
            "plugins:\n"
            "  enabled: []\n"
            "  disabled: []\n"
            "mcp_servers: {}\n"
            "hooks:\n"
            "  outbound: []\n"
            "hooks_auto_accept: false\n"
            "platforms: {}\n"
            "gateway:\n"
            "  platforms: {}\n"
            "security:\n"
            "  allow_lazy_installs: false\n",
            encoding="utf-8",
        )
        with self._state_lock:
            self._run_home = resolved
            self._run_token = token
        return resolved

    def _remove_run_home(self, run_home: Path) -> None:
        if not run_home.exists():
            return
        home_root = self.config.home_root.expanduser().resolve(strict=True)
        runs_root = (home_root / "runs").resolve(strict=True)
        resolved = run_home.resolve(strict=True)
        if resolved.parent != runs_root or not resolved.name.startswith(_RUN_PREFIX):
            raise HermesAdapterError("hermes_ephemeral_cleanup_refused")
        marker = resolved / _RUN_MARKER
        token = marker.read_text(encoding="ascii").strip()
        expected = resolved.name.removeprefix(_RUN_PREFIX)
        if not token or token != expected or token != self._run_token:
            raise HermesAdapterError("hermes_ephemeral_cleanup_refused")
        last_error: OSError | None = None
        for _attempt in range(3):
            try:
                shutil.rmtree(resolved, onerror=_make_removable_and_retry)
                return
            except FileNotFoundError:
                return
            except OSError as exc:
                last_error = exc
                time.sleep(0.05)
        raise HermesAdapterError("hermes_ephemeral_cleanup_failed") from last_error

    def _start(self, request: HermesRequest, run_home: Path, cancelled=None) -> None:
        with self._state_lock:
            if self._closed:
                raise HermesUnavailableError("hermes_adapter_closed")
        layout = self._resolve_layout()
        # Do expensive verification without holding the lifecycle lock. close()
        # can therefore permanently retire this adapter while hashing/archive
        # work is in flight; the final locked gate below observes that state.
        reason = self._verify_runtime(cancelled=cancelled)
        if reason is not None:
            raise HermesUnavailableError(reason)
        snapshot_root = self._create_runtime_snapshot(
            layout.runtime_root,
            run_home,
            cancelled=cancelled,
            capability=request.capability,
        )
        attestation_error = self._verify_venv_attestation(
            layout, cancelled=cancelled
        )
        if attestation_error is not None:
            raise HermesUnavailableError(attestation_error)
        snapshot_layout = _Layout(
            runtime_root=snapshot_root,
            python_path=layout.python_path,
        )
        env = self._build_environment(request, snapshot_layout, run_home)
        popen_kwargs: dict[str, Any] = {
            "cwd": str(snapshot_root),
            "env": env,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "bufsize": 0,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            popen_kwargs["start_new_session"] = True

        with self._state_lock:
            if self._closed:
                raise HermesUnavailableError("hermes_adapter_closed")
            # Revalidate the authorization-sensitive workspace and marker at
            # the final launch boundary. Cancellation is checked immediately
            # before Popen so reset/reconfigure cannot create a stale sidecar.
            request_error = self._validate_request(request)
            if request_error is not None:
                raise HermesUnavailableError(request_error)
            self._state = "starting"
            self._ready = False
            self._error = None
            messages: queue.Queue[object] = queue.Queue(
                maxsize=_MAX_QUEUED_MESSAGES
            )
            self._messages = messages
            self._queued_bytes = 0
            self._deferred.clear()
            self._deferred_bytes = 0
            self._reader_fault = None
            self._stderr_tail.clear()
            if _is_cancelled(cancelled):
                raise _HermesCancelled()
            try:
                process = subprocess.Popen(
                    [
                        str(layout.python_path),
                        "-s",
                        "-u",
                        "-m",
                        "tui_gateway.entry",
                    ],
                    **popen_kwargs,
                )
            except OSError as exc:
                raise HermesUnavailableError("hermes_gateway_start_failed") from exc
            try:
                job_handle = self._assign_windows_kill_job(process)
            except HermesUnavailableError:
                try:
                    process.kill()
                    process.wait(timeout=1)
                except (OSError, subprocess.SubprocessError):
                    pass
                raise
            self._process = process
            self._job_handle = job_handle
        assert process.stdout is not None
        assert process.stderr is not None
        self._reader_thread = threading.Thread(
            target=self._read_stdout,
            args=(process.stdout, messages, process),
            name="jarvs-hermes-stdout",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            args=(process.stderr,),
            name="jarvs-hermes-stderr",
            daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread.start()
        self._wait_gateway_ready(cancelled=cancelled)
        with self._state_lock:
            self._state = "ready"
            self._ready = True

    def _build_environment(
        self, request: HermesRequest, layout: _Layout, run_home: Path
    ) -> dict[str, str]:
        env = {
            key: value
            for key in _PASSTHROUGH_ENV
            if (value := os.environ.get(key)) is not None
        }
        env.update(
            {
                "PYTHONPATH": str(layout.runtime_root),
                "PYTHONUTF8": "1",
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPYCACHEPREFIX": str(run_home / ".pycache"),
                "PYTHONNOUSERSITE": "1",
                "HERMES_HOME": str(run_home),
                "HERMES_MODEL": _gateway_model(request.model),
                "HERMES_TUI_PROVIDER": "lmstudio",
                "HERMES_TUI_TOOLSETS": _CAPABILITY_TOOLSETS[request.capability],
                "HERMES_IGNORE_RULES": "1",
                "HERMES_DISABLE_LAZY_INSTALLS": "1",
                "HERMES_WRITE_SAFE_ROOT": str(Path(request.workspace_root).resolve()),
                "HERMES_SESSION_SOURCE": "jarvs",
                "HERMES_MANAGED_DIR": str(run_home / ".no-managed-scope"),
                "HERMES_TUI_GATEWAY_SHUTDOWN_GRACE_S": "1",
                "LM_BASE_URL": request.base_url,
                # Never forward a user/cloud credential into this sidecar.
                "LM_API_KEY": "lm-studio",
                "NO_PROXY": "127.0.0.1,localhost,::1",
                "no_proxy": "127.0.0.1,localhost,::1",
            }
        )
        if request.capability not in {"research", "browser_fixture"}:
            # Research/browser need their pinned bundled provider backends.
            # Every non-bundled plugin source remains opt-in/disabled in the
            # fresh profile and project-plugin opt-in is absent from this env.
            env["HERMES_SAFE_MODE"] = "1"
        return env

    def _wait_gateway_ready(self, cancelled=None) -> None:
        deadline = time.monotonic() + self.config.startup_timeout
        while True:
            message = self._queue_get(deadline, cancelled=cancelled)
            event = _event_parts(message)
            if event is not None and event[0] == "gateway.ready":
                return
            if _is_response(message):
                raise HermesProtocolError("hermes_gateway_protocol_error")

    def _validate_created_session(self, result: Mapping[str, Any]) -> tuple[str, str]:
        session_id = result.get("session_id")
        stored_session_id = result.get("stored_session_id")
        info = result.get("info")
        contract = info.get("desktop_contract") if isinstance(info, Mapping) else None
        if (
            not isinstance(session_id, str)
            or not session_id
            or len(session_id) > 256
            or not isinstance(stored_session_id, str)
            or not stored_session_id
            or len(stored_session_id) > 256
            or contract != HERMES_DESKTOP_CONTRACT
        ):
            raise HermesProtocolError("hermes_desktop_contract_mismatch")
        return session_id, stored_session_id

    def _consume_turn(
        self,
        request: HermesRequest,
        session_id: str,
        cancelled,
        *,
        event_notifier: Callable[[Mapping[str, Any]], Any] | None,
        interactive_handler: Callable[[Mapping[str, Any]], bool] | None,
    ) -> Mapping[str, Any]:
        deadline = time.monotonic() + self.config.turn_timeout
        # Do not expose deltas until the terminal frame has validated the
        # complete response and transcript budget. This avoids rendering text
        # that Jarvs must later reject and cannot retain in short-term history.
        chunks: list[str] = []
        chunk_bytes = 0
        events = 0
        frames = 0
        while True:
            if time.monotonic() >= deadline:
                raise HermesProtocolError("hermes_turn_timeout")
            if _is_cancelled(cancelled):
                raise _HermesCancelled()

            try:
                message = self._next_message(
                    timeout=min(0.1, max(0.001, deadline - time.monotonic()))
                )
            except queue.Empty:
                if time.monotonic() >= deadline:
                    raise HermesProtocolError("hermes_turn_timeout")
                continue
            frames += 1
            if frames > _MAX_FRAMES:
                raise HermesProtocolError("hermes_gateway_event_overflow")
            event = _event_parts(message)
            if event is None:
                if _is_response(message):
                    raise HermesProtocolError("hermes_gateway_protocol_error")
                continue
            event_type, event_session, raw_payload = event
            if event_session not in {None, "", session_id}:
                continue
            if event_type == "message.delta":
                text = raw_payload.get("text")
                if not isinstance(text, str):
                    raise HermesProtocolError("hermes_gateway_protocol_error")
                events += 1
                if events > _MAX_EVENTS:
                    raise HermesProtocolError("hermes_gateway_event_overflow")
                if text:
                    try:
                        chunk_bytes += len(text.encode("utf-8"))
                    except UnicodeError as exc:
                        raise HermesProtocolError(
                            "hermes_gateway_protocol_error"
                        ) from exc
                    if chunk_bytes > _MAX_RESPONSE_BYTES:
                        raise HermesProtocolError("hermes_response_too_large")
                    chunks.append(text)
                continue
            try:
                normalized = _normalize_event(event_type, raw_payload)
            except (TypeError, ValueError, UnicodeError) as exc:
                raise HermesProtocolError("hermes_gateway_protocol_error") from exc
            if normalized is not None:
                try:
                    normalized_size = len(
                        json.dumps(
                            normalized,
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    )
                except (TypeError, ValueError, UnicodeError) as exc:
                    raise HermesProtocolError(
                        "hermes_gateway_protocol_error"
                    ) from exc
                if normalized_size > _MAX_NORMALIZED_EVENT_BYTES:
                    raise HermesProtocolError("hermes_gateway_event_too_large")
                events += 1
                if events > _MAX_EVENTS:
                    raise HermesProtocolError("hermes_gateway_event_overflow")

            if event_type == "approval.request":
                if normalized is None:
                    raise HermesProtocolError("hermes_gateway_protocol_error")
                _notify(event_notifier, normalized)
                approved = False
                if normalized is not None and interactive_handler is not None:
                    try:
                        approved = bool(interactive_handler(normalized))
                    except Exception:
                        approved = False
                if _is_cancelled(cancelled):
                    raise _HermesCancelled()
                self._rpc(
                    "approval.respond",
                    {
                        "session_id": session_id,
                        "choice": "once" if approved else "deny",
                    },
                    cancelled=cancelled,
                )
                continue
            if event_type.endswith(".request"):
                if normalized is not None:
                    _notify(event_notifier, normalized)
                self._reject_unsupported_interaction(
                    event_type, session_id, raw_payload, cancelled=cancelled
                )
                raise HermesInteractionUnsupported(
                    "hermes_interaction_unsupported"
                )
            if event_type == "message.complete":
                status = raw_payload.get("status")
                if status != "complete":
                    raise HermesProtocolError("hermes_turn_failed")
                final = raw_payload.get("text")
                content = final if isinstance(final, str) else "".join(chunks)
                try:
                    content_size = len(content.encode("utf-8"))
                except UnicodeError as exc:
                    raise HermesProtocolError(
                        "hermes_gateway_protocol_error"
                    ) from exc
                if content_size > _MAX_RESPONSE_BYTES:
                    raise HermesProtocolError("hermes_response_too_large")
                if raw_payload.get("error"):
                    raise HermesProtocolError("hermes_turn_failed")
                result = {
                    "text": content,
                    "session_id": session_id,
                    "finish_reason": status,
                    "event_count": events,
                    "cancelled": False,
                    "provider": "lmstudio",
                    "model": request.model,
                }
                try:
                    result_size = len(
                        json.dumps(
                            result,
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    )
                except (TypeError, ValueError, UnicodeError) as exc:
                    raise HermesProtocolError("hermes_result_invalid") from exc
                if result_size > _MAX_FINAL_RESULT_BYTES:
                    raise HermesProtocolError("hermes_response_too_large")
                if _is_cancelled(cancelled):
                    raise _HermesCancelled()
                if normalized is not None:
                    _notify(event_notifier, normalized)
                return result
            if normalized is not None:
                _notify(event_notifier, normalized)
            if event_type == "error":
                raise HermesProtocolError("hermes_turn_failed")

    def _reject_unsupported_interaction(
        self,
        event_type: str,
        session_id: str,
        payload: Mapping[str, Any],
        *,
        cancelled=None,
    ) -> None:
        request_id = payload.get("request_id")
        params: dict[str, Any] = {"session_id": session_id}
        if isinstance(request_id, str) and request_id:
            params["request_id"] = request_id
        method_and_field = {
            "clarify.request": ("clarify.respond", "answer"),
            "secret.request": ("secret.respond", "value"),
            "sudo.request": ("sudo.respond", "password"),
            "terminal.read.request": ("terminal.read.respond", "text"),
        }
        resolution = method_and_field.get(event_type)
        if resolution is None:
            try:
                self._rpc(
                    "session.interrupt",
                    {"session_id": session_id},
                    timeout=min(self.config.rpc_timeout, 5.0),
                )
            except HermesAdapterError:
                pass
            return
        method, field = resolution
        params[field] = ""
        try:
            self._rpc(
                method,
                params,
                timeout=min(self.config.rpc_timeout, 5.0),
                cancelled=cancelled,
            )
        except HermesAdapterError:
            pass
        try:
            self._rpc(
                "session.interrupt",
                {"session_id": session_id},
                timeout=min(self.config.rpc_timeout, 5.0),
            )
        except HermesAdapterError:
            pass

    def _cancelled_result(
        self, request: HermesRequest, *, session_id: str | None, content: str = ""
    ) -> Mapping[str, Any]:
        return {
            "text": content,
            "session_id": session_id or "",
            "finish_reason": "cancelled",
            "event_count": 0,
            "cancelled": True,
            "provider": "lmstudio",
            "model": request.model,
        }

    def _rpc(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout: float | None = None,
        cancelled=None,
    ) -> Mapping[str, Any]:
        if _is_cancelled(cancelled):
            raise _HermesCancelled()
        with self._state_lock:
            process = self._process
            if process is None or process.poll() is not None or process.stdin is None:
                raise HermesProtocolError("hermes_gateway_exited")
            self._request_number += 1
            request_id = f"jarvs-{self._request_number}"
        envelope = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": dict(params),
        }
        try:
            encoded = json.dumps(
                envelope,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise HermesProtocolError("hermes_gateway_request_invalid") from exc
        request_limit = (
            ((_MAX_IMAGE_BYTES + 2) // 3) * 4 + 16_384
            if method == "image.attach_bytes"
            else _MAX_REQUEST_BYTES
        )
        if len(encoded) > request_limit:
            raise HermesProtocolError("hermes_gateway_request_too_large")
        deadline = time.monotonic() + (timeout or self.config.rpc_timeout)
        self._write_frame(
            process,
            encoded + b"\n",
            deadline=deadline,
            cancelled=cancelled,
        )
        while True:
            message, message_size = self._queue_get(
                deadline, cancelled=cancelled, with_size=True
            )
            if _is_response(message) and message.get("id") == request_id:
                if "error" in message:
                    error = message.get("error")
                    code = error.get("code") if isinstance(error, Mapping) else None
                    suffix = str(code) if isinstance(code, int) else "unknown"
                    raise HermesProtocolError(f"hermes_rpc_{method}_failed_{suffix}")
                result = message.get("result")
                if not isinstance(result, Mapping):
                    raise HermesProtocolError("hermes_gateway_protocol_error")
                return result
            if _is_response(message):
                raise HermesProtocolError("hermes_gateway_protocol_error")
            if len(self._deferred) >= _MAX_DEFERRED_MESSAGES:
                raise HermesProtocolError("hermes_gateway_event_overflow")
            if self._deferred_bytes + message_size > _MAX_DEFERRED_BYTES:
                raise HermesProtocolError("hermes_gateway_event_overflow")
            self._deferred.append(_QueuedMessage(message, message_size))
            self._deferred_bytes += message_size

    def _write_frame(self, process, frame: bytes, *, deadline: float, cancelled=None):
        """Write a potentially large image frame without trapping cancellation."""

        outcome: queue.Queue[BaseException | None] = queue.Queue(maxsize=1)

        def write_all() -> None:
            try:
                assert process.stdin is not None
                with self._write_lock:
                    view = memoryview(frame)
                    offset = 0
                    while offset < len(view):
                        written = process.stdin.write(view[offset:])
                        if not isinstance(written, int) or written <= 0:
                            raise BrokenPipeError("short gateway write")
                        offset += written
                    process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                try:
                    outcome.put_nowait(exc)
                except queue.Full:
                    pass
                return
            try:
                outcome.put_nowait(None)
            except queue.Full:
                pass

        writer = threading.Thread(
            target=write_all,
            name="jarvs-hermes-writer",
            daemon=True,
        )
        writer.start()
        while True:
            if _is_cancelled(cancelled):
                self._abort_owned_process(process)
                raise _HermesCancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._signal_reader_fault(process, "hermes_gateway_write_timeout")
                raise HermesProtocolError("hermes_gateway_write_timeout")
            try:
                result = outcome.get(timeout=min(0.05, remaining))
            except queue.Empty:
                continue
            if result is not None:
                raise HermesProtocolError("hermes_gateway_exited") from result
            return

    def _abort_owned_process(self, process) -> None:
        with self._state_lock:
            if self._process is not process:
                return
            job_handle = self._job_handle
            self._job_handle = None
        if process.poll() is None:
            self._terminate_tree(process, job_handle)
        else:
            self._close_windows_handle(job_handle)

    def _queue_get(self, deadline: float, *, cancelled=None, with_size=False):
        while True:
            if _is_cancelled(cancelled):
                raise _HermesCancelled()
            with self._state_lock:
                fault = self._reader_fault
            if fault is not None:
                raise HermesProtocolError(fault)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HermesProtocolError("hermes_gateway_timeout")
            try:
                item = self._messages.get(timeout=min(0.05, remaining))
            except queue.Empty:
                continue
            if isinstance(item, _ReaderFault):
                raise HermesProtocolError(item.reason)
            if not isinstance(item, _QueuedMessage):
                raise HermesProtocolError("hermes_gateway_protocol_error")
            with self._state_lock:
                self._queued_bytes = max(0, self._queued_bytes - item.size)
            return (item.value, item.size) if with_size else item.value

    def _next_message(self, *, timeout: float) -> Mapping[str, Any]:
        if self._deferred:
            item = self._deferred.popleft()
            self._deferred_bytes = max(0, self._deferred_bytes - item.size)
            return item.value
        deadline = time.monotonic() + timeout
        try:
            return self._queue_get(deadline)
        except HermesProtocolError as exc:
            if str(exc) == "hermes_gateway_timeout":
                raise queue.Empty() from exc
            raise

    def _read_stdout(
        self,
        stream: BinaryIO,
        messages: queue.Queue[object],
        process: subprocess.Popen[bytes],
    ) -> None:
        invalid_lines = 0
        while True:
            try:
                raw = stream.readline(_MAX_PROTOCOL_LINE_BYTES + 1)
            except (OSError, ValueError):
                self._signal_reader_fault(process, "hermes_gateway_read_failed")
                return
            if not raw:
                self._signal_reader_fault(process, "hermes_gateway_exited")
                return
            if isinstance(raw, str):
                raw = raw.encode("utf-8", errors="replace")
            if len(raw) > _MAX_PROTOCOL_LINE_BYTES or not raw.endswith(b"\n"):
                self._signal_reader_fault(process, "hermes_gateway_frame_too_large")
                return
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                invalid_lines += 1
                if invalid_lines > 20:
                    self._signal_reader_fault(
                        process, "hermes_gateway_protocol_error"
                    )
                    return
                continue
            if not isinstance(message, Mapping):
                self._signal_reader_fault(process, "hermes_gateway_protocol_error")
                return
            invalid_lines = 0
            if not self._queue_put(
                messages,
                process,
                _QueuedMessage(message, len(raw)),
            ):
                return

    def _queue_put(
        self,
        messages: queue.Queue[object],
        process: subprocess.Popen[bytes],
        item: object,
    ) -> bool:
        if not isinstance(item, _QueuedMessage):
            self._signal_reader_fault(process, "hermes_gateway_protocol_error")
            return False
        with self._state_lock:
            current = self._process is process
            if not current or self._reader_fault is not None:
                return False
            if self._queued_bytes + item.size > _MAX_QUEUED_BYTES:
                overflow = True
            else:
                try:
                    messages.put_nowait(item)
                    self._queued_bytes += item.size
                    return True
                except queue.Full:
                    overflow = True
        if overflow:
            self._signal_reader_fault(process, "hermes_gateway_queue_overflow")
        return False

    def _signal_reader_fault(
        self, process: subprocess.Popen[bytes], reason: str
    ) -> None:
        with self._state_lock:
            if self._process is not process:
                return
            if self._reader_fault is None:
                self._reader_fault = reason
            job_handle = self._job_handle
            self._job_handle = None
        if process.poll() is None:
            self._terminate_tree(process, job_handle)
        try:
            self._messages.put_nowait(_ReaderFault(reason))
        except queue.Full:
            pass

    def _read_stderr(self, stream: BinaryIO) -> None:
        while True:
            try:
                chunk = stream.read(8_192)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8", errors="replace")
            with self._state_lock:
                self._stderr_tail.extend(chunk)
                overflow = len(self._stderr_tail) - _MAX_STDERR_BYTES
                if overflow > 0:
                    del self._stderr_tail[:overflow]

    def _stop_process(self) -> None:
        with self._state_lock:
            process = self._process
            job_handle = self._job_handle
            self._process = None
            self._job_handle = None
            self._ready = False
        if process is None:
            self._close_windows_handle(job_handle)
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            self._terminate_tree(process, job_handle)
            job_handle = None
        else:
            self._close_windows_handle(job_handle)
            job_handle = None
        timed_out = False
        try:
            process.wait(timeout=self.config.shutdown_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        if os.name != "nt":
            # The group may still contain a TERM-ignoring descendant even
            # after the direct child exits. Always finish the owned group.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                if timed_out:
                    try:
                        process.kill()
                    except OSError:
                        pass
        elif timed_out:
            try:
                process.kill()
            except OSError:
                pass
        if timed_out:
            try:
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        for thread in (self._reader_thread, self._stderr_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=0.5)
        self._reader_thread = None
        self._stderr_thread = None

    def _assign_windows_kill_job(self, process: subprocess.Popen[bytes]):
        if os.name != "nt":
            return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise HermesUnavailableError("hermes_windows_job_setup_failed")
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x00002000
        configured = kernel32.SetInformationJobObject(
            handle,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        )
        process_handle = wintypes.HANDLE(int(getattr(process, "_handle", 0)))
        assigned = bool(process_handle) and kernel32.AssignProcessToJobObject(
            handle, process_handle
        )
        if not configured or not assigned:
            self._close_windows_handle(handle)
            raise HermesUnavailableError("hermes_windows_job_setup_failed")
        return handle

    @staticmethod
    def _close_windows_handle(handle) -> None:
        if os.name != "nt" or not handle:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(handle)

    def _terminate_tree(self, process: subprocess.Popen[bytes], job_handle=None) -> None:
        if os.name == "nt":
            if job_handle:
                # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE atomically terminates the
                # exact process tree assigned at creation; it cannot hit a
                # different process after PID reuse.
                self._close_windows_handle(job_handle)
                return
            try:
                process.kill()
            except OSError:
                pass
            return
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                return
            except (OSError, ProcessLookupError):
                pass
        try:
            process.terminate()
        except OSError:
            pass


def _looks_like_runtime(path: Path) -> bool:
    return (path / "pyproject.toml").is_file() and (
        path / "tui_gateway" / "entry.py"
    ).is_file()


def _resolve_trusted_git() -> Path | None:
    """Resolve Git without consulting the user-controlled PATH."""

    candidates: list[Path] = []
    if os.name == "nt":
        try:
            shell32 = ctypes.WinDLL("shell32", use_last_error=True)
            shell32.SHGetFolderPathW.argtypes = [
                wintypes.HWND,
                ctypes.c_int,
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.LPWSTR,
            ]
            shell32.SHGetFolderPathW.restype = ctypes.c_long
            for folder_id in (0x26, 0x2A):  # Program Files, Program Files (x86)
                buffer = ctypes.create_unicode_buffer(32_768)
                if shell32.SHGetFolderPathW(None, folder_id, None, 0, buffer) == 0:
                    base = Path(buffer.value)
                    candidates.extend(
                        (base / "Git" / "cmd" / "git.exe", base / "Git" / "bin" / "git.exe")
                    )
        except (AttributeError, OSError, ValueError):
            return None
    else:
        candidates.extend(
            Path(value)
            for value in ("/usr/bin/git", "/usr/local/bin/git", "/opt/homebrew/bin/git")
        )

    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            mode = resolved.lstat().st_mode
            if not stat.S_ISREG(mode) or candidate.is_symlink():
                continue
            if os.name == "nt":
                if not _windows_signature_is_valid(resolved):
                    continue
            else:
                metadata = resolved.stat()
                if metadata.st_uid != 0 or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    continue
            probe_env = {
                key: value
                for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR")
                if (value := os.environ.get(key)) is not None
            }
            probe_env.update(
                {
                    "LC_ALL": "C",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_SYSTEM": os.devnull,
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_TERMINAL_PROMPT": "0",
                }
            )
            version = subprocess.run(
                [str(resolved), "--version"],
                cwd=str(resolved.parent),
                env=probe_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    if os.name == "nt"
                    else 0
                ),
            )
            if version.returncode == 0 and re.fullmatch(
                rb"git version [0-9]+\.[0-9]+(?:\.[0-9]+)?(?:[^\r\n]*)?\r?\n?",
                version.stdout[:256],
            ):
                return resolved
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
    return None


def _windows_signature_is_valid(path: Path) -> bool:
    if os.name != "nt":
        return False

    class _Guid(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class _WintrustFileInfo(ctypes.Structure):
        _fields_ = [
            ("cbStruct", wintypes.DWORD),
            ("pcwszFilePath", wintypes.LPCWSTR),
            ("hFile", wintypes.HANDLE),
            ("pgKnownSubject", ctypes.c_void_p),
        ]

    class _WintrustData(ctypes.Structure):
        _fields_ = [
            ("cbStruct", wintypes.DWORD),
            ("pPolicyCallbackData", ctypes.c_void_p),
            ("pSIPClientData", ctypes.c_void_p),
            ("dwUIChoice", wintypes.DWORD),
            ("fdwRevocationChecks", wintypes.DWORD),
            ("dwUnionChoice", wintypes.DWORD),
            ("pFile", ctypes.POINTER(_WintrustFileInfo)),
            ("dwStateAction", wintypes.DWORD),
            ("hWVTStateData", wintypes.HANDLE),
            ("pwszURLReference", wintypes.LPCWSTR),
            ("dwProvFlags", wintypes.DWORD),
            ("dwUIContext", wintypes.DWORD),
        ]

    action = _Guid(
        0x00AAC56B,
        0xCD44,
        0x11D0,
        (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE),
    )
    file_info = _WintrustFileInfo(
        ctypes.sizeof(_WintrustFileInfo), str(path), None, None
    )
    trust_data = _WintrustData()
    trust_data.cbStruct = ctypes.sizeof(_WintrustData)
    trust_data.dwUIChoice = 2  # WTD_UI_NONE
    trust_data.fdwRevocationChecks = 0  # WTD_REVOKE_NONE
    trust_data.dwUnionChoice = 1  # WTD_CHOICE_FILE
    trust_data.pFile = ctypes.pointer(file_info)
    trust_data.dwProvFlags = 0x00001000  # WTD_CACHE_ONLY_URL_RETRIEVAL
    try:
        wintrust = ctypes.WinDLL("wintrust", use_last_error=True)
        wintrust.WinVerifyTrust.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(_Guid),
            ctypes.POINTER(_WintrustData),
        ]
        wintrust.WinVerifyTrust.restype = ctypes.c_long
        return wintrust.WinVerifyTrust(
            wintypes.HWND(-1), ctypes.byref(action), ctypes.byref(trust_data)
        ) == 0
    except (AttributeError, OSError, ValueError):
        return False


def _is_regular_unsymlinked_file(path: Path) -> bool:
    try:
        is_junction = getattr(path, "is_junction", lambda: False)()
        return (
            not path.is_symlink()
            and not is_junction
            and stat.S_ISREG(path.lstat().st_mode)
        )
    except OSError:
        return False


def _attestation_forbidden(relative: PurePosixPath | Path) -> bool:
    return "__pycache__" in relative.parts or relative.suffix.lower() in {
        ".pyc",
        ".pyo",
    }


def _sha256_file(path: Path, cancelled=None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            if _is_cancelled(cancelled):
                raise _HermesCancelled()
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _make_removable_and_retry(function, path: str, _exc_info) -> None:
    """Allow rmtree to remove the read-only, adapter-owned source snapshot."""

    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    function(path)


def _safe_display_text(value: str, limit: int) -> str:
    cleaned: list[str] = []
    for character in value:
        category = unicodedata.category(character)
        if category in {"Cc", "Cf"}:
            continue
        cleaned.append(character)
        if len(cleaned) >= limit:
            break
    return "".join(cleaned)


def _normalized_delta_chunks(text: str) -> list[Mapping[str, Any]]:
    return [
        {"type": "message.delta", "payload": {"content": text[index : index + 2_000]}}
        for index in range(0, len(text), 2_000)
    ]


def _read_pyproject_version(path: Path) -> str | None:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    match = re.search(r'^version\s*=\s*["\']([^"\']+)["\']\s*$', content, re.MULTILINE)
    return match.group(1) if match else None


def _is_loopback_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"}:
            return False
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return False
        hostname = (parsed.hostname or "").rstrip(".").lower()
        if "%" in hostname:
            return False
        # Accessing port also rejects malformed/non-numeric values.
        _ = parsed.port
        return hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
    except (ValueError, TypeError):
        return False


def _gateway_model(model: str) -> str:
    value = model.strip()
    return value.removeprefix("openai/") or value


def _render_prompt(messages: Sequence[Mapping[str, Any]]) -> str:
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        raise TypeError("messages must be a sequence")
    rendered: list[tuple[str, str]] = []
    for message in messages:
        if not isinstance(message, Mapping):
            raise TypeError("message must be a mapping")
        role = message.get("role")
        content = message.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            raise ValueError("invalid message")
        if content:
            rendered.append((role, content))
    if not rendered:
        raise ValueError("empty prompt")
    if len(rendered) == 1 and rendered[0][0] == "user":
        prompt = rendered[0][1]
    else:
        prompt = "\n\n".join(f"[{role}]\n{content}" for role, content in rendered)
    if len(prompt.encode("utf-8")) > _MAX_REQUEST_BYTES // 2:
        raise ValueError("prompt too large")
    return prompt


def _validated_image(image: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(image, Mapping):
        raise TypeError("image must be a mapping")
    content = image.get("content")
    image_format = image.get("format")
    if not isinstance(content, str) or not isinstance(image_format, str):
        raise ValueError("invalid image")
    match = re.fullmatch(r"base64\.(png|jpe?g|webp)", image_format.lower())
    if match is None:
        raise ValueError("unsupported image")
    if len(content) > ((_MAX_IMAGE_BYTES + 2) // 3) * 4 + 4:
        raise ValueError("image too large")
    try:
        decoded = base64.b64decode(content, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid image") from exc
    if not decoded or len(decoded) > _MAX_IMAGE_BYTES:
        raise ValueError("invalid image size")
    extension = "jpg" if match.group(1) in {"jpg", "jpeg"} else match.group(1)
    valid_signature = (
        decoded.startswith(b"\x89PNG\r\n\x1a\n")
        if extension == "png"
        else decoded.startswith(b"\xff\xd8\xff")
        if extension == "jpg"
        else decoded.startswith(b"RIFF")
        and len(decoded) >= 12
        and decoded[8:12] == b"WEBP"
    )
    if not valid_signature:
        raise ValueError("image signature mismatch")
    return content, extension


def _event_parts(
    message: Mapping[str, Any],
) -> tuple[str, str | None, Mapping[str, Any]] | None:
    if message.get("method") != "event":
        return None
    params = message.get("params")
    if not isinstance(params, Mapping):
        return None
    event_type = params.get("type")
    if not isinstance(event_type, str) or not event_type:
        return None
    session_id = params.get("session_id")
    payload = params.get("payload")
    return (
        event_type,
        session_id if isinstance(session_id, str) else None,
        payload if isinstance(payload, Mapping) else {},
    )


def _is_response(message: Mapping[str, Any]) -> bool:
    return "id" in message and ("result" in message or "error" in message)


def _normalize_event(
    event_type: str, payload: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    normalized: dict[str, Any]
    if event_type == "message.delta":
        text = payload.get("text")
        normalized = {
            "content": text if isinstance(text, str) else "",
            "status": str(payload.get("status") or "")[:100],
        }
    elif event_type == "message.complete":
        text = payload.get("text")
        normalized = {
            "content_chars": len(text) if isinstance(text, str) else 0,
            "status": str(payload.get("status") or "")[:100],
        }
    elif event_type.startswith("tool."):
        normalized = {}
        for key in ("tool_call_id", "id", "name", "tool_name", "status", "stage"):
            value = payload.get(key)
            if isinstance(value, str):
                normalized[key] = value[:200]
        for key in ("progress", "percent", "duration_ms"):
            value = payload.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                normalized[key] = value
        args = payload.get("args")
        if isinstance(args, Mapping):
            normalized["argument_keys"] = sorted(str(key)[:100] for key in args)[:100]
        result = payload.get("result")
        if isinstance(result, str):
            normalized["result_chars"] = len(result)
    elif event_type == "approval.request":
        normalized = {}
        for key in ("title", "name", "description"):
            value = payload.get(key)
            if isinstance(value, str):
                normalized[key] = value[:500]
        choices = payload.get("choices")
        if isinstance(choices, Sequence) and not isinstance(choices, (str, bytes)):
            normalized["choices"] = [
                str(choice)[:50] for choice in list(choices)[:10]
            ]
        command = payload.get("command")
        if isinstance(command, str):
            normalized["command_hash"] = hashlib.sha256(
                command.encode("utf-8")
            ).hexdigest()
            normalized["command_present"] = bool(command)
            normalized["display_command"] = _safe_display_text(
                command, _MAX_DISPLAY_COMMAND_CHARS
            )
    elif event_type.endswith(".request"):
        normalized = {"request_present": bool(payload.get("request_id"))}
    elif event_type in {"error", "status", "session.info"}:
        normalized = {"payload_keys": sorted(str(key)[:100] for key in payload)[:100]}
    else:
        return None
    return {"type": event_type, "payload": normalized}


def _notify(
    notifier: Callable[[Mapping[str, Any]], Any] | None,
    event: Mapping[str, Any],
) -> None:
    if notifier is None:
        return
    try:
        notifier(event)
    except Exception:
        # Rendering/activity sinks cannot change the sidecar security outcome.
        return


def _is_cancelled(cancelled: Any) -> bool:
    try:
        return bool(cancelled.is_set())
    except Exception:
        return False


__all__ = [
    "HERMES_DESKTOP_CONTRACT",
    "HERMES_REVISION",
    "HERMES_TAG",
    "HERMES_VERSION",
    "HermesAdapterError",
    "HermesConfig",
    "HermesGatewayAdapter",
    "HermesInteractionUnsupported",
    "HermesProtocolError",
    "HermesRequest",
    "HermesStatus",
    "HermesUnavailableError",
]
