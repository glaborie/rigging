"""Test doubles, importable by users writing their own harness tests.

FakeClient replays scripted responses in order — dicts or ModelResponse — so a
test never needs a network. The point of the harness contract: any object with
`complete(request) -> response` (and optionally `stream`) is a client.
"""

from __future__ import annotations

from typing import Any

from rigging.types import Message, ModelRequest, ModelResponse, ToolCall, Usage


def _coerce(response: ModelResponse | dict[str, Any]) -> ModelResponse:
    if isinstance(response, ModelResponse):
        return response
    return ModelResponse(
        message=Message(role="assistant", content=response.get("content") or "",
                        reasoning=response.get("reasoning"),
                        tool_calls=[ToolCall(**c) for c in response.get("tool_calls") or []]),
        usage=Usage(input_tokens=10, output_tokens=5),
        finish_reason="stop", model="fake",
    )


class FakeClient:
    """Replays scripted responses in order and records every request."""

    name = "fake"

    def __init__(self, responses: list[ModelResponse | dict[str, Any]]) -> None:
        self.responses = [_coerce(r) for r in responses]
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.responses.pop(0)

    def stream(self, request: ModelRequest, on_error: Any = None) -> Any:  # pragma: no cover
        raise NotImplementedError
