import threading

import pytest

from core.agent.approvals import ApprovalBroker
from core.agent.action_routing import ACTION_DISPATCH_PROVIDER_NAME
from core.agent.coordinator import NativeSessionService, RunCoordinator, load_system_prompt
from core.agent.egress import DataEgressPolicy
from core.agent.executor import ToolExecutor
from core.agent.model_adapter import ModelGenerationCancelled, ModelStreamEvent, ModelToolCallDelta, ProviderRequestError
from core.agent.routing import RoutingSignal
from core.agent.script_tools import register_script_tools
from core.agent.system_tools import register_system_tools
from core.agent.tooling import (
    ToolBackend,
    ToolEffect,
    ToolInvocation,
    ToolRegistry,
)
from core.agent.workspace_tools import register_workspace_tools


class FakeSettings:
    def __init__(self, root, *, provider="local", egress="deny", policy="auto_run"):
        self.root_dir = str(root)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.provider = provider
        self.egress = egress
        self.policy = policy

    def get_workspace_path(self):
        return str(self.workspace)

    def get_execution_policy(self):
        return self.policy

    def get_cloud_workspace_egress(self):
        return self.egress

    def get_active_model(self):
        if self.provider == "local":
            return {
                "id": "local",
                "provider": "local",
                "model_string": "openai/test-model",
                "api_base": "http://127.0.0.1:1234/v1",
                "api_key": "lm-studio",
            }
        return {
            "id": "cloud",
            "provider": "cloud",
            "model_string": "gpt-test",
            "api_key": "secret",
        }


class FixtureAdapter:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def stream(self, context, request, cancelled):
        self.requests.append((context, request))
        response = self.responses.pop(0)
        for event in response:
            yield event


def service(
    tmp_path,
    adapter,
    *,
    provider="local",
    egress="deny",
    policy="auto_run",
    action_backend=None,
    event_sinks=(),
    memory_context_provider=None,
):
    settings = FakeSettings(tmp_path, provider=provider, egress=egress, policy=policy)
    registry = ToolRegistry()
    register_workspace_tools(registry)
    register_script_tools(registry)
    register_system_tools(registry)
    registry.freeze()
    broker = ApprovalBroker(timeout_seconds=1)
    executor = ToolExecutor(
        registry,
        broker,
        settings.get_execution_policy,
        allowed_in_process_effects=frozenset(
            {ToolEffect.READ_ONLY, ToolEffect.WORKSPACE_WRITE}
        ),
        backend_executors=(
            {ToolBackend.ISOLATED_PROCESS: action_backend}
            if action_backend is not None
            else None
        ),
    )
    coordinator = RunCoordinator(
        settings_manager=settings,
        model_adapter=adapter,
        registry=registry,
        executor=executor,
        egress_policy=DataEgressPolicy(settings.get_cloud_workspace_egress),
        system_prompt_provider=lambda: load_system_prompt(settings),
        memory_context_provider=memory_context_provider,
        event_sinks=event_sinks,
    )
    return (
        NativeSessionService(
            coordinator,
            approval_broker=broker,
            execution_policy_provider=settings.get_execution_policy,
            initial_session_id="session",
        ),
        settings,
    )


def test_provider_failure_emits_one_terminal_run_failure(tmp_path):
    captured = []

    class FailingAdapter:
        def stream(self, _context, _request, _cancelled):
            raise ProviderRequestError("provider unavailable")
            yield  # pragma: no cover - keep this a generator adapter

    native, _settings = service(
        tmp_path,
        FailingAdapter(),
        event_sinks=(captured.append,),
    )
    generation = native.start_generation("session", "provider-failure")

    with pytest.raises(ProviderRequestError):
        list(
            native.stream(
                generation,
        [{"role": "user", "type": "message", "content": "Hello"}],
            )
        )

    terminal = [event for event in captured if event.type.startswith("run.")][-1]
    assert terminal.type == "run.failed"
    assert terminal.payload["reason_code"] == "provider_request_failed"
    assert terminal.payload["error_type"] == "ProviderRequestError"


