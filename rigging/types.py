"""Canonical types.

These are the only shapes the loop knows about. Adapters translate vendor wire
formats into these; nothing in the core imports a vendor.

Reasoning is a first-class field (ADR-0002): reasoning models bill for it, so
dropping it corrupts usage accounting and destroys the best debugging artifact.
"""

from __future__ import annotations

import json
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]


class ToolCall(BaseModel):
    """A tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_arguments: str | None = None  # as sent by the model, for malformed-args forensics


class ToolResult(BaseModel):
    """The outcome of a tool call, always sendable back to the model."""

    call_id: str
    name: str
    content: str
    is_error: bool = False
    denied: bool = False
    duration_ms: int = 0

    @classmethod
    def error(cls, call: ToolCall, message: str, *, denied: bool = False) -> ToolResult:
        return cls(call_id=call.id, name=call.name, content=message, is_error=True, denied=denied)


class Message(BaseModel):
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    reasoning: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """OpenAI chat-completions shape. Reasoning is deliberately not re-sent."""
        msg: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.raw_arguments or json.dumps(c.arguments)},
                }
                for c in self.tool_calls
            ]
            msg["content"] = self.content or None
        if self.tool_call_id:
            msg["tool_call_id"] = self.tool_call_id
        if self.name:
            msg["name"] = self.name
        return msg


class Usage(BaseModel):
    """Token accounting as reported by the gateway. Never estimated locally (ADR-0011)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )


class ModelRequest(BaseModel):
    model: str
    messages: list[Message]
    tools: list[dict[str, Any]] = Field(default_factory=list)  # JSON Schema, already wire-shaped
    temperature: float | None = None
    max_tokens: int | None = None
    stop: list[str] = Field(default_factory=list)


class ModelDelta(BaseModel):
    """One streamed chunk, already normalised."""

    text: str = ""
    reasoning: str = ""
    tool_call_delta: dict[str, Any] | None = None
    finish_reason: str | None = None
    usage: Usage | None = None


class ModelResponse(BaseModel):
    message: Message
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str | None = None
    model: str = ""
    latency_ms: int = 0

    @property
    def tool_calls(self) -> list[ToolCall]:
        return self.message.tool_calls


class Chunk(BaseModel):
    """A retrieved document fragment."""

    id: str
    text: str
    source: str = ""
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class Handoff(BaseModel):
    """Structured escalation payload (ADR-0007)."""

    reason: str
    summary: str
    facts: dict[str, Any] = Field(default_factory=dict)
    transcript_run_id: str | None = None
    created_at: float = Field(default_factory=time.time)


class RunResult(BaseModel):
    run_id: str
    status: Literal["ok", "budget_exhausted", "denied", "error"]
    output: str = ""
    handoff: Handoff | None = None
    usage: Usage = Field(default_factory=Usage)
    steps: int = 0
    cost_usd: float | None = None
    error: str | None = None
