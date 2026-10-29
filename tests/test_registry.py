from __future__ import annotations

import pytest

from rigging.errors import CapabilityNotFound
from rigging.registry import Registry
from rigging.tools.schema import FunctionTool


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
