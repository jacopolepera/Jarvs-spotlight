"""Managed out-of-process execution backend for explicit native tools.

This backend provides process separation, bounded output, cancellation, timeout,
an explicit argv vector, a fixed workspace cwd, and a reduced environment.  It
is not yet a strong OS security sandbox; code execution therefore remains
approval-only even when the global policy is auto-run.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .tooling import ToolContext, ToolDefinition, ToolValidationError
from .workspace_tools import _resolve_workspace_path, _workspace_root, _write_text


MAX_PROCESS_OUTPUT_BYTES = 256 * 1024
DEFAULT_PROCESS_TIMEOUT_SECONDS = 20


class ProcessExecutionCancelled(RuntimeError):
    pass


class _BoundedPipe:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    def consume(self, pipe) -> None:
        try:
            while True:
                chunk = pipe.read(8192)
                if not chunk:
                    return
                remaining = self.limit - len(self.data)
                if remaining > 0:
                    self.data.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self.truncated = True
        finally:
            pipe.close()

    def text(self) -> str:
        return bytes(self.data).decode("utf-8", errors="replace")


class ManagedProcessBackend:
    def __init__(
        self,
        *,
        max_output_bytes: int = MAX_PROCESS_OUTPUT_BYTES,
        default_timeout_seconds: int = DEFAULT_PROCESS_TIMEOUT_SECONDS,
    ) -> None:
        if max_output_bytes <= 0 or default_timeout_seconds <= 0:
            raise ValueError("managed process limits must be positive")
        self.max_output_bytes = max_output_bytes
        self.default_timeout_seconds = default_timeout_seconds

    def execute(
        self,
        definition: ToolDefinition,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        root = _workspace_root(context)
        saved_script = None
        if definition.name == "script.create_and_run_python":
            saved_script = _write_text(
                {
                    "path": arguments["path"],
                    "content": arguments["code"],
                    "overwrite": arguments.get("overwrite", False),
                    "create_parents": arguments.get("create_parents", False),
                },
                context,
            )
        command = self._command(definition.name, arguments, context)
        timeout = int(
            arguments.get("timeout_seconds", self.default_timeout_seconds)
        )
        environment = self._environment(root)
        creationflags = 0
        popen_kwargs: dict[str, Any] = {}
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            popen_kwargs["start_new_session"] = True
        process = subprocess.Popen(
            command,
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=creationflags,
            **popen_kwargs,
        )
        assert process.stdout is not None and process.stderr is not None
        stdout = _BoundedPipe(self.max_output_bytes)
        stderr = _BoundedPipe(self.max_output_bytes)
        readers = (
            threading.Thread(target=stdout.consume, args=(process.stdout,), daemon=True),
            threading.Thread(target=stderr.consume, args=(process.stderr,), daemon=True),
        )
        for reader in readers:
            reader.start()

        deadline = time.monotonic() + timeout
        timed_out = False
        cancelled = False
        while process.poll() is None:
            if context.cancelled.wait(0.05):
                cancelled = True
                self._terminate_tree(process)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                self._terminate_tree(process)
                break
        try:
            return_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._terminate_tree(process)
            return_code = process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=2)
        if cancelled:
            raise ProcessExecutionCancelled("managed process execution was cancelled")
        result = {
            "program": arguments.get("program", "python"),
            "exit_code": return_code,
            "stdout": stdout.text(),
            "stderr": stderr.text(),
            "stdout_truncated": stdout.truncated,
            "stderr_truncated": stderr.truncated,
            "timed_out": timed_out,
            "timeout_seconds": timeout,
        }
        if saved_script is not None:
            result["script"] = {
                **saved_script,
                "language": "python",
                "syntax_valid": True,
            }
        return result

    def _command(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> list[str]:
        if tool_name in {"script.run_python", "script.create_and_run_python"}:
            _root, path = _resolve_workspace_path(context, arguments["path"])
            return [
                sys.executable,
                "-I",
                str(path),
                *self._parse_argv(arguments.get("arguments_json", "[]")),
            ]
        if tool_name == "system.run_command":
            program = arguments["program"]
            supplied = self._parse_argv(arguments["arguments_json"])
            if program == "python":
                return [sys.executable, "-I", *supplied]
            if program == "powershell":
                return [str(self._powershell_path()), *supplied]
        raise ToolValidationError(
            "The managed process backend does not support this tool.",
            reason_code="unsupported_process_tool",
        )

    @staticmethod
    def _parse_argv(raw: object) -> list[str]:
        try:
            values = json.loads(raw) if isinstance(raw, str) else None
        except json.JSONDecodeError as exc:
            raise ToolValidationError(
                "Command arguments are not valid JSON.",
                reason_code="invalid_command_arguments",
            ) from exc
        if (
            not isinstance(values, list)
            or len(values) > 32
            or any(not isinstance(item, str) or len(item) > 4096 for item in values)
        ):
            raise ToolValidationError(
                "Command arguments must be a bounded JSON array of strings.",
                reason_code="invalid_command_arguments",
            )
        return values

    @staticmethod
    def _environment(root: Path) -> dict[str, str]:
        temporary = root / ".jarvs" / "tmp"
        temporary.mkdir(parents=True, exist_ok=True)
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        python_dir = Path(sys.executable).parent
        path_parts = [
            str(python_dir),
            str(system_root / "System32"),
            str(system_root),
            str(system_root / "System32" / "WindowsPowerShell" / "v1.0"),
        ]
        return {
            "PATH": os.pathsep.join(path_parts),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "SystemRoot": str(system_root),
            "WINDIR": str(system_root),
        }

    @staticmethod
    def _powershell_path() -> Path:
        if os.name != "nt":
            return Path("pwsh")
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        executable = (
            system_root
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        if not executable.exists():
            raise FileNotFoundError("Windows PowerShell is not available")
        return executable

    @staticmethod
    def _terminate_tree(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
            taskkill = system_root / "System32" / "taskkill.exe"
            try:
                subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                return
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                return
            except OSError:
                pass
        try:
            process.kill()
        except OSError:
            pass
