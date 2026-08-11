"""Authenticated FastAPI/WebSocket adapter for the Jarvs Python core."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import sys
import threading
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

from core.agent.approvals import ApprovalConflictError, ApprovalNotFoundError  # noqa: E402
from core.agent.policy import (  # noqa: E402
    ExecutionPolicy,
)
from core.agent.egress import CloudWorkspaceEgress  # noqa: E402
from core.agent.routing import RoutingSignal  # noqa: E402
from core.agent.personal_memory import (  # noqa: E402
    MemoryCategory,
    MemoryConsentRequired,
    MemoryNotFoundError,
    MemorySecretRejected,
)
from core.agent.routines import (  # noqa: E402
    RoutineNotFoundError,
    RoutineValidationError,
)
from core.agent.run_store import RunNotFoundError  # noqa: E402
from core.agent.tooling import (  # noqa: E402
    ToolInvocation,
    ToolValidationError,
)
from core.daemon.runtime import DaemonRuntime  # noqa: E402
from core.daemon.security import DaemonSecurity, WS_PROTOCOL  # noqa: E402
from core.daemon.session_service import (  # noqa: E402
    SessionBusyError,
    StaleSessionError,
)
from core.tools.settings_manager import SettingsManager  # noqa: E402


MAX_PROMPT_CHARS = 100_000
MAX_ATTACHMENTS = 8
MAX_VOICE_AUDIO_BYTES = 15 * 1024 * 1024


class ModelUpdate(BaseModel):
    model_id: str = Field(min_length=1, max_length=200)


class ModelSaveUpdate(BaseModel):
    """Unified save payload; omission preserves the current value."""

    model_id: str = Field(min_length=1, max_length=200)
    api_key: str | None = Field(default=None, max_length=8_192)
    api_base: str | None = Field(default=None, max_length=2_048)
    theme: str | None = Field(default=None, max_length=100)
    workspace_path: str | None = Field(default=None, max_length=32_768)
    model_string: str | None = Field(default=None, max_length=500)
    execution_policy: ExecutionPolicy | None = None
    cloud_workspace_egress: CloudWorkspaceEgress | None = None

    @field_validator("api_key")
    @classmethod
    def api_key_must_be_single_line(cls, value):
        if value is not None and ("\r" in value or "\n" in value):
            raise ValueError("API keys must be a single line")
        return value


class AddModelPayload(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    provider: Literal["cloud", "local"]
    model_string: str = Field(min_length=1, max_length=500)
    api_key: str | None = Field(default=None, max_length=8_192)
    api_base: str | None = Field(default=None, max_length=2_048)

    @field_validator("api_key")
    @classmethod
    def api_key_must_be_single_line(cls, value):
        if value is not None and ("\r" in value or "\n" in value):
            raise ValueError("API keys must be a single line")
        return value


class DeleteModelPayload(BaseModel):
    model_id: str = Field(min_length=1, max_length=200)


class ApprovalDecisionPayload(BaseModel):
    decision: Literal["approve", "deny"]
    session_id: str = Field(min_length=1, max_length=200)
    generation_id: str = Field(min_length=1, max_length=200)


class MemoryCreatePayload(BaseModel):
    category: MemoryCategory
    content: str = Field(min_length=1, max_length=16_384)
    provenance: str = Field(default="user_explicit", min_length=1, max_length=500)
    confidence: float = Field(default=1.0, ge=0, le=1)
    expires_at: str | None = Field(default=None, max_length=100)
    consent: bool


class MemoryUpdatePayload(BaseModel):
    category: MemoryCategory | None = None
    content: str | None = Field(default=None, min_length=1, max_length=16_384)
    provenance: str = Field(default="user_edit", min_length=1, max_length=500)
    confidence: float | None = Field(default=None, ge=0, le=1)
    expires_at: str | None = Field(default=None, max_length=100)
    consent: bool


class RoutineSavePayload(BaseModel):
    manifest: dict


class VoiceTranscriptionPayload(BaseModel):
    audio_base64: str = Field(
        min_length=1,
        max_length=((MAX_VOICE_AUDIO_BYTES + 2) // 3) * 4,
    )
    mime_type: str = Field(min_length=1, max_length=100)


def _public_model(settings_manager: SettingsManager, model: dict) -> dict:
    return settings_manager.redact_model(model)


def _apply_model_to_engine(engine, settings_manager, active):
    provider = active.get("provider", "cloud")
    engine.llm.model = active.get("model_string", "gemini/gemini-2.5-flash")

    if provider == "local":
        api_base = active.get("api_base") or "http://localhost:1234/v1"
        engine.llm.api_base = api_base
        engine.llm.api_key = active.get("api_key", "lm-studio")
        engine.offline = True
        os.environ["OPENAI_API_BASE"] = api_base
        os.environ["OPENAI_BASE_URL"] = api_base
    else:
        os.environ.pop("OPENAI_API_BASE", None)
        os.environ.pop("OPENAI_BASE_URL", None)
        if hasattr(engine.llm, "api_base"):
            engine.llm.api_base = None
        engine.llm.api_key = active.get("api_key") or settings_manager.get_api_key()
        engine.offline = False
    engine.auto_run = False


def create_app(
    runtime: DaemonRuntime | None = None,
    security: DaemonSecurity | None = None,
) -> FastAPI:
    runtime = runtime or DaemonRuntime()
    security = security or DaemonSecurity.from_environment()
    settings_manager = runtime.settings_manager

    app = FastAPI(title="Jarvs daemon", version="0.1.0")
    app.state.runtime = runtime
    app.state.security = security

    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(security.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.middleware("http")
    async def authenticate_http(request: Request, call_next):
        origin = request.headers.get("origin")
        if not security.origin_allowed(origin):
            return JSONResponse(
                status_code=403,
                content={"detail": "Origin is not allowed."},
            )
        if request.method == "OPTIONS" or request.url.path == "/health":
            return await call_next(request)
        if not security.bearer_allowed(request.headers.get("authorization")):
            return JSONResponse(
                status_code=401,
                content={"detail": "Valid daemon authentication is required."},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    @app.get("/health")
    def health():
        return {
            "status": "ok" if security.configured else "locked",
            "engine_ready": runtime.engine_ready,
        }

    @app.get("/settings")
    def get_settings():
        return {
            "models": settings_manager.get_public_models_list(),
            "active_model": settings_manager.get_active_model_id(),
            "active_model_config": settings_manager.get_public_active_model(),
            "theme": settings_manager.get_theme(),
            "workspace_path": settings_manager.get_workspace_path(),
            "execution_policy": settings_manager.get_execution_policy(),
            "cloud_workspace_egress": settings_manager.get_cloud_workspace_egress(),
        }

    @app.post("/settings/model")
    def set_model(update: ModelUpdate):
        try:
            settings_manager.set_active_model(update.model_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Model not found.")
        runtime.configure_ready_engine(
            lambda engine: _apply_model_to_engine(
                engine,
                settings_manager,
                settings_manager.get_active_model(),
            )
        )
        active = settings_manager.get_active_model()
        return {"status": "ok", "model": _public_model(settings_manager, active)}

    @app.post("/settings/save")
    def save_model_settings(update: ModelSaveUpdate):
        target_model = next(
            (
                model
                for model in settings_manager.get_models_list()
                if model.get("id") == update.model_id
            ),
            None,
        )
        if target_model is None:
            raise HTTPException(status_code=404, detail="Model not found.")

        if update.theme is not None or update.workspace_path is not None:
            settings_manager.save_settings(
                update.theme or settings_manager.get_theme(),
                update.workspace_path or settings_manager.get_workspace_path(),
                update.model_id,
            )
        if update.execution_policy is not None:
            canonical_policy = settings_manager.set_execution_policy(
                update.execution_policy.value
            )
            if canonical_policy == ExecutionPolicy.DENY_ALL.value:
                runtime.approval_broker.deny_pending()
        if update.cloud_workspace_egress is not None:
            settings_manager.set_cloud_workspace_egress(
                update.cloud_workspace_egress.value
            )

        if update.model_string:
            settings_manager.update_model(
                update.model_id,
                {"model_string": update.model_string},
            )

        provider = target_model.get("provider", "cloud")
        if provider == "local":
            if update.api_base:
                settings_manager.update_model(
                    update.model_id,
                    {"api_base": update.api_base},
                )
        elif update.api_key:
            # Blank means "keep the existing secret"; secrets are never sent
            # back to the renderer.
            settings_manager.update_model(
                update.model_id,
                {"api_key": update.api_key},
            )
        if update.model_id == settings_manager.get_active_model_id():
            runtime.configure_ready_engine(
                lambda engine: _apply_model_to_engine(
                    engine,
                    settings_manager,
                    settings_manager.get_active_model(),
                )
            )
        return {
            "status": "ok",
            "execution_policy": settings_manager.get_execution_policy(),
            "cloud_workspace_egress": settings_manager.get_cloud_workspace_egress(),
        }

    @app.post("/settings/models/add")
    def add_model(payload: AddModelPayload):
        model_id = (
            payload.name.lower()
            .replace(" ", "-")
            .replace("(", "")
            .replace(")", "")
        )
        model_dict = {
            "id": model_id,
            "name": payload.name,
            "provider": payload.provider,
            "model_string": payload.model_string,
            "api_key": payload.api_key if payload.provider == "cloud" else "lm-studio",
            "api_base": payload.api_base if payload.provider == "local" else None,
            "supports_vision": True,
            "context_window": None,
        }
        final_id = settings_manager.add_model(model_dict)
        return {"status": "ok", "model_id": final_id}

    @app.post("/settings/models/delete")
    def delete_model(payload: DeleteModelPayload):
        if not settings_manager.delete_model(payload.model_id):
            raise HTTPException(status_code=404, detail="Model not found.")
        if settings_manager.get_models_list():
            runtime.configure_ready_engine(
                lambda engine: _apply_model_to_engine(
                    engine,
                    settings_manager,
                    settings_manager.get_active_model(),
                )
            )
        return {
            "status": "ok",
            "active_model": settings_manager.get_active_model_id(),
        }

    @app.get("/memory")
    def list_memory(
        category: MemoryCategory | None = None,
        include_expired: bool = False,
    ):
        return {
            "schema_version": 1,
            "memories": [
                item.as_dict()
                for item in runtime.memory_store.list(
                    category=category,
                    include_expired=include_expired,
                )
            ],
        }

    @app.post("/memory")
    def create_memory(payload: MemoryCreatePayload):
        try:
            item = runtime.memory_store.create(
                category=payload.category,
                content=payload.content,
                provenance=payload.provenance,
                confidence=payload.confidence,
                expires_at=payload.expires_at,
                consent=payload.consent,
            )
        except (MemoryConsentRequired, MemorySecretRejected, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "ok", "memory": item.as_dict()}

    @app.patch("/memory/{memory_id}")
    def update_memory(memory_id: str, payload: MemoryUpdatePayload):
        updates = {
            "category": payload.category,
            "content": payload.content,
            "confidence": payload.confidence,
            "provenance": payload.provenance,
            "consent": payload.consent,
        }
        if "expires_at" in payload.model_fields_set:
            updates["expires_at"] = payload.expires_at
        try:
            item = runtime.memory_store.update(memory_id, **updates)
        except MemoryNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (MemoryConsentRequired, MemorySecretRejected, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "ok", "memory": item.as_dict()}

    @app.delete("/memory/{memory_id}")
    def delete_memory(memory_id: str):
        if not runtime.memory_store.delete(memory_id):
            raise HTTPException(status_code=404, detail="Memory item not found.")
        return {"status": "ok"}

    @app.get("/memory/export")
    def export_memory():
        return runtime.memory_store.export(include_expired=True)

    @app.get("/routines")
    def list_routines():
        return {
            "schema_version": 1,
            "routines": [item.as_dict() for item in runtime.get_routine_store().list()],
        }

    @app.post("/routines")
    def create_routine(payload: RoutineSavePayload):
        try:
            item = runtime.get_routine_store().save(payload.manifest)
        except (RoutineValidationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "ok", "routine": item.as_dict()}

    @app.put("/routines/{routine_id}")
    def update_routine(routine_id: str, payload: RoutineSavePayload):
        try:
            # Updating an unknown identifier would silently create it, so
            # require an existing active version at the API boundary.
            runtime.get_routine_store().get(routine_id)
            item = runtime.get_routine_store().save(
                payload.manifest,
                routine_id=routine_id,
            )
        except RoutineNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (RoutineValidationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "ok", "routine": item.as_dict()}

    @app.delete("/routines/{routine_id}")
    def delete_routine(routine_id: str):
        if not runtime.get_routine_store().delete(routine_id):
            raise HTTPException(status_code=404, detail="Routine not found.")
        return {"status": "ok"}

    @app.get("/routines/export")
    def export_routines():
        return runtime.get_routine_store().export()

    @app.get("/runs")
    def list_runs(
        status: Literal["running", "completed", "cancelled", "failed"] | None = None,
        limit: int = 100,
    ):
        try:
            runs = runtime.run_store.list_runs(status=status, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"runs": [run.as_dict() for run in runs]}

    @app.get("/runs/{run_id}")
    def get_run(run_id: str):
        try:
            run = runtime.run_store.get_run(run_id)
            events = runtime.run_store.replay(run_id)
            effects = runtime.run_store.list_effects(run_id=run_id, limit=500)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "run": run.as_dict(),
            "events": [event.as_dict() for event in events],
            "effects": [effect.as_dict() for effect in effects],
        }

    @app.post("/voice/transcribe")
    def transcribe_voice(payload: VoiceTranscriptionPayload):
        # Voice dependencies and their HTTP client stay out of startup and the
        # ordinary chat path.
        from core.audio.transcriber import (
            VoiceTranscriptionError,
            transcribe_audio_gemini_data,
        )

        try:
            audio = base64.b64decode(payload.audio_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_audio", "message": "The audio recording is invalid."},
            ) from exc
        try:
            text = transcribe_audio_gemini_data(
                audio,
                payload.mime_type,
                settings_manager.get_voice_api_key(),
            )
        except VoiceTranscriptionError as exc:
            status_code = 409 if exc.code == "voice_api_key_missing" else 422
            if exc.code in {"voice_provider_unavailable", "voice_provider_error"}:
                status_code = 502
            raise HTTPException(
                status_code=status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        return {"status": "ok", "text": text}

    @app.post("/approvals/{approval_id}/decision")
    def decide_approval(approval_id: str, payload: ApprovalDecisionPayload):
        try:
            status = runtime.approval_broker.resolve(
                approval_id,
                approve=payload.decision == "approve",
                session_id=payload.session_id,
                generation_id=payload.generation_id,
            )
        except ApprovalNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ApprovalConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "ok", "decision": status.value}

    @app.post("/session/reset")
    def reset_session():
        try:
            reset_result = runtime.reset_session()
        except SessionBusyError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        return {"status": "ok", **reset_result}

    @app.websocket("/chat")
    async def websocket_endpoint(websocket: WebSocket):
        origin = websocket.headers.get("origin")
        protocols = websocket.headers.get("sec-websocket-protocol")
        if (
            not security.origin_allowed(origin)
            or not security.websocket_allowed(protocols)
        ):
            await websocket.close(code=1008)
            return

        await websocket.accept(subprotocol=WS_PROTOCOL)
        await websocket.send_json(
            {"type": "session", "session_id": runtime.session_id}
        )
        active_generation = None
        try:
            while True:
                data = await websocket.receive_text()
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    await websocket.send_json(
                        _protocol_error(runtime.session_id, "invalid_payload", "The WebSocket message must be valid JSON.")
                    )
                    continue
                if not isinstance(payload, dict):
                    await websocket.send_json(
                        _protocol_error(runtime.session_id, "invalid_payload", "The WebSocket message must be a JSON object.")
                    )
                    continue

                prompt = payload.get("prompt", "")
                attachments = payload.get("attachments", [])
                requested_session_id = payload.get("session_id")
                requested_generation_id = payload.get("generation_id")
                if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT_CHARS:
                    await websocket.send_json(
                        _protocol_error(runtime.session_id, "invalid_prompt", "Prompt must be a string within the configured limit.", requested_generation_id)
                    )
                    continue
                if not isinstance(attachments, list) or len(attachments) > MAX_ATTACHMENTS:
                    await websocket.send_json(
                        _protocol_error(runtime.session_id, "invalid_attachment", "Too many or invalid attachments.", requested_generation_id)
                    )
                    continue
                if not prompt and not attachments:
                    continue

                try:
                    routing_signal, tool_invocation = _routing_request(payload)
                except (ValueError, ToolValidationError) as exc:
                    await websocket.send_json(
                        _protocol_error(
                            runtime.session_id,
                            "invalid_route",
                            str(exc),
                            requested_generation_id,
                        )
                    )
                    continue

                try:
                    service = await asyncio.to_thread(runtime.get_session_service)
                except Exception as exc:
                    print(f"[Daemon] Failed to initialize the agent runtime: {exc}")
                    await websocket.send_json(
                        _protocol_error(
                            runtime.session_id,
                            "engine_unavailable",
                            "The agent runtime could not be initialized.",
                            requested_generation_id,
                        )
                    )
                    continue

                try:
                    generation = service.start_generation(
                        requested_session_id,
                        requested_generation_id,
                        routing_signal=routing_signal,
                        tool_invocation=tool_invocation,
                    )
                    active_generation = generation
                except StaleSessionError as exc:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "code": exc.code,
                            "text": str(exc),
                            "session_id": exc.current_session_id,
                            "requested_session_id": exc.requested_session_id,
                            "generation_id": requested_generation_id,
                        }
                    )
                    continue
                except SessionBusyError as exc:
                    await websocket.send_json(
                        _protocol_error(service.session_id, exc.code, str(exc), requested_generation_id)
                    )
                    continue

                try:
                    if not all(isinstance(item, dict) for item in attachments):
                        raise ValueError("Each attachment must be an object.")
                    messages_to_send = await asyncio.to_thread(
                        runtime.attachment_broker.encode_many,
                        attachments,
                    )
                    if prompt:
                        messages_to_send.append(
                            {"role": "user", "type": "message", "content": prompt}
                        )
                except Exception as exc:
                    service.finish_generation(generation)
                    active_generation = None
                    await websocket.send_json(
                        _protocol_error(
                            service.session_id,
                            "invalid_attachment",
                            str(exc),
                            generation.generation_id,
                        )
                    )
                    continue

                await websocket.send_json(
                    {
                        "type": "started",
                        "session_id": generation.session_id,
                        "generation_id": generation.generation_id,
                    }
                )
                queue: asyncio.Queue = asyncio.Queue()
                loop = asyncio.get_running_loop()

                def run_chat():
                    terminal = None
                    try:
                        for chunk in service.stream(generation, messages_to_send):
                            if chunk.get("type") == "activity":
                                asyncio.run_coroutine_threadsafe(
                                    queue.put(
                                        {
                                            "type": "activity",
                                            "activity": chunk.get("content", {}),
                                            "session_id": generation.session_id,
                                            "generation_id": generation.generation_id,
                                        }
                                    ),
                                    loop,
                                )
                                continue
                            if chunk.get("type") == "approval_required":
                                asyncio.run_coroutine_threadsafe(
                                    queue.put(chunk),
                                    loop,
                                )
                                continue
                            if chunk.get("type") == "execution_denied":
                                reason = chunk.get("content", {}).get(
                                    "reason_code", "execution_denied"
                                )
                                terminal = {
                                    "type": "error",
                                    "code": "execution_denied",
                                    "text": f"Execution was not run ({reason}).",
                                    "session_id": generation.session_id,
                                    "generation_id": generation.generation_id,
                                }
                                break

                            text_to_send = _chunk_text(chunk)
                            if text_to_send:
                                metadata = _chunk_metadata(chunk)
                                asyncio.run_coroutine_threadsafe(
                                    queue.put(
                                        {
                                            "type": "chunk",
                                            "text": text_to_send,
                                            **metadata,
                                            "session_id": generation.session_id,
                                            "generation_id": generation.generation_id,
                                        }
                                    ),
                                    loop,
                                )

                        if terminal is None:
                            if (
                                generation.cancelled.is_set()
                                or generation.session_id != service.session_id
                            ):
                                terminal = {
                                    "type": "error",
                                    "code": "generation_cancelled",
                                    "text": "Generation cancelled because the session was reset.",
                                    "session_id": generation.session_id,
                                    "generation_id": generation.generation_id,
                                }
                            else:
                                terminal = {
                                    "type": "done",
                                    "session_id": generation.session_id,
                                    "generation_id": generation.generation_id,
                                }
                    except Exception as exc:
                        service.finish_generation(generation)
                        if generation.cancelled.is_set():
                            terminal = {
                                "type": "error",
                                "code": "generation_cancelled",
                                "text": "Generation cancelled because the session was reset.",
                                "session_id": generation.session_id,
                                "generation_id": generation.generation_id,
                            }
                        else:
                            error_code = getattr(exc, "code", "generation_failed")
                            terminal = {
                                "type": "error",
                                "code": error_code,
                                "text": _public_generation_error(error_code),
                                "session_id": generation.session_id,
                                "generation_id": generation.generation_id,
                            }
                    asyncio.run_coroutine_threadsafe(queue.put(terminal), loop)

                threading.Thread(target=run_chat, daemon=True).start()
                while True:
                    queued = asyncio.create_task(queue.get())
                    inbound = asyncio.create_task(websocket.receive())
                    completed, _pending = await asyncio.wait(
                        {queued, inbound},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    inbound_event = (
                        inbound.result() if inbound in completed else None
                    )
                    if inbound in completed:
                        if inbound_event.get("type") == "websocket.disconnect":
                            generation.cancelled.set()
                            if not queued.done():
                                queued.cancel()
                                await asyncio.gather(queued, return_exceptions=True)
                            raise WebSocketDisconnect(inbound_event.get("code", 1000))

                    if queued not in completed:
                        # A second prompt cannot share the stateful engine. Keep
                        # waiting for the active generation's next event.
                        queued.cancel()
                        await asyncio.gather(queued, return_exceptions=True)
                        await websocket.send_json(
                            _protocol_error(
                                service.session_id,
                                "session_busy",
                                "A generation is already running.",
                                _received_generation_id(
                                    inbound_event,
                                    generation.generation_id,
                                ),
                            )
                        )
                        continue

                    if inbound not in completed:
                        inbound.cancel()
                        await asyncio.gather(inbound, return_exceptions=True)
                    message = queued.result()
                    if inbound_event is not None:
                        # Never consume an inbound prompt silently, including
                        # when it becomes ready in the same tick as a terminal
                        # or stale outbound event.
                        await websocket.send_json(
                            _protocol_error(
                                service.session_id,
                                "session_busy",
                                "A generation is already running.",
                                _received_generation_id(
                                    inbound_event,
                                    generation.generation_id,
                                ),
                            )
                        )
                    if (
                        message["type"] == "chunk"
                        and message["session_id"] != service.session_id
                    ):
                        continue
                    await websocket.send_text(json.dumps(message))
                    if message["type"] in {"done", "error"}:
                        active_generation = None
                        break

        except WebSocketDisconnect:
            if active_generation is not None:
                active_generation.cancelled.set()

    return app


def _received_generation_id(event: dict, fallback: str) -> str:
    """Best-effort correlation for a prompt rejected while another run owns the engine."""

    raw = event.get("text")
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            generation_id = payload.get("generation_id")
            if isinstance(generation_id, str) and 0 < len(generation_id) <= 200:
                return generation_id
    return fallback


def _routing_request(payload: dict) -> tuple[RoutingSignal, ToolInvocation | None]:
    """Parse an optional structured lane signal without inspecting prompt text."""

    route = payload.get("route")
    if route is None:
        return RoutingSignal(), None
    if not isinstance(route, dict):
        raise ValueError("route must be a JSON object")
    unknown = set(route) - {"tool_name", "specialist_capability", "arguments"}
    if unknown:
        raise ValueError(f"route contains unknown fields: {', '.join(sorted(unknown))}")
    signal = RoutingSignal(
        tool_name=route.get("tool_name"),
        specialist_capability=route.get("specialist_capability"),
    )
    invocation = None
    if "arguments" in route:
        if signal.tool_name is None:
            raise ValueError("route arguments require tool_name")
        arguments = route["arguments"]
        if not isinstance(arguments, dict):
            raise ValueError("route arguments must be a JSON object")
        invocation = ToolInvocation.create(signal.tool_name, arguments)
    return signal, invocation


def _protocol_error(
    session_id: str,
    code: str,
    text: str,
    generation_id=None,
) -> dict:
    return {
        "type": "error",
        "code": code,
        "text": text,
        "session_id": session_id,
        "generation_id": generation_id,
    }


def _public_generation_error(code: str) -> str:
    messages = {
        "incomplete_model_response": (
            "The model did not produce a valid response or tool call, even after "
            "the recovery attempt."
        ),
        "invalid_model_tool_call": (
            "The model requested an unavailable tool or supplied invalid arguments."
        ),
        "provider_configuration_error": "The model configuration is invalid.",
        "provider_not_openai_compatible": (
            "The selected provider does not expose a Jarvs-compatible endpoint."
        ),
        "provider_request_failed": "The model provider is unreachable or did not respond.",
        "provider_protocol_error": "The provider returned an invalid response.",
        "coordinator_error": "The tool execution flow did not complete.",
    }
    return messages.get(code, "Generation did not complete. Review the activity trace.")


def _chunk_text(chunk):
    role = chunk.get("role")
    type_ = chunk.get("type")
    if role not in ["assistant", "computer"]:
        return ""
    if chunk.get("start"):
        if type_ == "code":
            return f"\n```{chunk.get('format', 'python')}\n"
        if type_ == "console":
            return "\n```console\n"
        return ""
    if chunk.get("end"):
        return "\n```\n" if type_ in ["code", "console"] else ""
    if "content" not in chunk:
        return ""
    content = chunk["content"]
    if type_ == "console" and chunk.get("format") == "active_line":
        return ""
    if isinstance(content, str):
        if type_ == "console" and len(content) > 1000 and " " not in content[:100]:
            return "\n\n*[Large binary/base64 output omitted]*\n\n"
        return content
    text_to_send = ""
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "message":
                text_to_send += block.get("content", "")
    return text_to_send


def _chunk_metadata(chunk):
    role = chunk.get("role")
    chunk_type = chunk.get("type") or "message"
    return {
        "channel": (
            "answer"
            if role == "assistant" and chunk_type == "message"
            else "thought"
        ),
        "chunk_type": chunk_type,
        "format": chunk.get("format"),
    }


settings_mgr = SettingsManager()
runtime = DaemonRuntime(settings_manager=settings_mgr)
app = create_app(runtime=runtime, security=DaemonSecurity.from_environment())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("JARVS_DAEMON_PORT", "8000")),
        ws_max_size=1_048_576,
        access_log=False,
    )
