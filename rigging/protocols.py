"""Every swappable interface in the harness.

Hard rule: this module imports nothing from the rest of the package (only
`rigging.types`). An implementation of any protocol must be writable and testable
without the loop, the registry or the CLI. If a protocol needs something else from
the core to make sense, the protocol is wrong.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from rigging.types import (
    Chunk,
    Message,
    ModelDelta,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolResult,
)

# --------------------------------------------------------------------------- model access

@runtime_checkable
class LLMClient(Protocol):
    """Gateway adapter. One implementation ships: OpenAICompatClient."""

    name: str

    def stream(self, request: ModelRequest) -> Iterator[ModelDelta]: ...

    def complete(self, request: ModelRequest) -> ModelResponse: ...


# --------------------------------------------------------------------------- tools

@runtime_checkable
class Tool(Protocol):
    """A capability the model can call.

    `side_effecting` is metadata the *guardrails and approval policy consume*; the
    tool itself is never trusted to enforce its own consequences (ADR-0007).
    """

    name: str
    description: str
    json_schema: dict[str, Any]
    side_effecting: bool

    def run(self, call: ToolCall) -> ToolResult: ...


@runtime_checkable
class ApprovalPolicy(Protocol):
    """Decides whether a side-effecting tool call may execute."""

    name: str

    def request(self, call: ToolCall, tool: Tool, reason: str) -> tuple[bool, str]:
        """Return (approved, note). Never raises: denial is a normal outcome."""
        ...


# --------------------------------------------------------------------------- knowledge

@dataclass(frozen=True)
class SkillMeta:
    name: str
    description: str
    path: str


@runtime_checkable
class Skill(Protocol):
    meta: SkillMeta
    body: str


@runtime_checkable
class SkillResolver(Protocol):
    """Progressive disclosure: `discover()` is cheap and index-only; `load()` is on demand."""

    def discover(self) -> Sequence[SkillMeta]: ...

    def load(self, name: str) -> Skill: ...


@runtime_checkable
class Retriever(Protocol):
    """Retrieval as a tool backend (ADR-0008). Swapping Qdrant for anything is one class."""

    name: str
    requires_indexing: bool

    def search(self, query: str, k: int = 5) -> list[Chunk]: ...

    def index(self, documents: Sequence[Chunk]) -> int:
        """Upsert and return the number of documents indexed."""
        ...


# --------------------------------------------------------------------------- safety

@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class Deny:
    reason: str


@dataclass(frozen=True)
class Transform:
    """Replacement payload: rewritten text (input/output) or rewritten args (tool call)."""

    value: Any
    note: str = ""


Verdict = Allow | Deny | Transform
HookName = Literal["on_input", "on_tool_call", "on_tool_result", "on_output"]


@runtime_checkable
class Guardrail(Protocol):
    """Four named hooks, chain-composed (ADR-0004). First Deny wins."""

    name: str
    hooks: Sequence[HookName]

    def on_input(self, messages: Sequence[Message]) -> Verdict: ...

    def on_tool_call(self, call: ToolCall, tool: Tool) -> Verdict: ...

    def on_tool_result(self, call: ToolCall, content: str) -> Verdict:
        """Untrusted content (retrieval, MCP, external APIs) is inspected here."""
        ...

    def on_output(self, text: str, messages: Sequence[Message]) -> Verdict: ...


@runtime_checkable
class DecisionModel(Protocol):
    """Typed questions in, calibrated probabilities out (ADR-0009). Advisory only in v1."""

    name: str

    def decide(self, state: str, questions: dict[str, Sequence[str]]) -> dict[str, DecisionAnswer]: ...


@dataclass(frozen=True)
class DecisionAnswer:
    question: str
    choice: str
    probability: float
    distribution: dict[str, float]


# --------------------------------------------------------------------------- observability

@runtime_checkable
class Exporter(Protocol):
    """OTel gen_ai.* spans. Langfuse first, MLflow second (ADR-0012)."""

    name: str

    def start_span(self, name: str, attributes: dict[str, Any]) -> Any: ...

    def end_span(self, span: Any, attributes: dict[str, Any] | None = None) -> None: ...

    def close(self) -> None: ...


# --------------------------------------------------------------------------- scale-out (v0.6)

@runtime_checkable
class SubagentRunner(Protocol):
    """Not implemented in v1. The slot and `parent_run_id` exist so it stays additive."""

    def spawn(self, manifest_path: str, task: str, *, parent_run_id: str) -> Any: ...
