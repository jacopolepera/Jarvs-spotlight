"""Lightweight OpenAI-compatible model adapter used by the native fast path.

The module deliberately uses only the Python standard library.  Importing it
must not initialize optional orchestration frameworks or specialist runtimes.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Iterator, Mapping, Protocol, Sequence


DEFAULT_LM_STUDIO_URL = "http://127.0.0.1:1234/v1"
DEFAULT_OPENAI_URL = "https://api.openai.com/v1"
DEFAULT_GEMINI_OPENAI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/openai"
)
MAX_PROVIDER_ERROR_CHARS = 2_000


class ModelAdapterError(RuntimeError):
    """Base error safe for translation at the transport boundary."""

    code = "model_adapter_error"


class ProviderConfigurationError(ModelAdapterError, ValueError):
    code = "provider_configuration_error"


class ProviderNotCompatibleError(ProviderConfigurationError):
    code = "provider_not_openai_compatible"


class ProviderRequestError(ModelAdapterError):
    code = "provider_request_failed"


class ProviderProtocolError(ModelAdapterError):
    code = "provider_protocol_error"


class ModelGenerationCancelled(ModelAdapterError):
    code = "generation_cancelled"


class ProviderDestination(str, Enum):
    LOCAL = "local"
    CLOUD = "cloud"


@dataclass(frozen=True)
class ProviderCapabilities:
    streaming: bool = True
    tool_calling: bool = True
    vision: bool = False
    reasoning_channel: bool = True


@dataclass(frozen=True)
class ProviderContext:
    """Explicit, immutable destination and credentials for one model call."""

    model_id: str
    model: str
    destination: ProviderDestination
    api_base: str
    api_key: str
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)

    def __post_init__(self) -> None:
        if not self.model_id or not self.model:
            raise ProviderConfigurationError("A model ID and model name are required.")
        parsed = urllib.parse.urlparse(self.api_base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProviderConfigurationError("The provider URL must be an absolute HTTP URL.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ProviderConfigurationError("The provider URL contains unsupported components.")
        if self.destination is ProviderDestination.LOCAL:
            hostname = (parsed.hostname or "").casefold()
            if hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ProviderConfigurationError(
                    "Local model endpoints must use the loopback interface."
                )
        elif parsed.scheme != "https":
            raise ProviderConfigurationError("Cloud model endpoints must use HTTPS.")

    @property
    def chat_completions_url(self) -> str:
        return f"{self.api_base.rstrip('/')}/chat/completions"


def provider_context_from_model(model_config: Mapping[str, Any]) -> ProviderContext:
    """Normalize legacy model-registry entries into a direct-provider context.

    Local entries and cloud APIs with an OpenAI-compatible surface are supported
    directly.  Other providers fail explicitly unless an OpenAI-compatible base
    URL is configured; the native path has no hidden legacy fallback.
    """

    model_id = str(model_config.get("id") or "").strip()
    raw_model = str(model_config.get("model_string") or "").strip()
    provider = str(model_config.get("provider") or "cloud").casefold()
    supports_vision = bool(model_config.get("supports_vision", False))

    if provider == ProviderDestination.LOCAL.value:
        model = raw_model.removeprefix("openai/")
        return ProviderContext(
            model_id=model_id,
            model=model,
            destination=ProviderDestination.LOCAL,
            api_base=str(model_config.get("api_base") or DEFAULT_LM_STUDIO_URL),
            api_key=str(model_config.get("api_key") or "lm-studio"),
            capabilities=ProviderCapabilities(vision=supports_vision),
        )

    lowered = raw_model.casefold()
    explicit_base = model_config.get("api_base")
    if explicit_base:
        api_base = str(explicit_base)
        model = raw_model.removeprefix("openai/")
    elif lowered.startswith("gemini/"):
        api_base = DEFAULT_GEMINI_OPENAI_URL
        model = raw_model.split("/", 1)[1]
    elif lowered.startswith("openai/"):
        api_base = DEFAULT_OPENAI_URL
        model = raw_model.split("/", 1)[1]
    elif lowered.startswith(("gpt-", "o1", "o3", "o4")):
        api_base = DEFAULT_OPENAI_URL
        model = raw_model
    else:
        raise ProviderNotCompatibleError(
            "The selected cloud model does not declare an OpenAI-compatible endpoint."
        )

    api_key = str(model_config.get("api_key") or "").strip()
    if not api_key or api_key == "replace-with-your-api-key":
        raise ProviderConfigurationError("The selected cloud model has no API key.")
    return ProviderContext(
        model_id=model_id,
        model=model,
        destination=ProviderDestination.CLOUD,
        api_base=api_base,
        api_key=api_key,
        capabilities=ProviderCapabilities(vision=supports_vision),
    )


@dataclass(frozen=True)
class ModelToolCallDelta:
    index: int
    call_id: str | None = None
    name: str | None = None
    arguments: str = ""


@dataclass(frozen=True)
class ModelStreamEvent:
    content: str = ""
    reasoning_content: str = ""
    tool_calls: tuple[ModelToolCallDelta, ...] = ()
    finish_reason: str | None = None


@dataclass(frozen=True)
class ModelRequest:
    messages: Sequence[Mapping[str, Any]]
    tools: Sequence[Mapping[str, Any]] = ()
    temperature: float | None = None
    max_tokens: int | None = None


class ModelAdapter(Protocol):
    def stream(
        self,
        context: ProviderContext,
        request: ModelRequest,
        cancelled: threading.Event,
    ) -> Iterator[ModelStreamEvent]: ...


class OpenAICompatibleAdapter:
    """Stream chat-completion deltas without importing an SDK."""

    def __init__(
        self,
        *,
        connect_timeout_seconds: float = 10.0,
        read_timeout_seconds: float = 120.0,
    ) -> None:
        if connect_timeout_seconds <= 0 or read_timeout_seconds <= 0:
            raise ValueError("provider timeouts must be positive")
        self.connect_timeout_seconds = connect_timeout_seconds
        self.read_timeout_seconds = read_timeout_seconds

    def stream(
        self,
        context: ProviderContext,
        request: ModelRequest,
        cancelled: threading.Event,
    ) -> Iterator[ModelStreamEvent]:
        if cancelled.is_set():
            raise ModelGenerationCancelled("The generation was cancelled.")
        payload: dict[str, Any] = {
            "model": context.model,
            "messages": [dict(message) for message in request.messages],
            "stream": True,
        }
        if request.tools:
            if not context.capabilities.tool_calling:
                raise ProviderConfigurationError("The provider does not support tool calls.")
            payload["tools"] = [dict(tool) for tool in request.tools]
            payload["tool_choice"] = "auto"
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "text/event-stream, application/json",
            "Authorization": f"Bearer {context.api_key}",
            "Content-Type": "application/json",
        }
        provider_request = urllib.request.Request(
            context.chat_completions_url,
            data=body,
            headers=headers,
            method="POST",
        )
        response = None
        watcher_stop = threading.Event()
        try:
            response = urllib.request.urlopen(
                provider_request,
                timeout=self.connect_timeout_seconds,
            )
            self._set_read_timeout(response, self.read_timeout_seconds)

            def close_on_cancel() -> None:
                while not watcher_stop.is_set():
                    if cancelled.wait(0.05):
                        if response is not None:
                            response.close()
                        return

            threading.Thread(target=close_on_cancel, daemon=True).start()
            content_type = response.headers.get("Content-Type", "").casefold()
            if "text/event-stream" not in content_type:
                record = self._decode_record(response.read())
                yield from self._events_from_record(record)
                return

            while True:
                if cancelled.is_set():
                    raise ModelGenerationCancelled("The generation was cancelled.")
                try:
                    raw_line = response.readline()
                except (AttributeError, ValueError) as exc:
                    # urllib may clear fp or close its buffer in the cancel
                    # watcher while the reader is still unwinding.
                    if cancelled.is_set():
                        raise ModelGenerationCancelled("The generation was cancelled.") from exc
                    raise
                except (TimeoutError, socket.timeout):
                    if cancelled.is_set():
                        raise ModelGenerationCancelled("The generation was cancelled.")
                    continue
                if cancelled.is_set():
                    raise ModelGenerationCancelled("The generation was cancelled.")
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                yield from self._events_from_record(self._decode_record(data.encode("utf-8")))
        except ModelAdapterError:
            raise
        except urllib.error.HTTPError as exc:
            detail = exc.read(MAX_PROVIDER_ERROR_CHARS).decode("utf-8", errors="replace")
            raise ProviderRequestError(
                f"Provider HTTP {exc.code}: {detail[:MAX_PROVIDER_ERROR_CHARS]}"
            ) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            if cancelled.is_set():
                raise ModelGenerationCancelled("The generation was cancelled.") from exc
            raise ProviderRequestError(f"Provider connection failed: {type(exc).__name__}") from exc
        finally:
            watcher_stop.set()
            if response is not None:
                response.close()

    @staticmethod
    def _set_read_timeout(response: Any, timeout_seconds: float) -> None:
        """Best-effort separation of urllib's connect and stream read timeout."""

        candidates = [
            getattr(
                getattr(getattr(response, "fp", None), "raw", None),
                "_sock",
                None,
            ),
            getattr(getattr(response, "fp", None), "_sock", None),
        ]
        for candidate in candidates:
            setter = getattr(candidate, "settimeout", None)
            if callable(setter):
                setter(timeout_seconds)
                return

    @staticmethod
    def _decode_record(raw: bytes) -> Mapping[str, Any]:
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderProtocolError("The provider returned invalid JSON.") from exc
        if not isinstance(value, dict):
            raise ProviderProtocolError("The provider response must be a JSON object.")
        if value.get("error"):
            error = value["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise ProviderRequestError(str(message)[:MAX_PROVIDER_ERROR_CHARS])
        return value

    @staticmethod
    def _events_from_record(record: Mapping[str, Any]) -> Iterable[ModelStreamEvent]:
        choices = record.get("choices")
        if not isinstance(choices, list):
            raise ProviderProtocolError("The provider response has no choices array.")
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                delta = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            content = delta.get("content")
            reasoning = delta.get("reasoning_content")
            calls: list[ModelToolCallDelta] = []
            raw_calls = delta.get("tool_calls")
            if isinstance(raw_calls, list):
                for position, raw_call in enumerate(raw_calls):
                    if not isinstance(raw_call, dict):
                        continue
                    function = raw_call.get("function")
                    if not isinstance(function, dict):
                        function = {}
                    index = raw_call.get("index", position)
                    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                        raise ProviderProtocolError("A tool-call delta has an invalid index.")
                    calls.append(
                        ModelToolCallDelta(
                            index=index,
                            call_id=raw_call.get("id") if isinstance(raw_call.get("id"), str) else None,
                            name=function.get("name") if isinstance(function.get("name"), str) else None,
                            arguments=function.get("arguments") if isinstance(function.get("arguments"), str) else "",
                        )
                    )
            yield ModelStreamEvent(
                content=content if isinstance(content, str) else "",
                reasoning_content=reasoning if isinstance(reasoning, str) else "",
                tool_calls=tuple(calls),
                finish_reason=(
                    choice.get("finish_reason")
                    if isinstance(choice.get("finish_reason"), str)
                    else None
                ),
            )
