"""Bounded speech transcription with explicit local or cloud provider selection."""

from __future__ import annotations

import base64
import json
import mimetypes
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

import requests

from core.audio.config import DEFAULT_VOICE_CONFIG, normalize_voice_config


MAX_AUDIO_BYTES = 15 * 1024 * 1024
ALLOWED_AUDIO_MIME_TYPES = frozenset(
    {
        "audio/wav",
        "audio/x-wav",
        "audio/webm",
        "audio/ogg",
        "audio/mpeg",
        "audio/mp4",
    }
)
TRANSCRIPTION_TIMEOUT_SECONDS = 45.0
MAX_TRANSCRIPTION_RESPONSE_BYTES = 256 * 1024

_AUDIO_EXTENSIONS = {
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/webm": "webm",
    "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "mp4",
}


class VoiceTranscriptionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code



def _validate_audio(audio_data: bytes, mime_type: str) -> str:
    if not isinstance(audio_data, bytes) or not audio_data:
        raise VoiceTranscriptionError("invalid_audio", "The audio recording is empty.")
    if len(audio_data) > MAX_AUDIO_BYTES:
        raise VoiceTranscriptionError("audio_too_large", "The recording exceeds the 15 MiB limit.")
    canonical_mime = mime_type.partition(";")[0].strip().lower() if isinstance(mime_type, str) else ""
    if canonical_mime not in ALLOWED_AUDIO_MIME_TYPES:
        raise VoiceTranscriptionError("unsupported_audio_type", "The audio recording format is not supported.")
    return canonical_mime


def transcribe_audio_data(
    audio_data: bytes,
    mime_type: str,
    config: Mapping[str, Any] | None = None,
    *,
    api_key: str = "",
    post: Callable[..., Any] | None = None,
) -> str:
    """Transcribe using exactly the selected provider, never a cloud fallback.

    The local protocol is OpenAI-compatible multipart /audio/transcriptions,
    supported for example by a locally running Speaches server. Jarvs neither
    starts a model server nor downloads models as a side effect of recording.
    """
    canonical_mime = _validate_audio(audio_data, mime_type)
    try:
        voice = normalize_voice_config(config)
    except ValueError as exc:
        raise VoiceTranscriptionError("voice_configuration_invalid", str(exc)) from exc
    if voice["provider"] == "gemini":
        if post is None:
            return transcribe_audio_gemini_data(audio_data, canonical_mime, api_key)
        return transcribe_audio_gemini_data(audio_data, canonical_mime, api_key, post=post)

    fields = {"model": voice["model"], "response_format": "json"}
    if voice["language"]:
        fields["language"] = voice["language"]
    session = None
    response = None
    try:
        if post is None:
            session = requests.Session()
            # Ignore HTTP(S)_PROXY, netrc credentials and environment trust.
            session.trust_env = False
            post = session.post
        response = post(
            voice["base_url"] + "/audio/transcriptions",
            files={"file": (f"recording.{_AUDIO_EXTENSIONS[canonical_mime]}", audio_data, canonical_mime)},
            data=fields,
            timeout=(5.0, TRANSCRIPTION_TIMEOUT_SECONDS),
            allow_redirects=False,
            stream=True,
        )
        if response.status_code != 200:
            raise VoiceTranscriptionError(
                "voice_provider_error",
                f"The local transcription service returned status {response.status_code}. Check its configured model and endpoint.",
            )
        chunks = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            chunks.extend(chunk)
            if len(chunks) > MAX_TRANSCRIPTION_RESPONSE_BYTES:
                raise VoiceTranscriptionError("invalid_voice_response", "The transcription response exceeds its size limit.")
        try:
            text = json.loads(chunks)["text"]
        except (KeyError, TypeError, ValueError) as exc:
            raise VoiceTranscriptionError("invalid_voice_response", "The transcription service returned an invalid response.") from exc
        if not isinstance(text, str):
            raise VoiceTranscriptionError("invalid_voice_response", "The transcription service returned an invalid response.")
        if not text.strip():
            raise VoiceTranscriptionError("empty_transcription", "No text was recognized in the recording.")
        return text.strip()
    except requests.Timeout as exc:
        raise VoiceTranscriptionError("voice_provider_timeout", "Local transcription timed out. Try a shorter recording or a smaller speech model.") from exc
    except requests.RequestException as exc:
        raise VoiceTranscriptionError("voice_provider_unavailable", "The local speech service is unreachable. Start it and check the speech settings.") from exc
    finally:
        if response is not None:
            response.close()
        if session is not None:
            session.close()


def transcribe_audio_gemini_data(
    audio_data: bytes,
    mime_type: str,
    api_key: str,
    *,
    post: Callable[..., Any] = requests.post,
) -> str:
    """Transcribe bounded in-memory audio without exposing the provider key."""

    if not api_key:
        raise VoiceTranscriptionError(
            "voice_api_key_missing",
            "Configure a Gemini API key to use voice transcription.",
        )
    canonical_mime = _validate_audio(audio_data, mime_type)

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": (
                            "Transcribe this audio exactly as spoken in its original "
                            "language, with no commentary or markdown. Return only the "
                            "transcription."
                        )
                    },
                    {
                        "inline_data": {
                            "mime_type": canonical_mime,
                            "data": base64.b64encode(audio_data).decode("ascii"),
                        }
                    },
                ]
            }
        ]
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/gemini-2.5-flash:generateContent?key={api_key}"
    )
    try:
        response = post(
            url,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=TRANSCRIPTION_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise VoiceTranscriptionError(
            "voice_provider_unavailable",
            "The transcription service is unreachable.",
        ) from exc
    except Exception as exc:
        raise VoiceTranscriptionError(
            "voice_provider_unavailable",
            "The transcription request failed.",
        ) from exc
    if response.status_code != 200:
        raise VoiceTranscriptionError(
            "voice_provider_error",
            f"The transcription service returned status {response.status_code}.",
        )
    try:
        text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise VoiceTranscriptionError(
            "invalid_voice_response",
            "The transcription service returned an invalid response.",
        ) from exc
    if not isinstance(text, str) or not text.strip():
        raise VoiceTranscriptionError(
            "empty_transcription",
            "No text was recognized in the recording.",
        )
    return text.strip()


def transcribe_audio_gemini(audio_path: str, api_key: str) -> str:
    """Path-based wrapper retained for integrations that supply audio files."""

    path = Path(audio_path)
    try:
        if path.stat().st_size > MAX_AUDIO_BYTES:
            raise VoiceTranscriptionError(
                "audio_too_large",
                "The recording exceeds the 15 MiB limit.",
            )
        audio_data = path.read_bytes()
    except VoiceTranscriptionError as exc:
        return f"[Error: {exc}]"
    except OSError:
        return "[Error reading audio file]"
    mime_type, _encoding = mimetypes.guess_type(path.name)
    try:
        return transcribe_audio_gemini_data(
            audio_data,
            mime_type or "audio/wav",
            api_key,
        )
    except VoiceTranscriptionError as exc:
        return f"[Error: {exc}]"
