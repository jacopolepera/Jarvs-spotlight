"""Model-visible dispatch contract for progressive native tool disclosure."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .tooling import (
    ToolBackend,
    ToolDefinition,
    ToolEffect,
    ToolNotFoundError,
    ToolRegistry,
    ToolValidationError,
)


ACTION_DISPATCH_NAME = "jarvs.request_action"
ACTION_DISPATCH_PROVIDER_NAME = "jarvs__request_action"
ACTION_PROFILES = ("inspect", "automate", "system")
# Compatibility alias for code that imported the original constant.  These are
# disclosure profiles, not authorization capabilities.
ACTION_CAPABILITIES = ACTION_PROFILES
LEGACY_PROFILE_ALIASES = {
    "workspace": "automate",
    "routine": "automate",
    "script": "automate",
    "system": "system",
}


@dataclass(frozen=True)
class ActionSelection:
    profile: str
    definitions: tuple[ToolDefinition, ...]


@dataclass(frozen=True)
class ActionCatalogHint:
    profile: str
    reason_code: str
    definitions: tuple[ToolDefinition, ...]


_PYTHON_SCRIPT_PATTERN = re.compile(r"(?i)(?:\bpython\b|\.py\b)")
_CREATE_PATTERN = re.compile(
    r"(?i)\b(?:crea|creami|scrivi|scrivimi|genera|generami|create|write|generate)\b"
)
_RUN_PATTERN = re.compile(
    r"(?i)\b(?:esegui|eseguilo|eseguila|avvia|avvialo|lancia|lancialo|run|runnalo|execute)\b"
)
_EXPLANATION_PATTERN = re.compile(
    r"(?i)\b(?:spiegami|spiega|come\s+(?:si|posso)|explain|how\s+(?:do|can|to)|"
    r"show\s+me\s+how|tell\s+me\s+how)\b"
)
_NEGATED_RUN_PATTERN = re.compile(
    r"(?i)\b(?:non|senza|do\s+not|don't|not)\s+"
    r"(?:esegui(?:re|rlo|rla)?|avvia(?:re|rlo|rla)?|lancia(?:re|rlo|rla)?|run|execute)\b"
)


def action_dispatch_tool() -> dict[str, Any]:
    """Return the single constant schema attached to an undecided request."""

    return {
        "type": "function",
        "function": {
            "name": ACTION_DISPATCH_PROVIDER_NAME,
            "description": (
                "Request Jarvs native tools only when the user asks for a local action. "
                "Use inspect only for read-only workspace or routine inspection. Use automate "
                "for every task that may create/change files, reuse/save routines, or create "
                "and run scripts, including multi-step create-and-run requests. Use system only "
                "when an explicit raw Python or PowerShell command is necessary. Do not call "
                "this for conversation, translation, rewriting, summarization, or factual "
                "answers that need no local action."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string",
                        "enum": list(ACTION_PROFILES),
                    }
                },
                "required": ["profile"],
                "additionalProperties": False,
            },
        },
    }


def parse_action_selection(
    raw_arguments: str,
    registry: ToolRegistry,
) -> ActionSelection:
    try:
        arguments = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError as exc:
        raise ToolValidationError("Action dispatch arguments are not valid JSON.") from exc
    if not isinstance(arguments, dict):
        raise ToolValidationError(
            "Action dispatch arguments must be an object."
        )
    if set(arguments) == {"profile"}:
        profile = arguments.get("profile")
    elif set(arguments) == {"capability"}:
        # Accept in-flight calls produced from the pre-migration schema.  The
        # provider never sees these legacy values after restart.
        profile = LEGACY_PROFILE_ALIASES.get(arguments.get("capability"))
    else:
        raise ToolValidationError(
            "Action dispatch requires only the profile field."
        )
    if profile not in ACTION_PROFILES:
        raise ToolValidationError("Action dispatch requested an unknown profile.")
    definitions = definitions_for_profile(registry.definitions(), profile)
    if not definitions:
        raise ToolValidationError("No native tools are available for this profile.")
    return ActionSelection(profile=profile, definitions=definitions)


def definitions_for_profile(
    definitions: Sequence[ToolDefinition],
    profile: str,
) -> tuple[ToolDefinition, ...]:
    """Build one bounded native catalog from stable tool metadata.

    The coordinator depends only on this planner, not on concrete tool names.
    MCP/external disclosure can therefore be added as another profile later
    without replacing validation, policy, approval, or execution.
    """

    if profile not in ACTION_PROFILES:
        raise ToolValidationError("Action catalog requested an unknown profile.")
    selected: list[ToolDefinition] = []
    for definition in definitions:
        native_backend = definition.backend in {
            ToolBackend.IN_PROCESS,
            ToolBackend.ISOLATED_PROCESS,
        }
        if profile == "inspect":
            include = native_backend and definition.effect is ToolEffect.READ_ONLY
        elif profile == "automate":
            include = (
                native_backend
                and definition.effect is not ToolEffect.EXTERNAL_SIDE_EFFECT
                and not definition.capability.startswith("system.")
            )
        else:
            include = native_backend and definition.effect is not ToolEffect.EXTERNAL_SIDE_EFFECT
        if include:
            selected.append(definition)
    return tuple(selected)


def conservative_action_hint(
    prompt: str,
    registry: ToolRegistry,
) -> ActionCatalogHint | None:
    """Return a safety-neutral catalog hint only for an unambiguous workflow.

    A hint changes disclosure and latency, never authorization.  The concrete
    tool still traverses registry, schema, policy, approval and executor.  This
    small rule set is intentionally isolated so future MCP/tool discovery can
    add hint providers without changing the coordinator loop.
    """

    if (
        not isinstance(prompt, str)
        or _EXPLANATION_PATTERN.search(prompt)
        or _NEGATED_RUN_PATTERN.search(prompt)
        or not _PYTHON_SCRIPT_PATTERN.search(prompt)
        or not _CREATE_PATTERN.search(prompt)
        or not _RUN_PATTERN.search(prompt)
    ):
        return None
    try:
        definition = registry.resolve("script.create_and_run_python")
    except ToolNotFoundError:
        return None
    return ActionCatalogHint(
        profile="automate",
        reason_code="deterministic_script_create_run_hint",
        definitions=(definition,),
    )
