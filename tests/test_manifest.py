from __future__ import annotations

from pathlib import Path

import pytest

from rigging.errors import ManifestError
from rigging.manifest import load_manifest


def test_example_manifest_loads() -> None:
    spec = load_manifest("examples/b2b-support/agent.yaml")
    assert spec.name == "b2b-support"
    assert spec.model
    assert spec.limits.max_steps == 20
    assert [g.name for g in spec.guardrails.on_output]
    assert len(spec.spec_hash) == 16


def test_guardrail_shorthand_and_config() -> None:
    spec = load_manifest("examples/b2b-support/agent.yaml")
    consequentials = spec.guardrails.on_tool_call
    assert consequentials[0].name == "consequential_action"
    assert consequentials[0].config["max_amount_usd"] == 500


def test_defaults_merge(tmp_path: Path) -> None:
    defaults = tmp_path / "harness.yaml"
    defaults.write_text("model: qwen3:8b\nlimits:\n  max_steps: 5\ntools: [a]\n", encoding="utf-8")
    agent = tmp_path / "agent.yaml"
    agent.write_text("name: x\ntools: [b]\nlimits:\n  max_tokens: 100\n", encoding="utf-8")
    spec = load_manifest(agent, defaults_path=defaults)
    assert spec.model == "qwen3:8b"
    assert spec.limits.max_steps == 5
    assert spec.limits.max_tokens == 100
    assert spec.tools == ["b"]  # lists replace, they do not concatenate


def test_invalid_manifest_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\nlimits:\n  max_steps: 0\n", encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(bad)