def test_fast_path_exposes_only_constant_dispatch(tmp_path):
    adapter = FixtureAdapter([[ModelStreamEvent(content="Hello"), ModelStreamEvent(finish_reason="stop")]])
    native, _settings = service(tmp_path, adapter)
    generation = native.start_generation("session", "generation")

    chunks = list(native.stream(generation, [{"role": "user", "type": "message", "content": "Hello"}]))

    assert "".join(chunk.get("content", "") for chunk in chunks if chunk.get("type") == "message") == "Hello"
    tools = adapter.requests[0][1].tools
    assert len(tools) == 1
    assert tools[0]["function"]["name"] == ACTION_DISPATCH_PROVIDER_NAME
    assert adapter.requests[0][1].temperature == 0.0
    assert adapter.requests[0][1].max_tokens == 2_048
    assert [item["content"]["event"] for item in chunks if item.get("type") == "activity"][:2] == [
        "run.started",
        "route.selected",
    ]


@pytest.mark.parametrize(
    ("provider", "memory_expected"),
    (("local", True), ("cloud", False)),
)
def test_personal_memory_context_is_local_only(tmp_path, provider, memory_expected):
    adapter = FixtureAdapter(
        [[ModelStreamEvent(content="Hello"), ModelStreamEvent(finish_reason="stop")]]
    )
    memory_calls = []
    native, _settings = service(
        tmp_path,
        adapter,
        provider=provider,
        egress="allow",
        memory_context_provider=lambda prompt: memory_calls.append(prompt)
        or "Saved memory: private preference",
    )
    generation = native.start_generation("session", "generation")

    list(native.stream(generation, [{"role": "user", "content": "Hello"}]))

    system_prompt = adapter.requests[0][1].messages[0]["content"]
    assert ("Saved memory: private preference" in system_prompt) is memory_expected
    assert bool(memory_calls) is memory_expected


