"""Data-egress classification and fail-closed provider policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .model_adapter import ProviderDestination


class DataClass(str, Enum):
    USER_CONTENT = "user_content"
    WORKSPACE_CONTENT = "workspace_content"
    PERSONAL_MEMORY = "personal_memory"
    SENSITIVE_CONTENT = "sensitive_content"


class CloudWorkspaceEgress(str, Enum):
    DENY = "deny"
    ALLOW = "allow"


@dataclass(frozen=True)
class EgressDecision:
    allowed: bool
    reason_code: str


class DataEgressPolicy:
    """Evaluate each provider-bound payload at the point of use.

    Selecting a cloud model authorizes the user's prompt to reach that model,
    but it does not silently authorize local workspace tool output.  Sensitive
    content is never eligible for provider egress in this phase.
    """

    def __init__(
        self,
        cloud_workspace_policy_provider: Callable[[], str],
    ) -> None:
        self._policy_provider = cloud_workspace_policy_provider

    def evaluate(
        self,
        destination: ProviderDestination,
        data_class: DataClass,
    ) -> EgressDecision:
        if data_class is DataClass.SENSITIVE_CONTENT:
            return EgressDecision(False, "sensitive_data_egress_denied")
        if data_class is DataClass.PERSONAL_MEMORY:
            if destination is ProviderDestination.LOCAL:
                return EgressDecision(True, "local_memory_context")
            return EgressDecision(False, "cloud_memory_egress_denied")
        if destination is ProviderDestination.LOCAL:
            return EgressDecision(True, "local_destination")
        if data_class is DataClass.USER_CONTENT:
            return EgressDecision(True, "cloud_model_selected")
        try:
            policy = CloudWorkspaceEgress(self._policy_provider())
        except Exception:
            return EgressDecision(False, "data_egress_policy_unavailable")
        if policy is CloudWorkspaceEgress.ALLOW:
            return EgressDecision(True, "cloud_workspace_egress_allowed")
        return EgressDecision(False, "cloud_workspace_egress_denied")
