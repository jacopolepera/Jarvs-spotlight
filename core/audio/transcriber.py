"""Bounded Gemini speech transcription used by the desktop daemon."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any, Callable

import requests


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


class VoiceTranscriptionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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
    if not isinstance(audio_data, bytes) or not audio_data:
        raise VoiceTranscriptionError("invalid_audio", "The audio recording is empty.")
    if len(audio_data) > MAX_AUDIO_BYTES:
        raise VoiceTranscriptionError(
            "audio_too_large",
            "The recording exceeds the 15 MiB limit.",
        )
    canonical_mime = mime_type.partition(";")[0].strip().lower()
    if canonical_mime not in ALLOWED_AUDIO_MIME_TYPES:
        raise VoiceTranscriptionError(
            "unsupported_audio_type",
            "The audio recording format is not supported.",
        )

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
