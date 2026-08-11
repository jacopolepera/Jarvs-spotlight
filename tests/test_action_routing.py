import pytest

from core.agent.action_routing import (
    ACTION_DISPATCH_PROVIDER_NAME,
    action_dispatch_tool,
    conservative_action_hint,
    parse_action_selection,
)
from core.agent.script_tools import register_script_tools
from core.agent.system_tools import register_system_tools
from core.agent.tooling import ToolRegistry, ToolValidationError
from core.agent.workspace_tools import register_workspace_tools


def registry():
    value = ToolRegistry()
    register_workspace_tools(value)
    register_script_tools(value)
    register_system_tools(value)
    value.freeze()
    return value


def test_dispatch_schema_is_constant_and_bounded():
    tool = action_dispatch_tool()
    function = tool["function"]
    assert function["name"] == ACTION_DISPATCH_PROVIDER_NAME
    assert function["parameters"]["required"] == ["profile"]
    assert function["parameters"]["properties"]["profile"]["enum"] == [
        "inspect",
        "automate",
        "system",
    ]
    assert function["parameters"]["additionalProperties"] is False


def test_automate_profile_covers_composite_file_and_script_tasks():
    selected = parse_action_selection('{"profile":"automate"}', registry())
    names = {definition.name for definition in selected.definitions}
    assert "workspace.read_text" in names
    assert "workspace.write_text" in names
    assert "script.save_python" in names
    assert "script.run_python" in names
    assert "script.create_and_run_python" in names
    assert "system.run_command" not in names


def test_inspect_profile_is_read_only_and_system_profile_can_execute():
    inspected = parse_action_selection('{"profile":"inspect"}', registry())
    assert {definition.effect.value for definition in inspected.definitions} == {
        "read_only"
    }
    system = parse_action_selection('{"profile":"system"}', registry())
    assert "system.run_command" in {
        definition.name for definition in system.definitions
    }


def test_deprecated_script_dispatch_is_normalized_to_automate():
    selected = parse_action_selection('{"capability":"script"}', registry())
    assert selected.profile == "automate"
    assert "script.run_python" in {
        definition.name for definition in selected.definitions
    }


def test_conservative_hint_selects_only_composite_python_create_and_run():
    hint = conservative_action_hint(
        "Create a Python script named hello.py that prints hello, then run it.",
        registry(),
    )

    assert hint is not None
    assert hint.profile == "automate"
    assert hint.reason_code == "deterministic_script_create_run_hint"
    assert [definition.name for definition in hint.definitions] == [
        "script.create_and_run_python"
    ]


@pytest.mark.parametrize(
    "prompt",
    [
        "Hello",
        "Explain how to create and run a Python script",
        "Explain how to create and run a Python script",
        "Create a Python script but do not run it",
        "Create a text file and then read it",
        "Run the existing Python script",
    ],
)
def test_conservative_hint_does_not_capture_ambiguous_or_non_matching_prompts(prompt):
    assert conservative_action_hint(prompt, registry()) is None


@pytest.mark.parametrize(
    "arguments",
    [
        "{}",
        '{"profile":"unknown"}',
        '{"profile":"automate","extra":true}',
        "not-json",
    ],
)
def test_invalid_dispatch_fails_closed(arguments):
    with pytest.raises(ToolValidationError):
        parse_action_selection(arguments, registry())
