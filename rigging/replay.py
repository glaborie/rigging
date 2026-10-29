"""Replay: re-run a recorded transcript through the loop with zero network.

`EventLog.export_jsonl()` writes the transcript; `ReplayClient` consumes it
(A DR-0009: no live API in tests/CI — eval and CI replay recordings instead).
On mismatch, a `replay_miss` event is the signal, not a silent substitute.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rigging.types import Message, ModelRequest, ModelResponse, ToolCall, Usage


class ReplayClient:
    """Serves recorded `model_response` events, in order, without a network."""

    def __init__(self, path: str | Path) -> None:
        self.responses: list[ModelResponse] = []
        self.meta: dict[str, Any] = {}
        with Path(path).open("r", encoding="utf-8") as fh:
            for line in fh:
                row = json.loads(line)
                if row.get("type") == "run":
                    self.meta = row.get("run", {})
                    continue
                if row.get("type") == "model_response":
                    p = row["payload"]
                    self.responses.append(ModelResponse(
                        message=Message(role="assistant", content=p.get("content") or "",
                                        reasoning=p.get("reasoning"),
                                        tool_calls=[ToolCall(**c) for c in p.get("tool_calls") or []]),
                        usage=Usage(**p.get("usage") or {}),
                        finish_reason=p.get("finish_reason") or "stop",
                    ))
        if not self.responses:
            raise ValueError(f"{path}: no model_response events to replay")

    def complete(self, request: ModelRequest) -> ModelResponse:
        if not self.responses:
            raise RuntimeError(
                "replay exhausted: the transcript has fewer model turns than this run needs. "
                "The manifest, prompt or tools changed since the recording.")
        return self.responses.pop(0)
