import asyncio
import json

from core.agent.approvals import ApprovalBroker
from core.agent.events import RunEventStream
from core.agent.executor import ToolExecutor
from core.agent.policy import evaluate_native_tool_execution
from core.agent.process_backend import ManagedProcessBackend
from core.agent.script_tools import register_script_tools
from core.agent.system_tools import register_system_tools
from core.agent.tooling import (
    ToolBackend,
    ToolContext,
    ToolEffect,
    ToolInvocation,
    ToolRegistry,
    ToolStatus,
)
from core.agent.workspace_tools import register_workspace_tools


def native_runtime(tmp_path, *, approve=False, output_limit=256 * 1024):
    registry = ToolRegistry()
    register_workspace_tools(registry)
    register_script_tools(registry)
    register_system_tools(registry)
    registry.freeze()
    broker = ApprovalBroker(timeout_seconds=2)
    approvals = []

    def notifier(event):
        approvals.append(event)
        if approve:
            broker.resolve(
                event["approval_id"],
                approve=True,
                session_id=event["session_id"],
                generation_id=event["generation_id"],
            )

    context = ToolContext(
        run_id="run",
        session_id="session",
        generation_id="generation",
        workspace_root=tmp_path,
        events=RunEventStream("run"),
        approval_notifier=notifier if approve else None,
    )
    executor = ToolExecutor(
        registry,
        broker,
        lambda: "auto_run",
        policy_evaluator=evaluate_native_tool_execution,
        allowed_in_process_effects=frozenset(
            {ToolEffect.READ_ONLY, ToolEffect.WORKSPACE_WRITE}
        ),
        backend_executors={
            ToolBackend.ISOLATED_PROCESS: ManagedProcessBackend(
                max_output_bytes=output_limit
            ).execute
        },
    )
    return executor, context, approvals


def execute(executor, context, name, arguments):
    return asyncio.run(
        executor.execute(context, ToolInvocation.create(name, arguments))
    )


def test_python_script_is_validated_saved_approved_and_run(tmp_path):
    executor, context, approvals = native_runtime(tmp_path, approve=True)
    saved = execute(
        executor,
        context,
        "script.save_python",
        {
            "path": "scripts/hello.py",
            "code": "import sys\nprint('hello', sys.argv[1])\n",
            "create_parents": True,
        },
    )
    assert saved.status is ToolStatus.COMPLETED
    assert saved.output["syntax_valid"] is True

    result = execute(
        executor,
        context,
        "script.run_python",
        {"path": "scripts/hello.py", "arguments_json": json.dumps(["jarvs"])},
    )
    assert result.status is ToolStatus.COMPLETED
    assert result.output["exit_code"] == 0
    assert result.output["stdout"].strip() == "hello jarvs"
    assert len(approvals) == 1
    assert approvals[0]["tool_name"] == "script.run_python"


def test_create_and_run_python_is_one_approved_typed_operation(tmp_path):
    executor, context, approvals = native_runtime(tmp_path, approve=True)
    result = execute(
        executor,
        context,
        "script.create_and_run_python",
        {
            "path": "scripts/combined.py",
            "code": "print('combined hello')\n",
            "arguments_json": "[]",
            "create_parents": True,
        },
    )

    assert result.status is ToolStatus.COMPLETED
    assert result.output["exit_code"] == 0
    assert result.output["stdout"].strip() == "combined hello"
    assert result.output["script"]["syntax_valid"] is True
    assert (tmp_path / "scripts" / "combined.py").read_text(encoding="utf-8") == (
        "print('combined hello')\n"
    )
    assert len(approvals) == 1
    assert approvals[0]["tool_name"] == "script.create_and_run_python"
    assert approvals[0]["arguments"]["code"] == "print('combined hello')\n"


def test_native_code_execution_cannot_inherit_auto_run_without_approval(tmp_path):
    executor, context, approvals = native_runtime(tmp_path, approve=False)
    marker = tmp_path / "marker.txt"
    result = execute(
        executor,
        context,
        "system.run_command",
        {
            "program": "python",
            "arguments_json": json.dumps([
                "-c",
                "from pathlib import Path; Path('marker.txt').write_text('ran')",
            ]),
        },
    )
    assert result.status is ToolStatus.DENIED
    assert result.reason_code == "approval_channel_unavailable"
    assert approvals == []
    assert not marker.exists()


def test_managed_process_has_timeout_and_bounded_output(tmp_path):
    executor, context, _approvals = native_runtime(
        tmp_path,
        approve=True,
        output_limit=128,
    )
    bounded = execute(
        executor,
        context,
        "system.run_command",
        {
            "program": "python",
            "arguments_json": json.dumps(["-c", "print('x' * 1000)"]),
        },
    )
    assert bounded.status is ToolStatus.COMPLETED
    assert len(bounded.output["stdout"].encode("utf-8")) <= 128
    assert bounded.output["stdout_truncated"] is True

    timed = execute(
        executor,
        context,
        "system.run_command",
        {
            "program": "python",
            "arguments_json": json.dumps(["-c", "import time; time.sleep(10)"]),
            "timeout_seconds": 1,
        },
    )
    assert timed.status is ToolStatus.COMPLETED
    assert timed.output["timed_out"] is True


def test_invalid_python_is_denied_before_write(tmp_path):
    executor, context, _approvals = native_runtime(tmp_path)
    result = execute(
        executor,
        context,
        "script.save_python",
        {"path": "broken.py", "code": "def broken(:\n"},
    )
    assert result.status is ToolStatus.DENIED
    assert result.reason_code == "python_syntax_invalid"
    assert not (tmp_path / "broken.py").exists()