def test_unambiguous_python_create_and_run_uses_one_tool_and_no_final_model_turn(
    tmp_path,
):
    backend_calls = []

    def backend(definition, arguments, _context):
        backend_calls.append((definition.name, dict(arguments)))
        return {
            "exit_code": 0,
                "stdout": "colored hello\n",
            "stderr": "",
            "timed_out": False,
            "script": {
                "path": arguments["path"],
                "bytes": len(arguments["code"].encode("utf-8")),
                "sha256": "test",
                "operation": "created",
                "language": "python",
                "syntax_valid": True,
            },
        }

    adapter = FixtureAdapter(
        [
            [
                ModelStreamEvent(
                    tool_calls=(
                        ModelToolCallDelta(
                            index=0,
                            call_id="create-run",
                            name="script__create_and_run_python",
                            arguments=(
                                '{"path":"hello_test.py","code":'
                                    '"print(\\"colored hello\\")\\n"}'
                            ),
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            ]
        ]
    )
    native, _settings = service(tmp_path, adapter, action_backend=backend)
    generation = native.start_generation("session", "generation")

    chunks = list(
        native.stream(
            generation,
            [
                {
                    "role": "user",
                    "content": (
                        "Create a Python script named hello_test.py that prints colored "
                        "hello world text in the terminal, then run it."
                    ),
                }
            ],
        )
    )

    assert len(adapter.requests) == 1
    assert [
        tool["function"]["name"] for tool in adapter.requests[0][1].tools
    ] == ["script__create_and_run_python"]
    assert backend_calls[0][0] == "script.create_and_run_python"
    answer = "".join(
        item.get("content", "")
        for item in chunks
        if item.get("type") == "message"
    )
    assert "created and ran successfully" in answer
    assert "colored hello" in answer
    routes = [
        item["content"]["payload"]
        for item in chunks
        if item.get("type") == "activity"
        and item["content"]["event"] == "route.selected"
    ]
    assert routes[-1]["reason_code"] == "deterministic_script_create_run_hint"
    completed = [
        item["content"]["payload"]
        for item in chunks
        if item.get("type") == "activity"
        and item["content"]["event"] == "run.completed"
    ]
    assert completed[-1]["deterministic_runtime_summary"] is True


def test_model_dispatches_to_native_workspace_tools_and_writes_file(tmp_path):
    adapter = FixtureAdapter(
        [
            [
                ModelStreamEvent(
                    tool_calls=(
                        ModelToolCallDelta(
                            index=0,
                            call_id="dispatch-1",
                            name=ACTION_DISPATCH_PROVIDER_NAME,
                            arguments='{"profile":"automate"}',
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            [
                ModelStreamEvent(
                    tool_calls=(
                        ModelToolCallDelta(
                            index=0,
                            call_id="write-1",
                            name="workspace__write_text",
                            arguments=(
                                '{"path":"hello.txt","content":"hello",'
                                '"overwrite":false}'
                            ),
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            [
                ModelStreamEvent(content="File created."),
                ModelStreamEvent(finish_reason="stop"),
            ],
        ]
    )
    native, settings = service(tmp_path, adapter)
    generation = native.start_generation("session", "generation")

    chunks = list(
        native.stream(
            generation,
            [{"role": "user", "content": "Create hello.txt containing hello"}],
        )
    )

    assert (settings.workspace / "hello.txt").read_text(encoding="utf-8") == "hello"
    assert len(adapter.requests[0][1].tools) == 1
    selected = {
        item["function"]["name"] for item in adapter.requests[1][1].tools
    }
    assert "workspace__write_text" in selected
    assert "script__save_python" in selected
    assert "script__run_python" in selected
    assert adapter.requests[1][1].temperature == 0.0
    assert adapter.requests[1][1].max_tokens == 2_048
    assert "/no_think" in adapter.requests[1][1].messages[0]["content"]
    assert any(item.get("content") == "File created." for item in chunks)
    routes = [
        item["content"]["payload"]
        for item in chunks
        if item.get("type") == "activity"
        and item["content"]["event"] == "route.selected"
    ]
    assert routes[-1]["lane"] == "action"
    assert routes[-1]["profile"] == "automate"


def test_composite_script_flow_recovers_from_generic_writer_then_runs(tmp_path):
    backend_calls = []

    def backend(definition, arguments, _context):
        backend_calls.append((definition.name, dict(arguments)))
        return {
            "exit_code": 0,
            "stdout": "colored hello\n",
            "stderr": "",
            "timed_out": False,
        }

    code = 'print("colored hello")\n'
    adapter = FixtureAdapter(
        [
            [
                ModelStreamEvent(
                    tool_calls=(
                        ModelToolCallDelta(
                            index=0,
                            call_id="dispatch-script",
                            name=ACTION_DISPATCH_PROVIDER_NAME,
                            arguments='{"profile":"automate"}',
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            [
                ModelStreamEvent(
                    tool_calls=(
                        ModelToolCallDelta(
                            index=0,
                            call_id="wrong-writer",
                            name="workspace__write_text",
                            arguments=(
                                '{"path":"hello.py","content":'
                                '"print(\\"colored hello\\")\\n"}'
                            ),
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            [
                ModelStreamEvent(
                    tool_calls=(
                        ModelToolCallDelta(
                            index=0,
                            call_id="save-script",
                            name="script__save_python",
                            arguments=(
                                '{"path":"hello.py","code":'
                                '"print(\\"colored hello\\")\\n"}'
                            ),
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            [
                ModelStreamEvent(
                    tool_calls=(
                        ModelToolCallDelta(
                            index=0,
                            call_id="run-script",
                            name="script__run_python",
                            arguments='{"path":"hello.py","arguments_json":"[]"}',
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            [
                ModelStreamEvent(content="Script created and executed."),
                ModelStreamEvent(finish_reason="stop"),
            ],
        ]
    )
    native, settings = service(tmp_path, adapter, action_backend=backend)
    generation = native.start_generation("session", "generation")

    chunks = list(
        native.stream(
            generation,
            [
                {
                    "role": "user",
                    "content": (
                        "I need a Python program: prepare it and show me the result"
                    ),
                }
            ],
        )
    )

    assert (settings.workspace / "hello.py").read_text(encoding="utf-8") == code
    assert backend_calls == [
        ("script.run_python", {"path": "hello.py", "arguments_json": "[]"})
    ]
    correction_messages = adapter.requests[2][1].messages
    assert any(
        message.get("role") == "tool"
        and '"status": "rejected"' in message["content"]
        and "typed_script_tool_required" in message["content"]
        for message in correction_messages
    )
    assert not any(item.get("type") == "execution_denied" for item in chunks)
    assert any(
        item.get("content") == "Script created and executed." for item in chunks
    )


def test_reasoning_only_before_first_tool_retries_and_broadens_catalog(tmp_path):
    adapter = FixtureAdapter(
        [
            [
                ModelStreamEvent(
                    tool_calls=(
                        ModelToolCallDelta(
                            index=0,
                            call_id="dispatch-inspect",
                            name=ACTION_DISPATCH_PROVIDER_NAME,
                            arguments='{"profile":"inspect"}',
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            [
                ModelStreamEvent(reasoning_content="I need to use a tool."),
                ModelStreamEvent(finish_reason="stop"),
            ],
            [
                ModelStreamEvent(
                    tool_calls=(
                        ModelToolCallDelta(
                            index=0,
                            call_id="list-after-retry",
                            name="workspace__list_files",
                            arguments='{"path":"","limit":5}',
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            [
                ModelStreamEvent(content="Listing completed."),
                ModelStreamEvent(finish_reason="stop"),
            ],
        ]
    )
    native, _settings = service(tmp_path, adapter)
    generation = native.start_generation("session", "generation")

    chunks = list(
        native.stream(
            generation,
            [{"role": "user", "content": "Check the available files"}],
        )
    )

    retry_catalog = {
        item["function"]["name"] for item in adapter.requests[2][1].tools
    }
    assert "script__save_python" in retry_catalog
    retries = [
        item["content"]["payload"]
        for item in chunks
        if item.get("type") == "activity"
        and item["content"]["event"] == "model.retry_scheduled"
    ]
    assert retries == [
        {
            "phase": "action",
            "reason_code": "model_reasoning_only",
            "attempt": 1,
            "from_profile": "inspect",
            "profile": "automate",
            "tool_catalog_size": len(retry_catalog),
        }
    ]
    assert any(item.get("content") == "Listing completed." for item in chunks)


def test_recoverable_tool_failure_is_returned_to_model_for_retry(tmp_path):
    adapter = FixtureAdapter(
        [
            [
                ModelStreamEvent(
                    tool_calls=(
                        ModelToolCallDelta(
                            index=0,
                            call_id="dispatch",
                            name=ACTION_DISPATCH_PROVIDER_NAME,
                            arguments='{"profile":"inspect"}',
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            [
                ModelStreamEvent(
                    tool_calls=(
                        ModelToolCallDelta(
                            index=0,
                            call_id="bad-list",
                            name="workspace__list_files",
                            arguments='{"path":"workspace","limit":5}',
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            [
                ModelStreamEvent(
                    tool_calls=(
                        ModelToolCallDelta(
                            index=0,
                            call_id="good-list",
                            name="workspace__list_files",
                            arguments='{"path":"","limit":5}',
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            [
                ModelStreamEvent(content="Listing completed."),
                ModelStreamEvent(finish_reason="stop"),
            ],
        ]
    )
    native, _settings = service(tmp_path, adapter)
    generation = native.start_generation("session", "generation")

    chunks = list(
        native.stream(
            generation,
            [{"role": "user", "content": "List the workspace"}],
        )
    )

    retry_messages = adapter.requests[2][1].messages
    assert any(
        message.get("role") == "tool" and '"status": "failed"' in message["content"]
        for message in retry_messages
    )
    assert not any(item.get("type") == "execution_denied" for item in chunks)
    assert any(item.get("content") == "Listing completed." for item in chunks)


def test_reasoning_only_after_tool_gets_action_continuation_retry(tmp_path):
    adapter = FixtureAdapter(
        [
            [
                ModelStreamEvent(
                    tool_calls=(
                        ModelToolCallDelta(
                            index=0,
                            call_id="dispatch",
                            name=ACTION_DISPATCH_PROVIDER_NAME,
                            arguments='{"profile":"inspect"}',
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            [
                ModelStreamEvent(
                    tool_calls=(
                        ModelToolCallDelta(
                            index=0,
                            call_id="list",
                            name="workspace__list_files",
                            arguments='{"path":"","limit":5}',
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            [
                ModelStreamEvent(reasoning_content="I completed the inspection."),
                ModelStreamEvent(finish_reason="stop"),
            ],
            [
                ModelStreamEvent(content="The workspace contains several files."),
                ModelStreamEvent(finish_reason="stop"),
            ],
        ]
    )
    native, _settings = service(tmp_path, adapter)
    generation = native.start_generation("session", "generation")

    chunks = list(
        native.stream(
            generation,
            [{"role": "user", "content": "List the workspace"}],
        )
    )

    assert adapter.requests[2][1].tools
    assert adapter.requests[3][1].tools
    assert adapter.requests[3][1].messages[0]["role"] == "system"
    assert "previous tool completed only one step" in adapter.requests[3][1].messages[0]["content"]
    assert any(
        item.get("content") == "The workspace contains several files."
        for item in chunks
    )


def test_repeated_reasoning_only_uses_verified_tool_result_fallback(tmp_path):
    adapter = FixtureAdapter(
        [
            [
                ModelStreamEvent(
                    tool_calls=(
                        ModelToolCallDelta(
                            index=0,
                            call_id="dispatch",
                            name=ACTION_DISPATCH_PROVIDER_NAME,
                            arguments='{"profile":"inspect"}',
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            [
                ModelStreamEvent(
                    tool_calls=(
                        ModelToolCallDelta(
                            index=0,
                            call_id="list",
                            name="workspace__list_files",
                            arguments='{"path":"","limit":5}',
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            [
                ModelStreamEvent(reasoning_content="Primo tentativo."),
                ModelStreamEvent(finish_reason="stop"),
            ],
            [
                ModelStreamEvent(reasoning_content="Secondo tentativo."),
                ModelStreamEvent(finish_reason="stop"),
            ],
            [
                ModelStreamEvent(reasoning_content="Terzo tentativo."),
                ModelStreamEvent(finish_reason="stop"),
            ],
        ]
    )
    native, _settings = service(tmp_path, adapter)
    generation = native.start_generation("session", "generation")

    chunks = list(
        native.stream(
            generation,
            [{"role": "user", "content": "List the workspace"}],
        )
    )

    answer = "".join(
        item.get("content", "")
        for item in chunks
        if item.get("type") == "message"
    )
    assert "runtime-verified result" in answer
    assert '"tool": "workspace.list_files"' in answer
    completed = [
        item["content"]["payload"]
        for item in chunks
        if item.get("type") == "activity"
        and item["content"]["event"] == "run.completed"
    ]
    assert completed[-1]["deterministic_tool_fallback"] is True


def test_structured_workspace_action_uses_executor_then_model(tmp_path):
    adapter = FixtureAdapter([[ModelStreamEvent(content="The file contains a note."), ModelStreamEvent(finish_reason="stop")]])
    native, settings = service(tmp_path, adapter)
    (settings.workspace / "note.txt").write_text("local note", encoding="utf-8")
    invocation = ToolInvocation.create("workspace.read_text", {"path": "note.txt"})
    generation = native.start_generation(
        "session",
        "generation",
        routing_signal=RoutingSignal(tool_name="workspace.read_text"),
        tool_invocation=invocation,
    )

    chunks = list(native.stream(generation, [{"role": "user", "type": "message", "content": "Read the note"}]))

    request_messages = adapter.requests[0][1].messages
    assert any(message.get("role") == "tool" and "local note" in message["content"] for message in request_messages)
    assert any(chunk.get("content") == "The file contains a note." for chunk in chunks)


def test_cloud_workspace_output_is_not_sent_without_egress_consent(tmp_path):
    adapter = FixtureAdapter([])
    native, settings = service(tmp_path, adapter, provider="cloud", egress="deny")
    (settings.workspace / "note.txt").write_text("private data", encoding="utf-8")
    generation = native.start_generation(
        "session",
        "generation",
        routing_signal=RoutingSignal(tool_name="workspace.read_text"),
        tool_invocation=ToolInvocation.create("workspace.read_text", {"path": "note.txt"}),
    )

    chunks = list(native.stream(generation, [{"role": "user", "type": "message", "content": "Read"}]))

    answer = "".join(chunk.get("content", "") for chunk in chunks if chunk.get("type") == "message")
    assert "was not sent to the cloud model" in answer
    assert "private data" in answer
    assert adapter.requests == []


def test_model_tool_call_loop_uses_only_structured_selected_tool(tmp_path):
    adapter = FixtureAdapter(
        [
            [
                ModelStreamEvent(
                    tool_calls=(
                        ModelToolCallDelta(
                            index=0,
                            call_id="call-1",
                            name="workspace__read_text",
                            arguments='{"path":"note.txt"}',
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            [ModelStreamEvent(content="Result read."), ModelStreamEvent(finish_reason="stop")],
        ]
    )
    native, settings = service(tmp_path, adapter)
    (settings.workspace / "note.txt").write_text("content", encoding="utf-8")
    generation = native.start_generation(
        "session",
        "generation",
        routing_signal=RoutingSignal(tool_name="workspace.read_text"),
    )

    chunks = list(native.stream(generation, [{"role": "user", "content": "Read the note"}]))

    assert len(adapter.requests[0][1].tools) == 1
    assert any(
        message.get("role") == "tool" and "content" in message["content"]
        for message in adapter.requests[1][1].messages
    )
    assert any(chunk.get("content") == "Result read." for chunk in chunks)


def test_native_always_ask_waits_for_one_shot_approval(tmp_path):
    adapter = FixtureAdapter([[ModelStreamEvent(content="Approved."), ModelStreamEvent(finish_reason="stop")]])
    native, settings = service(tmp_path, adapter, policy="always_ask")
    (settings.workspace / "note.txt").write_text("content", encoding="utf-8")
    generation = native.start_generation(
        "session",
        "generation",
        routing_signal=RoutingSignal(tool_name="workspace.read_text"),
        tool_invocation=ToolInvocation.create("workspace.read_text", {"path": "note.txt"}),
    )
    stream = native.stream(generation, [{"role": "user", "content": "Read"}])
    approval = None
    emitted = []
    while approval is None:
        item = next(stream)
        emitted.append(item)
        if item.get("type") == "approval_required":
            approval = item

    native.approval_broker.resolve(
        approval["approval_id"],
        approve=True,
        session_id="session",
        generation_id="generation",
    )
    emitted.extend(stream)

    assert any(item.get("content") == "Approved." for item in emitted)
    assert adapter.requests


def test_native_deny_all_never_calls_tool_or_model(tmp_path):
    adapter = FixtureAdapter([])
    native, settings = service(tmp_path, adapter, policy="deny_all")
    (settings.workspace / "note.txt").write_text("content", encoding="utf-8")
    generation = native.start_generation(
        "session",
        "generation",
        routing_signal=RoutingSignal(tool_name="workspace.read_text"),
        tool_invocation=ToolInvocation.create("workspace.read_text", {"path": "note.txt"}),
    )

    chunks = list(native.stream(generation, [{"role": "user", "content": "Read"}]))

    denied = next(item for item in chunks if item.get("type") == "execution_denied")
    assert denied["content"]["reason_code"] == "policy_denies_execution"
    assert adapter.requests == []


def test_reset_cancels_native_generation_and_clears_short_session(tmp_path):
    entered = threading.Event()

    class BlockingAdapter:
        def stream(self, context, request, cancelled):
            entered.set()
            while not cancelled.wait(0.01):
                pass
            return
            yield

    native, _settings = service(tmp_path, BlockingAdapter())
    generation = native.start_generation("session", "generation")
    errors = []
    worker = threading.Thread(
        target=lambda: _consume(native, generation, errors),
        daemon=True,
    )
    worker.start()
    assert entered.wait(1)

    result = native.reset()
    worker.join(1)

    assert result["previous_session_id"] == "session"
    assert result["invalidated_generation_id"] == "generation"
    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ModelGenerationCancelled)


def _consume(native, generation, errors):
    try:
        list(native.stream(generation, [{"role": "user", "content": "wait"}]))
    except Exception as exc:
        errors.append(exc)
