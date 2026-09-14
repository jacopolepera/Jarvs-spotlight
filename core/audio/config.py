"""Secret-free speech configuration, safe to load without HTTP dependencies."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit


DEFAULT_VOICE_CONFIG = {
    "provider": "local",
    "base_url": "http://127.0.0.1:8000/v1",
    "model": "Systran/faster-whisper-small",
    "language": "",
}


def normalize_voice_config(config: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Validate persistent, secret-free settings; absent settings always mean local.

    Local hosts are literal loopback addresses; localhost is rewritten to a
    literal address to avoid DNS and accidental forwarding outside the device.
    """
    if config is not None and not isinstance(config, Mapping):
        raise ValueError("Voice settings must be an object.")
    values = dict(DEFAULT_VOICE_CONFIG)
    if config is not None:
        if set(config) - set(values):
            raise ValueError("Unknown voice setting.")
        values.update(config)
    if any(not isinstance(value, str) for value in values.values()):
        raise ValueError("Voice settings must be strings.")
    values = {key: value.strip() for key, value in values.items()}
    if values["provider"] not in {"local", "gemini"}:
        raise ValueError("Select the local or Gemini transcription provider.")
    if not values["model"] or len(values["model"]) > 200 or any(
        ord(char) < 32 for char in values["model"]
    ):
        raise ValueError("Set a valid local transcription model name.")
    if values["language"] and not re.fullmatch(r"[a-z]{2,3}", values["language"]):
        raise ValueError("Voice language must be a lowercase language code, or empty for automatic detection.")
    try:
        if len(values["base_url"]) > 2048 or any(
            ord(char) < 32 for char in values["base_url"]
        ):
            raise ValueError
        parsed = urlsplit(values["base_url"])
        host = parsed.hostname
        if host == "localhost":
            host = "127.0.0.1"
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or not host
            or not ipaddress.ip_address(host).is_loopback
        ):
            raise ValueError
        port = parsed.port
        if port == 0:
            raise ValueError
        netloc = f"[{host}]" if ":" in host else host
        if port is not None:
            netloc += f":{port}"
        values["base_url"] = urlunsplit(
            (parsed.scheme, netloc, parsed.path.rstrip("/"), "", "")
        )
    except (ValueError, TypeError) as exc:
        raise ValueError("The local speech URL must use a loopback address without credentials, query, or fragment.") from exc
    return values

