"""Authentication and origin checks for the loopback-only daemon."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Iterable


DEFAULT_ALLOWED_ORIGINS = (
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "http://tauri.localhost",
    "tauri://localhost",
)
WS_PROTOCOL = "jarvs-v1"
WS_AUTH_PREFIX = "jarvs-auth."


@dataclass(frozen=True)
class DaemonSecurity:
    token: str
    allowed_origins: frozenset[str]

    @classmethod
    def from_environment(cls) -> "DaemonSecurity":
        configured = os.environ.get("JARVS_ALLOWED_ORIGINS", "")
        origins = (
            [item.strip() for item in configured.split(",") if item.strip()]
            if configured
            else DEFAULT_ALLOWED_ORIGINS
        )
        return cls(
            token=os.environ.get("JARVS_DAEMON_TOKEN", ""),
            allowed_origins=frozenset(origins),
        )

    @classmethod
    def create(
        cls,
        token: str,
        allowed_origins: Iterable[str] = DEFAULT_ALLOWED_ORIGINS,
    ) -> "DaemonSecurity":
        return cls(token=token, allowed_origins=frozenset(allowed_origins))

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def origin_allowed(self, origin: str | None) -> bool:
        # Non-browser harnesses do not necessarily send Origin. They still need
        # the bearer/subprotocol secret, so absence is not an authentication
        # bypass.
        return origin is None or origin in self.allowed_origins

    def bearer_allowed(self, authorization: str | None) -> bool:
        if not self.configured or not authorization:
            return False
        scheme, separator, credential = authorization.partition(" ")
        return (
            bool(separator)
            and scheme.lower() == "bearer"
            and secrets.compare_digest(credential, self.token)
        )

    def websocket_allowed(self, protocols_header: str | None) -> bool:
        if not self.configured or not protocols_header:
            return False
        protocols = {
            protocol.strip()
            for protocol in protocols_header.split(",")
            if protocol.strip()
        }
        expected = f"{WS_AUTH_PREFIX}{self.token}"
        return WS_PROTOCOL in protocols and any(
            secrets.compare_digest(protocol, expected) for protocol in protocols
        )
