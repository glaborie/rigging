"""The v0.1 exit criterion in miniature: the core tools are registered, their
JSON Schema is wire-shaped, and both run deterministically."""

from __future__ import annotations

import hashlib
from datetime import datetime

from rigging.registry import DEFAULT_REGISTRY
from rigging.tools.schema import FunctionTool


def test_core_tools_are_registered() -> None:
    import rigging.tools.core  # noqa: F401  (registration side-effect)

    for name in ("current_time", "sha256_hex"):
        assert name in DEFAULT_REGISTRY.names("tool"), name


def test_schemas_are_wire_shaped() -> None:
    import rigging.tools.core  # noqa: F401

    schema = FunctionTool(DEFAULT_REGISTRY.get("tool", "sha256_hex")).json_schema
    fn = schema["function"]
    assert fn["name"] == "sha256_hex"
    assert fn["parameters"]["required"] == ["text"]
    assert fn["parameters"]["properties"]["text"]["type"] == "string"


def test_current_time_and_sha256_run() -> None:
    import rigging.tools.core  # noqa: F401

    now = FunctionTool(DEFAULT_REGISTRY.get("tool", "current_time"))
    result = now.run(type("C", (), {"id": "1", "name": "current_time",
                                    "arguments": {"tz": "Europe/Zurich"}})())  # type: ignore[arg-type]
    assert "T" in result.content  # ISO 8601
    assert datetime.fromisoformat(result.content).utcoffset() is not None

    digest = FunctionTool(DEFAULT_REGISTRY.get("tool", "sha256_hex"))
    result = digest.run(type("C", (), {"id": "2", "name": "sha256_hex",
                                       "arguments": {"text": "rigging"}})())  # type: ignore[arg-type]
    assert result.content == hashlib.sha256(b"rigging").hexdigest()

    bad = digest.run(type("C", (), {"id": "3", "name": "sha256_hex",
                                    "arguments": {"wrong": "arg"}})())  # type: ignore[arg-type]
    assert bad.is_error  # bad args from the model become a tool message, never a crash
