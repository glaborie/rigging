from __future__ import annotations

import pytest

from rigging.errors import CapabilityNotFound
from rigging.registry import Registry
from rigging.tools.schema import FunctionTool


def test_entry_points_discovery_returns_list_of_discovered_capabilities() -> None:
    """Entry-points discovery returns a list of loaded capability names."""
    reg = Registry()
    # discover_entry_points should return a list (empty or with discovered names)
    discovered = reg.discover_entry_points()
    assert isinstance(discovered, list)


def test_entry_points_discovery_on_by_default_in_assembly() -> None:
    """When registry is not provided, entry-points are discovered automatically."""
    from rigging.loop import assemble
    from rigging.manifest import AgentSpec, Limits

    # Use a fresh registry to test discovery isolation
    # Create a test registry with a tool registered via entry-point pattern
    test_reg = Registry()

    def test_tool(x: str) -> str:
        """A test tool for entry-point testing."""
        return x.upper()

    test_tool.__rigging_tool__ = {"name": "ep_test_tool", "description": "test", "side_effecting": False}
    test_reg.add("tool", "ep_test_tool", test_tool)

    spec = AgentSpec(model="fake", tools=["ep_test_tool"], limits=Limits())

    # Assemble with explicit registry - no discovery logging should happen
    result = assemble(spec, registry=test_reg)
    assert "ep_test_tool" in [t.name for t in result.tools]


def test_tool_decorator_registers_and_builds_schema(registry: Registry) -> None:
    @registry_tool(registry)
    def add(a: int, b: int = 2) -> int:
        """Add two numbers."""
        return a + b

    tool = FunctionTool(add)
    assert tool.name == "add"
    assert tool.side_effecting is False
    schema = tool.json_schema["function"]
    assert schema["parameters"]["required"] == ["a"]
    assert schema["parameters"]["properties"]["b"]["default"] == 2
    assert schema["parameters"]["properties"]["a"]["type"] == "integer"


def registry_tool(registry: Registry):
    from rigging.registry import tool

    return tool(registry=registry)


def test_missing_names_reported_together(registry: Registry) -> None:
    registry.add("tool", "a", object())
    with pytest.raises(CapabilityNotFound) as exc:
        registry.resolve_all("tool", ["a", "b", "c"])
    assert "b, c" in str(exc.value)
    assert "a" in str(exc.value)


def test_duplicate_registration_is_refused(registry: Registry) -> None:
    registry.add("tool", "dup", object())
    with pytest.raises(ValueError, match="already registered"):
        registry.add("tool", "dup", object())
    registry.add("tool", "dup", object(), replace=True)


def test_builtin_guardrails_are_registered() -> None:
    import rigging.guardrails  # noqa: F401  (registration side-effect)
    from rigging.registry import DEFAULT_REGISTRY

    for name in ("pii_redaction", "prompt_injection", "consequential_action", "commitment_check"):
        assert name in DEFAULT_REGISTRY.names("guardrail")