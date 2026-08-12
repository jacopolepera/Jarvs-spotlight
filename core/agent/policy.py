"""Pure execution-policy contracts.

The model never decides whether an operation may run.  Every registered native
tool or external backend must first normalize its request into
:class:`ExecutionIntent` and evaluate it here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class ExecutionPolicy(str, Enum):
    """User-selected global behavior for executable actions."""

    ALWAYS_ASK = "always_ask"
    AUTO_RUN = "auto_run"
    DENY_ALL = "deny_all"


class ExecutionDecision(str, Enum):
    """Decision returned by the policy layer for one concrete invocation."""

    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True)
class ExecutionIntent:
    """A single, immutable execution request.

    ``blocked_reason`` is reserved for non-overridable safety boundaries such
    as an invalid schema, disabled capability or workspace-scope escape.
    """

    source: str
    tool_name: str
    title: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    capability: str = "unspecified"
    effect: str = "unknown"
    risk: str = "unknown"
    scope: str = "unspecified"
    tool_version: str = "unspecified"
    blocked_reason: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "source",
            "tool_name",
            "title",
            "capability",
            "effect",
            "risk",
            "scope",
            "tool_version",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or len(value) > 500:
                raise ValueError(f"{field_name} must be a non-empty bounded string")
        if self.blocked_reason is not None and (
            not isinstance(self.blocked_reason, str) or not self.blocked_reason
        ):
            raise ValueError("blocked_reason must be None or a non-empty string")
        if not isinstance(self.arguments, Mapping):
            raise ValueError("execution arguments must be a JSON object")
        try:
            normalized = json.loads(
                json.dumps(
                    _thaw_json(self.arguments),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("execution arguments must contain only JSON values") from exc
        object.__setattr__(self, "arguments", _freeze_json(normalized))

    def arguments_dict(self) -> dict[str, Any]:
        return _thaw_json(self.arguments)

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(
            {
                "source": self.source,
                "tool_name": self.tool_name,
                "arguments": self.arguments_dict(),
                "capability": self.capability,
                "effect": self.effect,
                "risk": self.risk,
                "scope": self.scope,
                "tool_version": self.tool_version,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class PolicyResult:
    decision: ExecutionDecision
    reason_code: str


def parse_execution_policy(value: object) -> ExecutionPolicy:
    """Parse persisted input, failing closed for missing or invalid values."""

    try:
        return ExecutionPolicy(value)
    except (TypeError, ValueError):
        return ExecutionPolicy.ALWAYS_ASK


def evaluate_execution(
    policy: ExecutionPolicy | str,
    intent: ExecutionIntent,
) -> PolicyResult:
    """Return a deterministic decision for one invocation.

    Hard safety denials always win, including over ``AUTO_RUN``.  Capability-
    and tool-specific overrides can be inserted between that boundary and the
    global policy without changing the transport contract.
    """

    mode = parse_execution_policy(policy)
    if intent.blocked_reason:
        return PolicyResult(ExecutionDecision.DENY, intent.blocked_reason)
    if mode is ExecutionPolicy.DENY_ALL:
        return PolicyResult(ExecutionDecision.DENY, "policy_denies_execution")
    if mode is ExecutionPolicy.AUTO_RUN:
        return PolicyResult(ExecutionDecision.ALLOW, "policy_allows_auto_run")
    return PolicyResult(
        ExecutionDecision.REQUIRE_APPROVAL,
        "policy_requires_approval",
    )


def evaluate_native_tool_execution(
    policy: ExecutionPolicy | str,
    intent: ExecutionIntent,
) -> PolicyResult:
    """Apply the transitional native-runtime restriction for generated code.

    Managed subprocesses provide cancellation and resource bounds but are not
    yet a strong OS sandbox.  Code and terminal execution therefore always
    require a one-shot approval.  This override is monotonic: deny-all still
    wins, while workspace reads/writes retain the user's global policy.
    """

    baseline = evaluate_execution(policy, intent)
    if baseline.decision is ExecutionDecision.DENY:
        return baseline
    if intent.effect == "code_execution":
        return PolicyResult(
            ExecutionDecision.REQUIRE_APPROVAL,
            "native_code_execution_requires_approval",
        )
    return baseline
