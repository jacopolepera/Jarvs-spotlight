"""Native run coordinator and bounded short-term conversation lifecycle."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from .action_routing import (
    ACTION_DISPATCH_NAME,
    ACTION_DISPATCH_PROVIDER_NAME,
    action_dispatch_tool,
    conservative_action_hint,
    definitions_for_profile,
    parse_action_selection,
)
from .approvals import ApprovalBroker
from .egress import DataClass, DataEgressPolicy
from .events import EventSink, EventType, RunEvent, RunEventStream
from .executor import ToolExecutor
from .model_adapter import (
    ModelAdapter,
    ModelGenerationCancelled,
    ModelRequest,
    ModelStreamEvent,
    ModelToolCallDelta,
    ProviderContext,
    provider_context_from_model,
)
from .routing import ExecutionLane, RoutingSignal, select_execution_lane
from .tooling import (
    ToolContext,
    ToolInvocation,
    ToolRegistry,
    ToolResult,
    ToolStatus,
    ToolValidationError,
)
from core.daemon.session_service import (
    Generation,
    SessionBusyError,
    StaleGenerationError,
    StaleSessionError,
)


MAX_SESSION_MESSAGES = 24
MAX_SESSION_BYTES = 128 * 1024
MAX_TOOL_ITERATIONS = 8
MAX_TOOL_ARGUMENT_TEXT_BYTES = 256 * 1024
DIRECT_MODEL_MAX_TOKENS = 2_048
ACTION_MODEL_MAX_TOKENS = 2_048
FINAL_MODEL_MAX_TOKENS = 1_024
DETERMINISTIC_TERMINAL_TOOLS = frozenset({"script.create_and_run_python"})
MAX_DETERMINISTIC_SUMMARY_CHARS = 16_000
RECOVERABLE_TOOL_REJECTIONS = frozenset(
    {
        "invalid_tool_arguments",
        "invalid_command_arguments",
        "python_script_extension_required",
        "python_script_not_found",
        "python_script_not_utf8",
        "python_syntax_invalid",
        "typed_script_tool_required",
        "workspace_parent_missing",
        "workspace_target_exists",
    }
)


class CoordinatorError(RuntimeError):
    code = "coordinator_error"


class IncompleteModelResponseError(CoordinatorError):
    code = "incomplete_model_response"


class InvalidModelToolCallError(CoordinatorError):
    code = "invalid_model_tool_call"


class SpecialistUnavailableError(CoordinatorError):
    code = "specialist_unavailable"


class SpecialistExecutionError(CoordinatorError):
    code = "specialist_execution_failed"


class ShortTermTranscript:
    """In-memory, bounded transcript; it never writes conversation history."""

    def __init__(self) -> None:
        self._messages: list[dict[str, Any]] = []
        self._contains_workspace_content = False
        self._lock = threading.RLock()

    def snapshot(self, *, allow_workspace: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            # Summaries and later answers can derive from earlier tool output.
            # Keep this conservative marker across commits and history trimming
            # until reset; stripping only tool messages would still leak them.
            if not allow_workspace and self._contains_workspace_content:
                return []
            return copy.deepcopy(self._messages)

    def commit(self, messages: Sequence[Mapping[str, Any]]) -> None:
        with self._lock:
            if any(message.get("role") == "tool" for message in messages):
                self._contains_workspace_content = True
            self._messages.extend(copy.deepcopy([dict(item) for item in messages]))
            self._bound()

    def clear(self) -> None:
        with self._lock:
            self._messages.clear()
            self._contains_workspace_content = False

    def _bound(self) -> None:
        if len(self._messages) > MAX_SESSION_MESSAGES:
            self._messages = self._messages[-MAX_SESSION_MESSAGES:]
        while self._messages and self._encoded_size() > MAX_SESSION_BYTES:
            self._messages.pop(0)

    def _encoded_size(self) -> int:
        return len(
            json.dumps(self._messages, ensure_ascii=False, separators=(",", ":"))
            .encode("utf-8")
        )


@dataclass
class _AccumulatedToolCall:
    index: int
    call_id: str = ""
    name: str = ""
    arguments: str = ""

    def apply(self, delta: ModelToolCallDelta) -> None:
        if delta.call_id:
            self.call_id = delta.call_id
        if delta.name:
            self.name += delta.name
        self.arguments += delta.arguments
        if len(self.arguments.encode("utf-8")) > MAX_TOOL_ARGUMENT_TEXT_BYTES:
            raise InvalidModelToolCallError("Tool-call arguments exceed the size limit.")


def _activity(event: RunEvent) -> dict[str, Any]:
    return {
        "role": "jarvs",
        "type": "activity",
        "content": {
            "event": event.type,
            "sequence": event.sequence,
            "payload": event.payload_dict(),
        },
    }


def _provider_tool_name(name: str) -> str:
    return name.replace(".", "__")


def _openai_tool(definition) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": _provider_tool_name(definition.name),
            "description": definition.description,
            "parameters": definition.public_metadata()["input_schema"],
        },
    }


def _incoming_to_openai(messages: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role not in {"user", "assistant"}:
            role = "user"
        kind = message.get("type", "message")
        content = message.get("content")
        if kind == "image" and isinstance(content, str):
            image_format = str(message.get("format") or "base64.png")
            mime = image_format.split(".", 1)[-1]
            if mime == "jpg":
                mime = "jpeg"
            normalized.append(
                {
                    "role": role,
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/{mime};base64,{content}"},
                        }
                    ],
                }
            )
        elif isinstance(content, str) and content:
            normalized.append({"role": role, "content": content})
    return normalized


def _history_safe_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    safe: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            safe.append({"role": message.get("role", "user"), "content": "[Image attached]"})
        else:
            safe.append(dict(message))
    return safe


class RunCoordinator:
    """Coordinate native model streaming and policy-gated tool execution."""

    def __init__(
        self,
        *,
        settings_manager: Any,
        model_adapter: ModelAdapter,
        registry: ToolRegistry,
        executor: ToolExecutor,
        egress_policy: DataEgressPolicy,
        system_prompt_provider: Callable[[], str],
        memory_context_provider: Callable[[str], str] | None = None,
        event_sinks: Sequence[EventSink] = (),
        specialist_request_stager: Callable[
            [str, Sequence[Mapping[str, Any]], Generation], ToolInvocation
        ]
        | None = None,
        specialist_executor_provider: Callable[[], ToolExecutor] | None = None,
        specialist_request_discarder: Callable[[str], None] | None = None,
    ) -> None:
        self.settings_manager = settings_manager
        self.model_adapter = model_adapter
        self.registry = registry
        self.executor = executor
        self.egress_policy = egress_policy
        self.system_prompt_provider = system_prompt_provider
        self.memory_context_provider = memory_context_provider
        self.event_sinks = tuple(event_sinks)
        self.specialist_request_stager = specialist_request_stager
        self.specialist_executor_provider = specialist_executor_provider
        self.specialist_request_discarder = specialist_request_discarder

    def stream(
        self,
        generation: Generation,
        incoming: Sequence[Mapping[str, Any]],
        transcript: ShortTermTranscript,
    ) -> Iterator[dict[str, Any]]:
        run_id = uuid.uuid4().hex
        events = RunEventStream(run_id, sinks=self.event_sinks)
        terminal_types = {
            EventType.RUN_COMPLETED.value,
            EventType.RUN_CANCELLED.value,
            EventType.RUN_FAILED.value,
        }
        # Only runtime-confirmed tool results survive an interrupted answer.
        # Keep each call/result pair closed, even when Stop interrupts a batch.
        completed_tool_messages: list[dict[str, Any]] = []
        try:
            yield from self._stream_run(
                generation, incoming, transcript, events, completed_tool_messages
            )
        except ModelGenerationCancelled:
            if completed_tool_messages:
                transcript.commit(
                    _history_safe_messages(_incoming_to_openai(incoming))
                    + completed_tool_messages
                )
            if events.last_type not in terminal_types:
                yield _activity(
                    events.emit(
                        EventType.RUN_CANCELLED,
                        {"reason_code": "generation_cancelled"},
                    )
                )
            raise
        except Exception as exc:
            if events.last_type not in terminal_types:
                yield _activity(
                    events.emit(
                        EventType.RUN_FAILED,
                        {
                            "reason_code": getattr(exc, "code", "generation_failed"),
                            "error_type": type(exc).__name__,
                        },
                    )
                )
            raise

    def _stream_run(
        self,
        generation: Generation,
        incoming: Sequence[Mapping[str, Any]],
        transcript: ShortTermTranscript,
        events: RunEventStream,
        completed_tool_messages: list[dict[str, Any]],
    ) -> Iterator[dict[str, Any]]:
        started = events.emit(
            EventType.RUN_STARTED,
            {"session_id": generation.session_id, "generation_id": generation.generation_id},
        )
        yield _activity(started)
        decision = select_execution_lane(generation.routing_signal)
        yield _activity(
            events.emit(
                EventType.ROUTE_SELECTED,
                {"lane": decision.lane.value, "reason_code": decision.reason_code},
            )
        )
        if decision.lane is ExecutionLane.SPECIALIST:
            yield from self._stream_specialist(
                generation,
                incoming,
                transcript,
                events,
            )
            return

        provider = provider_context_from_model(self.settings_manager.get_active_model())
        prompt_text = "\n".join(
            str(item.get("content", ""))
            for item in incoming
            if isinstance(item.get("content"), str)
        )
        system_prompt = self.system_prompt_provider().strip()
        memory_allowed = self.egress_policy.evaluate(
            provider.destination,
            DataClass.PERSONAL_MEMORY,
        ).allowed
        if memory_allowed and self.memory_context_provider is not None:
            memory_context = self.memory_context_provider(prompt_text).strip()
            if memory_context:
                system_prompt = f"{system_prompt}\n\n{memory_context}"
        current_messages = transcript.snapshot(
            allow_workspace=self.egress_policy.evaluate(
                provider.destination, DataClass.WORKSPACE_CONTENT
            ).allowed
        ) + _incoming_to_openai(incoming)
        committed: list[dict[str, Any]] = _history_safe_messages(_incoming_to_openai(incoming))
        tools, reverse_names = self._tools_for(generation)
        catalog_hint = None
        if (
            generation.tool_invocation is None
            and generation.routing_signal.tool_name is None
        ):
            catalog_hint = conservative_action_hint(prompt_text, self.registry)
            if catalog_hint is not None:
                tools, reverse_names = self._tools_for_definitions(
                    catalog_hint.definitions
                )
                yield _activity(
                    events.emit(
                        EventType.ROUTE_SELECTED,
                        {
                            "lane": ExecutionLane.ACTION.value,
                            "reason_code": catalog_hint.reason_code,
                            "profile": catalog_hint.profile,
                            "tool_catalog_size": len(tools),
                            "catalog_hint": True,
                        },
                    )
                )

        if generation.tool_invocation is not None:
            result = yield from self._execute_tool(
                generation,
                generation.tool_invocation,
                events,
            )
            if result.status is ToolStatus.CANCELLED:
                raise ModelGenerationCancelled("The generation was cancelled.")
            if result.status is not ToolStatus.COMPLETED:
                yield {
                    "role": "jarvs",
                    "type": "execution_denied",
                    "content": {
                        "reason_code": result.reason_code or result.status.value,
                        "tool_name": result.tool_name,
                    },
                }
                yield _activity(events.emit(EventType.RUN_FAILED, {"reason_code": result.reason_code or result.status.value}))
                return

            if not self._egress_allowed(provider, result):
                text = self._local_only_result(provider, result)
                yield {"role": "assistant", "type": "message", "content": text}
                committed.append(self._local_only_history_message())
                transcript.commit(committed)
                yield _activity(events.emit(EventType.RUN_COMPLETED, {"local_only": True}))
                return
            call_id = f"call_{generation.tool_invocation.invocation_id}"
            assistant_call = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": _provider_tool_name(result.tool_name),
                            "arguments": json.dumps(generation.tool_invocation.arguments_dict()),
                        },
                    }
                ],
            }
            tool_message = {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(result.output, ensure_ascii=False),
            }
            current_messages.extend([assistant_call, tool_message])
            committed.extend([assistant_call, tool_message])
            completed_tool_messages.extend([assistant_call, tool_message])
            # A structured invocation is already planned and executed.  The
            # follow-up model call summarizes it but cannot silently expand the action.
            tools = []

        action_dispatched = catalog_hint is not None
        selected_profile: str | None = (
            catalog_hint.profile if catalog_hint is not None else None
        )
        tool_completed = False
        finalization_retried = False
        pretool_retry_phases: set[str] = set()
        runtime_system_suffix = ""
        completed_results: list[ToolResult] = []
        for iteration in range(MAX_TOOL_ITERATIONS + 1):
            if generation.cancelled.is_set():
                raise ModelGenerationCancelled("The generation was cancelled.")
            model_phase = self._model_phase(
                action_dispatched=action_dispatched,
                tool_completed=tool_completed,
                tools=tools,
            )
            catalog_names = sorted(reverse_names.values())
            catalog_hash = hashlib.sha256(
                "\n".join(catalog_names).encode("utf-8")
            ).hexdigest()[:16]
            yield _activity(
                events.emit(
                    EventType.MODEL_STARTED,
                    {
                        "model_id": provider.model_id,
                        "destination": provider.destination.value,
                        "tool_catalog_size": len(tools),
                        "tool_catalog_hash": catalog_hash,
                        "tool_names": catalog_names,
                        "profile": selected_profile or model_phase,
                        "phase": model_phase,
                        "iteration": iteration,
                    },
                )
            )
            phase_instruction = self._phase_instruction(model_phase, provider)
            runtime_instructions = "\n\n".join(
                item
                for item in (phase_instruction, runtime_system_suffix)
                if item
            )
            effective_system_prompt = (
                f"{system_prompt}\n\n{runtime_instructions}".strip()
                if runtime_instructions
                else system_prompt
            )
            model_messages = (
                ([{"role": "system", "content": effective_system_prompt}]
                if effective_system_prompt
                else [])
                + current_messages
            )
            temperature, max_tokens = self._model_limits(model_phase)
            model_started_at = time.perf_counter()
            text, reasoning, raw_calls, finish_reason = yield from self._stream_model(
                provider,
                ModelRequest(
                    messages=model_messages,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                ),
                generation,
            )
            model_duration_ms = (time.perf_counter() - model_started_at) * 1000
            if generation.cancelled.is_set():
                raise ModelGenerationCancelled("The generation was cancelled.")
            yield _activity(
                events.emit(
                    EventType.MODEL_COMPLETED,
                    {
                        "finish_reason": finish_reason or "unknown",
                        "has_answer": bool(text.strip()),
                        "tool_call_count": len(raw_calls),
                        "reasoning_only": bool(
                            reasoning.strip() and not text.strip() and not raw_calls
                        ),
                        "text_chars": len(text),
                        "reasoning_chars": len(reasoning),
                        "duration_ms": round(model_duration_ms, 3),
                        "profile": selected_profile or model_phase,
                        "phase": model_phase,
                    },
                )
            )

            if not raw_calls:
                if not text.strip():
                    if (
                        tool_completed
                        and tools
                        and "action_after_tool" not in pretool_retry_phases
                    ):
                        runtime_system_suffix = self._pretool_retry_instruction(
                            "action_after_tool"
                        )
                        pretool_retry_phases.add("action_after_tool")
                        yield _activity(
                            events.emit(
                                EventType.MODEL_RETRY_SCHEDULED,
                                {
                                    "phase": "action_after_tool",
                                    "reason_code": self._incomplete_reason(
                                        finish_reason,
                                        reasoning,
                                    ),
                                    "attempt": 1,
                                    "profile": selected_profile or "automate",
                                    "tool_catalog_size": len(tools),
                                },
                            )
                        )
                        continue
                    if tool_completed and not finalization_retried:
                        # Some local reasoning models finish a post-tool turn
                        # with reasoning_content only.  Retry once without tools
                        # and keep this runtime steering out of the transcript.
                        runtime_system_suffix = (
                            "Jarvs runtime: the requested action has completed. "
                            "Return the concise user-facing result now. Do not call "
                            "another tool and do not return reasoning-only output."
                        )
                        tools = []
                        reverse_names = {}
                        finalization_retried = True
                        yield _activity(
                            events.emit(
                                EventType.MODEL_RETRY_SCHEDULED,
                                {
                                    "phase": "finalize",
                                    "reason_code": self._incomplete_reason(
                                        finish_reason,
                                        reasoning,
                                    ),
                                    "attempt": 1,
                                    "profile": selected_profile or "finalize",
                                    "tool_catalog_size": 0,
                                },
                            )
                        )
                        continue
                    if tool_completed and completed_results:
                        fallback = self._deterministic_tool_result(completed_results)
                        yield {
                            "role": "assistant",
                            "type": "message",
                            "content": fallback,
                        }
                        committed.append({"role": "assistant", "content": fallback})
                        transcript.commit(committed)
                        yield _activity(
                            events.emit(
                                EventType.RUN_COMPLETED,
                                {
                                    "iterations": iteration + 1,
                                    "deterministic_tool_fallback": True,
                                },
                            )
                        )
                        return
                    retry_phase = "action" if action_dispatched else "dispatch"
                    if retry_phase not in pretool_retry_phases and tools:
                        previous_profile = selected_profile or "dispatch"
                        if action_dispatched and selected_profile == "inspect":
                            selected_profile = "automate"
                            definitions = definitions_for_profile(
                                self.registry.definitions(),
                                selected_profile,
                            )
                            tools, reverse_names = self._tools_for_definitions(definitions)
                        runtime_system_suffix = self._pretool_retry_instruction(
                            retry_phase,
                        )
                        pretool_retry_phases.add(retry_phase)
                        yield _activity(
                            events.emit(
                                EventType.MODEL_RETRY_SCHEDULED,
                                {
                                    "phase": retry_phase,
                                    "reason_code": self._incomplete_reason(
                                        finish_reason,
                                        reasoning,
                                    ),
                                    "attempt": 1,
                                    "from_profile": previous_profile,
                                    "profile": selected_profile or "dispatch",
                                    "tool_catalog_size": len(tools),
                                },
                            )
                        )
                        continue
                    reason_code = self._incomplete_reason(finish_reason, reasoning)
                    yield _activity(
                        events.emit(
                            EventType.RUN_FAILED,
                            {
                                "reason_code": reason_code,
                                "phase": retry_phase,
                                "profile": selected_profile or "dispatch",
                            },
                        )
                    )
                    raise IncompleteModelResponseError(
                        "The model did not produce a tool call or final response, even "
                        "after the recovery attempt. Try again or select a local model "
                        "with native tool calling."
                    )
                if text:
                    assistant_message = {"role": "assistant", "content": text}
                    current_messages.append(assistant_message)
                    committed.append(assistant_message)
                transcript.commit(committed)
                yield _activity(events.emit(EventType.RUN_COMPLETED, {"iterations": iteration + 1}))
                return

            if iteration >= MAX_TOOL_ITERATIONS:
                yield _activity(
                    events.emit(
                        EventType.RUN_FAILED,
                        {"reason_code": "tool_iteration_limit"},
                    )
                )
                raise CoordinatorError(
                    "The tool flow exceeded the iteration limit."
                )

            # Retry steering belongs to one model turn only and is never added
            # to the conversation transcript.
            runtime_system_suffix = ""

            dispatch_calls = [
                raw_call
                for raw_call in raw_calls
                if reverse_names.get(raw_call.name) == ACTION_DISPATCH_NAME
            ]
            if dispatch_calls:
                if (
                    action_dispatched
                    or generation.routing_signal.tool_name is not None
                    or len(dispatch_calls) != 1
                    or len(raw_calls) != 1
                ):
                    raise InvalidModelToolCallError(
                        "The model returned an invalid action dispatch."
                    )
                raw_call = dispatch_calls[0]
                try:
                    selection = parse_action_selection(
                        raw_call.arguments,
                        self.registry,
                    )
                except ToolValidationError as exc:
                    raise InvalidModelToolCallError(str(exc)) from exc
                # The dispatcher belongs to Jarvs' control plane, not to the
                # conversation transcript.  Re-run the same user request with
                # only the selected catalog; this avoids teaching the provider
                # that routing itself was an executed user-facing tool.
                tools, reverse_names = self._tools_for_definitions(
                    selection.definitions
                )
                action_dispatched = True
                selected_profile = selection.profile
                yield _activity(
                    events.emit(
                        EventType.ROUTE_SELECTED,
                        {
                            "lane": ExecutionLane.ACTION.value,
                            "reason_code": "model_requested_native_action",
                            "profile": selection.profile,
                            "tool_catalog_size": len(tools),
                        },
                    )
                )
                continue

            assistant_calls = []
            tool_messages = []
            terminal_result: ToolResult | None = None
            for raw_call in raw_calls:
                canonical_name = reverse_names.get(raw_call.name)
                if canonical_name is None:
                    raise InvalidModelToolCallError("The model requested an unavailable tool.")
                try:
                    arguments = json.loads(raw_call.arguments or "{}")
                except json.JSONDecodeError as exc:
                    raise InvalidModelToolCallError("The model returned invalid tool arguments.") from exc
                if not isinstance(arguments, dict):
                    raise InvalidModelToolCallError("Tool arguments must be a JSON object.")
                invocation = ToolInvocation.create(
                    canonical_name,
                    arguments,
                    invocation_id=raw_call.call_id or uuid.uuid4().hex,
                )
                result = yield from self._execute_tool(generation, invocation, events)
                if result.status is ToolStatus.CANCELLED:
                    raise ModelGenerationCancelled("The generation was cancelled.")
                call_id = raw_call.call_id or f"call_{invocation.invocation_id}"
                recoverable_rejection = (
                    result.status is ToolStatus.DENIED
                    and result.reason_code in RECOVERABLE_TOOL_REJECTIONS
                )
                if result.status is ToolStatus.FAILED or recoverable_rejection:
                    assistant_calls.append(
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": raw_call.name,
                                "arguments": raw_call.arguments or "{}",
                            },
                        }
                    )
                    tool_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": json.dumps(
                                {
                                    "status": (
                                        "rejected" if recoverable_rejection else "failed"
                                    ),
                                    "reason_code": result.reason_code or "tool_failed",
                                    "error_type": result.error,
                                    "instruction": self._tool_retry_instruction(
                                        result.reason_code
                                    ),
                                },
                                ensure_ascii=False,
                            ),
                        }
                    )
                    continue
                if result.status is not ToolStatus.COMPLETED:
                    yield {
                        "role": "jarvs",
                        "type": "execution_denied",
                        "content": {
                            "reason_code": result.reason_code or result.status.value,
                            "tool_name": result.tool_name,
                        },
                    }
                    yield _activity(events.emit(EventType.RUN_FAILED, {"reason_code": result.reason_code or result.status.value}))
                    return
                if not self._egress_allowed(provider, result):
                    text = self._local_only_result(provider, result)
                    yield {"role": "assistant", "type": "message", "content": text}
                    committed.append(self._local_only_history_message())
                    transcript.commit(committed)
                    yield _activity(events.emit(EventType.RUN_COMPLETED, {"local_only": True}))
                    return
                tool_completed = True
                completed_results.append(result)
                if result.tool_name in DETERMINISTIC_TERMINAL_TOOLS:
                    terminal_result = result
                assistant_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": raw_call.name,
                            "arguments": raw_call.arguments or "{}",
                        },
                    }
                )
                tool_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(result.output, ensure_ascii=False),
                    }
                )
                completed_tool_messages.extend(
                    [
                        {"role": "assistant", "content": None,
                         "tool_calls": [assistant_calls[-1]]},
                        tool_messages[-1],
                    ]
                )
            assistant_message = {
                "role": "assistant",
                "content": text or None,
                "tool_calls": assistant_calls,
            }
            current_messages.append(assistant_message)
            current_messages.extend(tool_messages)
            committed.append(assistant_message)
            committed.extend(tool_messages)
            if terminal_result is not None:
                summary = self._deterministic_terminal_result(terminal_result)
                yield {
                    "role": "assistant",
                    "type": "message",
                    "content": summary,
                }
                committed.append({"role": "assistant", "content": summary})
                transcript.commit(committed)
                yield _activity(
                    events.emit(
                        EventType.RUN_COMPLETED,
                        {
                            "iterations": iteration + 1,
                            "deterministic_runtime_summary": True,
                            "tool_name": terminal_result.tool_name,
                        },
                    )
                )
                return

    def _stream_specialist(
        self,
        generation: Generation,
        incoming: Sequence[Mapping[str, Any]],
        transcript: ShortTermTranscript,
        events: RunEventStream,
    ) -> Iterator[dict[str, Any]]:
        """Run one explicitly selected specialist without a native model call.

        The full request is staged out-of-band by the runtime. The registered
        invocation therefore contains only bounded correlation metadata and
        can safely pass through policy, approval, and durable audit.
        """

        capability = generation.routing_signal.specialist_capability
        if (
            capability is None
            or self.specialist_request_stager is None
            or self.specialist_executor_provider is None
        ):
            raise SpecialistUnavailableError(
                "The requested specialist capability is not configured."
            )

        # Give Hermes the bounded short-term context Jarvs already owns while
        # preserving room for every message in the current turn.  Hermes does
        # not become the canonical transcript owner.
        history = transcript.snapshot()
        current = [dict(message) for message in incoming]
        if len(current) > MAX_SESSION_MESSAGES:
            raise SpecialistUnavailableError(
                "The specialist turn contains too many messages."
            )
        history_room = MAX_SESSION_MESSAGES - len(current)
        selected_history = history[-history_room:] if history_room else []
        specialist_messages = selected_history + current
        invocation = self.specialist_request_stager(
            capability,
            specialist_messages,
            generation,
        )
        stream_state: dict[str, Any] = {"text_chunks": []}
        try:
            result = yield from self._execute_tool(
                generation,
                invocation,
                events,
                executor=self.specialist_executor_provider(),
                specialist_stream_state=stream_state,
            )
        finally:
            if self.specialist_request_discarder is not None:
                self.specialist_request_discarder(invocation.invocation_id)

        if result.status is ToolStatus.DENIED:
            reason_code = result.reason_code or result.status.value
            yield {
                "role": "jarvs",
                "type": "execution_denied",
                "content": {
                    "reason_code": reason_code,
                    "tool_name": result.tool_name,
                },
            }
            yield _activity(
                events.emit(
                    EventType.RUN_FAILED,
                    {
                        "reason_code": reason_code,
                        "specialist": "hermes",
                        "capability": capability,
                    },
                )
            )
            return
        if result.status is ToolStatus.CANCELLED:
            yield _activity(
                events.emit(
                    EventType.RUN_CANCELLED,
                    {
                        "reason_code": result.reason_code or "specialist_cancelled",
                        "specialist": "hermes",
                        "capability": capability,
                    },
                )
            )
            raise ModelGenerationCancelled("The specialist run was cancelled.")
        if result.status is ToolStatus.FAILED:
            yield _activity(
                events.emit(
                    EventType.RUN_FAILED,
                    {
                        "reason_code": result.reason_code or "specialist_failed",
                        "specialist": "hermes",
                        "capability": capability,
                    },
                )
            )
            raise SpecialistExecutionError("The Hermes specialist run failed.")
        if result.status is not ToolStatus.COMPLETED:
            raise SpecialistExecutionError(
                "The Hermes specialist returned an invalid execution status."
            )

        output = result.output if isinstance(result.output, Mapping) else {}
        final_text = str(output.get("text") or output.get("content") or "")
        committed = _history_safe_messages(_incoming_to_openai(incoming))
        if final_text:
            prospective = committed + [{"role": "assistant", "content": final_text}]
            if (
                len(
                    json.dumps(
                        prospective,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                > MAX_SESSION_BYTES
            ):
                raise SpecialistExecutionError(
                    "The Hermes turn exceeds Jarvs' short-term history budget."
                )
            yield {
                "role": "assistant",
                "type": "message",
                "content": final_text,
            }

        # Only the final normalized answer is displayed and committed during
        # the pilot. Hermes' contract can replace earlier delta segments via
        # message.interim, so forwarding raw deltas would let UI and history
        # disagree. Full streaming can return with explicit segment handling.
        if final_text:
            committed.append({"role": "assistant", "content": final_text})
        transcript.commit(committed)
        yield _activity(
            events.emit(
                EventType.RUN_COMPLETED,
                {
                    "specialist": "hermes",
                    "capability": capability,
                    "finish_reason": str(output.get("finish_reason") or "completed"),
                },
            )
        )

    @staticmethod
    def _model_phase(
        *,
        action_dispatched: bool,
        tool_completed: bool,
        tools: Sequence[Mapping[str, Any]],
    ) -> str:
        if not tools:
            return "finalize"
        if not action_dispatched:
            return "dispatch"
        return "action_after_tool" if tool_completed else "action"

    @staticmethod
    def _model_limits(phase: str) -> tuple[float, int]:
        if phase == "dispatch":
            # This turn may still be a normal conversational answer, so keep a
            # useful answer budget while making tool selection deterministic.
            return 0.0, DIRECT_MODEL_MAX_TOKENS
        if phase.startswith("action"):
            return 0.0, ACTION_MODEL_MAX_TOKENS
        return 0.0, FINAL_MODEL_MAX_TOKENS

    @staticmethod
    def _phase_instruction(phase: str, provider: ProviderContext) -> str:
        if provider.destination.value != "local" or phase == "dispatch":
            return ""
        if phase.startswith("action"):
            return (
                "/no_think\nJarvs tool turn: use the available structured tools to "
                "advance the request. Emit a tool call or a concise final answer; do "
                "not return reasoning-only output."
            )
        return (
            "/no_think\nJarvs finalization turn: return only the concise user-facing "
            "result. Do not return reasoning-only output."
        )

    @staticmethod
    def _incomplete_reason(finish_reason: str | None, reasoning: str) -> str:
        if finish_reason == "length":
            return "model_output_limit"
        if reasoning.strip():
            return "model_reasoning_only"
        return "model_empty_response"

    @staticmethod
    def _pretool_retry_instruction(phase: str) -> str:
        if phase == "dispatch":
            return (
                "Jarvs control-plane recovery: do not return reasoning-only output. "
                "If the user requested a local effect, call jarvs__request_action now; "
                "otherwise return the final user-facing answer."
            )
        if phase == "action_after_tool":
            return (
                "Jarvs action continuation: the previous tool completed only one step. "
                "Re-read the user's original request. If any requested step remains, call "
                "the next available structured tool now; for create-and-run Python tasks "
                "this means script.run_python after script.save_python. If every requested "
                "step is complete, return the concise final result. Do not return "
                "reasoning-only output."
            )
        return (
            "Jarvs action recovery: do not return reasoning-only output. Choose and call "
            "exactly one available structured tool that advances the user's request. For "
            "Python source use script.save_python, never workspace.write_text."
        )

    @staticmethod
    def _tool_retry_instruction(reason_code: str | None) -> str:
        if reason_code == "typed_script_tool_required":
            return (
                "This writer cannot create executable source. Use script.save_python "
                "with the same path and source code."
            )
        return "Correct the arguments or choose another available tool."

    def _stream_model(
        self,
        provider: ProviderContext,
        request: ModelRequest,
        generation: Generation,
    ) -> Iterator[dict[str, Any]]:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: dict[int, _AccumulatedToolCall] = {}
        finish_reason: str | None = None
        for delta in self.model_adapter.stream(provider, request, generation.cancelled):
            if generation.cancelled.is_set():
                raise ModelGenerationCancelled("The generation was cancelled.")
            if delta.content:
                text_parts.append(delta.content)
                yield {"role": "assistant", "type": "message", "content": delta.content}
            if delta.reasoning_content:
                reasoning_parts.append(delta.reasoning_content)
            for call_delta in delta.tool_calls:
                calls.setdefault(
                    call_delta.index,
                    _AccumulatedToolCall(index=call_delta.index),
                ).apply(call_delta)
            if delta.finish_reason is not None:
                finish_reason = delta.finish_reason
        return (
            "".join(text_parts),
            "".join(reasoning_parts),
            [calls[index] for index in sorted(calls)],
            finish_reason,
        )

    def _tools_for(
        self,
        generation: Generation,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        if generation.routing_signal.tool_name is None:
            return (
                [action_dispatch_tool()],
                {ACTION_DISPATCH_PROVIDER_NAME: ACTION_DISPATCH_NAME},
            )
        definition = self.registry.resolve(generation.routing_signal.tool_name)
        return self._tools_for_definitions((definition,))

    @staticmethod
    def _tools_for_definitions(
        definitions: Sequence[Any],
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        tools: list[dict[str, Any]] = []
        reverse: dict[str, str] = {}
        for definition in definitions:
            provider_name = _provider_tool_name(definition.name)
            tools.append(_openai_tool(definition))
            reverse[provider_name] = definition.name
        return tools, reverse

    def _execute_tool(
        self,
        generation: Generation,
        invocation: ToolInvocation,
        events: RunEventStream,
        *,
        executor: ToolExecutor | None = None,
        specialist_stream_state: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        bridge: queue.Queue[tuple[str, Any]] = queue.Queue()

        def event_sink(event: RunEvent) -> None:
            bridge.put(("activity", event))

        tool_events = RunEventStream(
            events.run_id,
            sinks=[*self.event_sinks, event_sink],
            starting_sequence=events.last_sequence,
        )

        def approval_notifier(event: dict[str, Any]) -> None:
            bridge.put(("approval", event))

        context = ToolContext(
            run_id=events.run_id,
            session_id=generation.session_id,
            generation_id=generation.generation_id,
            workspace_root=Path(self.settings_manager.get_workspace_path()),
            events=tool_events,
            cancelled=generation.cancelled,
            approval_notifier=approval_notifier,
        )
        if specialist_stream_state is not None:
            # ToolContext intentionally stays protocol-agnostic.  This private
            # callback is attached only for the specialist backend and bridges
            # normalized sidecar events without changing the public contract.
            context.specialist_event_notifier = lambda event: bridge.put(
                ("specialist", event)
            )

        def execute() -> None:
            try:
                active_executor = executor or self.executor
                result = asyncio.run(active_executor.execute(context, invocation))
                bridge.put(("result", result))
            except BaseException as exc:
                bridge.put(("error", exc))

        worker = threading.Thread(target=execute, daemon=True)
        worker.start()
        result: ToolResult | None = None
        while result is None:
            if generation.cancelled.is_set() and not worker.is_alive() and bridge.empty():
                break
            try:
                kind, value = bridge.get(timeout=0.05)
            except queue.Empty:
                continue
            if kind == "activity":
                events.advance_to(max(events.last_sequence, value.sequence))
                yield _activity(value)
            elif kind == "approval":
                yield value
            elif kind == "specialist":
                # Runtime/audit still receives normalized progress metadata.
                # Text is reconciled only from message.complete; see
                # _stream_specialist for the upstream interim semantics.
                continue
            elif kind == "result":
                result = value
            elif kind == "error":
                raise value
        worker.join(timeout=0.1)
        if result is None:
            return ToolResult(
                invocation_id=invocation.invocation_id,
                tool_name=invocation.tool_name,
                status=ToolStatus.CANCELLED,
                reason_code="execution_cancelled",
            )
        return result

    def _egress_allowed(self, provider: ProviderContext, result: ToolResult) -> bool:
        return self.egress_policy.evaluate(
            provider.destination,
            DataClass.WORKSPACE_CONTENT,
        ).allowed

    def _local_only_result(self, provider: ProviderContext, result: ToolResult) -> str:
        reason = self.egress_policy.evaluate(
            provider.destination,
            DataClass.WORKSPACE_CONTENT,
        ).reason_code
        output = json.dumps(result.output, ensure_ascii=False, indent=2)
        return (
            f"Tool `{result.tool_name}` ran locally, but its result was not sent to "
            f"the cloud model (`{reason}`).\n\n```json\n{output}\n```"
        )

    @staticmethod
    def _local_only_history_message() -> dict[str, str]:
        # The UI may display local output, but the next model request must not
        # recover that denied output through assistant history.
        return {
            "role": "assistant",
            "content": "A local tool completed. Its output was withheld from the cloud model.",
        }

    @staticmethod
    def _deterministic_tool_result(results: Sequence[ToolResult]) -> str:
        payload = [
            {
                "tool": result.tool_name,
                "status": result.status.value,
                "output": result.output,
            }
            for result in results
        ]
        rendered: Any = payload[0] if len(payload) == 1 else payload
        return (
            "The action completed. The model did not produce a final response; here "
            "is the runtime-verified result:\n\n"
            f"```json\n{json.dumps(rendered, ensure_ascii=False, indent=2)}\n```"
        )

    @staticmethod
    def _deterministic_terminal_result(result: ToolResult) -> str:
        output = result.output if isinstance(result.output, Mapping) else {}
        script = output.get("script")
        script_data = script if isinstance(script, Mapping) else {}
        path = str(script_data.get("path") or "script Python")
        exit_code = output.get("exit_code")
        timed_out = bool(output.get("timed_out"))
        if timed_out:
            lead = f"Script `{path}` was created, but execution timed out."
        elif exit_code == 0:
            lead = f"Script `{path}` was created and ran successfully."
        else:
            lead = (
                f"Script `{path}` was created, but execution exited with code "
                f"`{exit_code}`."
            )
        details = {
            "exit_code": exit_code,
            "timed_out": timed_out,
            "stdout": output.get("stdout", ""),
            "stderr": output.get("stderr", ""),
        }
        rendered = json.dumps(details, ensure_ascii=False, indent=2)
        if len(rendered) > MAX_DETERMINISTIC_SUMMARY_CHARS:
            rendered = (
                rendered[:MAX_DETERMINISTIC_SUMMARY_CHARS]
                + "\n... output truncated by the runtime ..."
            )
        indented = "\n".join(f"    {line}" for line in rendered.splitlines())
        return f"{lead}\n\nRuntime-verified result:\n\n{indented}"


class NativeSessionService:
    """Own the short-term session used by the native runtime."""

    def __init__(
        self,
        coordinator: RunCoordinator,
        *,
        approval_broker: ApprovalBroker,
        execution_policy_provider: Callable[[], str],
        initial_session_id: str | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.approval_broker = approval_broker
        self._execution_policy_provider = execution_policy_provider
        self._session_id = initial_session_id or uuid.uuid4().hex
        self._transcript = ShortTermTranscript()
        self._state = threading.Condition(threading.RLock())
        self._active_generation: Generation | None = None
        self._reset_pending = False

    @property
    def session_id(self) -> str:
        with self._state:
            return self._session_id

    def start_generation(
        self,
        requested_session_id: str | None,
        requested_generation_id: str | None = None,
        *,
        routing_signal: RoutingSignal | None = None,
        tool_invocation: ToolInvocation | None = None,
    ) -> Generation:
        with self._state:
            effective = requested_session_id or self._session_id
            if effective != self._session_id:
                raise StaleSessionError(effective, self._session_id)
            if self._active_generation is not None or self._reset_pending:
                raise SessionBusyError("The previous generation is still stopping.")
            generation = Generation(
                session_id=self._session_id,
                generation_id=requested_generation_id or uuid.uuid4().hex,
                routing_signal=routing_signal or RoutingSignal(),
                tool_invocation=tool_invocation,
            )
            self._active_generation = generation
            return generation

    def is_current(self, generation: Generation) -> bool:
        with self._state:
            return (
                self._active_generation is generation
                and generation.session_id == self._session_id
                and not generation.cancelled.is_set()
            )

    def cancel_generation(self, session_id: str, generation_id: str) -> bool:
        """Stop one generation without rotating or clearing its session."""
        with self._state:
            if session_id != self._session_id:
                raise StaleSessionError(session_id, self._session_id)
            active = self._active_generation
            if active is None:
                return False
            if active.generation_id != generation_id:
                raise StaleGenerationError("The requested generation is no longer active.")
            active.cancelled.set()
            return True

    def stream(
        self,
        generation: Generation,
        messages: Iterable[Mapping[str, Any]],
    ) -> Iterator[dict[str, Any]]:
        incoming = list(messages)
        try:
            yield from self.coordinator.stream(generation, incoming, self._transcript)
        finally:
            with self._state:
                if self._active_generation is generation:
                    self._active_generation = None
                self._state.notify_all()

    def finish_generation(self, generation: Generation) -> None:
        with self._state:
            if self._active_generation is generation:
                self._active_generation = None
            self._state.notify_all()

    def reset(self, wait_timeout: float = 10.0) -> dict[str, str | None]:
        deadline = time.monotonic() + wait_timeout
        with self._state:
            if self._reset_pending:
                raise SessionBusyError("The previous session is still resetting.")
            old_session = self._session_id
            active = self._active_generation
            invalidated = active.generation_id if active is not None else None
            self._reset_pending = True
            if active is not None:
                active.cancelled.set()
            self._session_id = uuid.uuid4().hex
            while self._active_generation is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._state.wait(remaining):
                    self._reset_pending = False
                    self._state.notify_all()
                    raise SessionBusyError("The cancelled generation has not stopped yet.")
            self._transcript.clear()
            self._reset_pending = False
            self._state.notify_all()
            return {
                "session_id": self._session_id,
                "previous_session_id": old_session,
                "invalidated_generation_id": invalidated,
            }


def load_system_prompt(settings_manager: Any) -> str:
    path = os.path.join(settings_manager.root_dir, "system_prompt.txt")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return "You are Jarvs Spotlight, a concise desktop assistant."
