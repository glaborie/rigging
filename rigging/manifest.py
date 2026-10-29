"""Manifest loading and validation (ADR-0015).

Flat YAML, single-level defaults merge, JSON Schema validation, no inheritance.
Assembly resolves names against the registry and fails fast on unknowns.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from rigging.errors import ManifestError

SCHEMA_PATH = Path(__file__).resolve().parent / "schema" / "agent.schema.json"


class GuardrailRef(BaseModel):
    name: str
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _from_str(cls, value: Any) -> Any:
        return {"name": value} if isinstance(value, str) else value


class Guardrails(BaseModel):
    on_input: list[GuardrailRef] = Field(default_factory=list)
    on_tool_call: list[GuardrailRef] = Field(default_factory=list)
    on_tool_result: list[GuardrailRef] = Field(default_factory=list)
    on_output: list[GuardrailRef] = Field(default_factory=list)


class ApprovalCfg(BaseModel):
    policy: Literal["cli", "scripted", "auto_deny"] = "auto_deny"
    side_effecting_tools: list[str] = Field(default_factory=list)


class Limits(BaseModel):
    max_steps: int = 20
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    max_wall_clock_s: float | None = None


class RetrieverCfg(BaseModel):
    name: str = "fts5"
    options: dict[str, Any] = Field(default_factory=dict)


class ObservabilityCfg(BaseModel):
    exporters: list[str] = Field(default_factory=list)
    capture_content: bool = True


class McpServerCfg(BaseModel):
    name: str
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    transport: Literal["stdio", "http", "sse"] = "stdio"


class AgentSpec(BaseModel):
    """The whole behaviour of one agent, in one file."""

    name: str = "agent"
    description: str = ""
    model: str
    credential_profile: str = "ollama"
    system_prompt: str = ""
    system_prompt_file: str | None = None
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    skills_dir: str = "skills"
    retriever: RetrieverCfg | None = None
    guardrails: Guardrails = Field(default_factory=Guardrails)
    approval: ApprovalCfg = Field(default_factory=ApprovalCfg)
    limits: Limits = Field(default_factory=Limits)
    observability: ObservabilityCfg = Field(default_factory=ObservabilityCfg)
    mcp_servers: list[McpServerCfg] = Field(default_factory=list)

    # provenance, not authorable
    source_path: str = ""
    spec_hash: str = ""

    def prompt(self) -> str:
        if self.system_prompt_file:
            p = Path(self.source_path).parent / self.system_prompt_file
            if not p.exists():
                raise ManifestError(f"system_prompt_file not found: {p}")
            return p.read_text(encoding="utf-8")
        return self.system_prompt


def _validate_schema(raw: dict[str, Any], path: Path) -> None:
    """JSON Schema first (editor-facing errors), pydantic second (typed access)."""
    try:
        import jsonschema
    except ImportError:  # jsonschema is a dev extra; pydantic still validates
        return
    if not SCHEMA_PATH.exists():
        return
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(raw), key=lambda e: list(e.path))
    if errors:
        lines = [f"{path}: {'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors]
        raise ManifestError("invalid manifest:\n  " + "\n  ".join(lines))


def _merge(defaults: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    """Single-level merge: lists replace, dicts merge one level deep."""
    out = dict(defaults)
    for key, value in raw.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **value}
        else:
            out[key] = value
    return out


def load_manifest(path: str | Path, *, defaults_path: str | Path | None = None) -> AgentSpec:
    path = Path(path)
    if not path.exists():
        raise ManifestError(f"manifest not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ManifestError(f"{path}: manifest must be a mapping")

    if defaults_path:
        dpath = Path(defaults_path)
        if not dpath.exists():
            raise ManifestError(f"defaults not found: {dpath}")
        raw = _merge(yaml.safe_load(dpath.read_text(encoding="utf-8")) or {}, raw)

    _validate_schema(raw, path)

    spec = AgentSpec(**raw)
    spec.source_path = str(path.resolve())
    payload = spec.model_dump(exclude={"source_path", "spec_hash"})
    spec.spec_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    return spec
