"""Turn a plain function into a Tool and a wire-shaped JSON Schema.

Scope note (documented limitation): annotations map to a small JSON Schema subset
(str/int/float/bool/list/dict/Optional/enum). Anything richer should be authored as
an explicit `Tool` implementation with a hand-written `json_schema`.
"""

from __future__ import annotations

import enum
import inspect
import typing
from collections.abc import Callable, Sequence
from typing import Any

from rigging.types import ToolCall, ToolResult

_PRIMITIVES: dict[Any, str] = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _json_type(annotation: Any) -> dict[str, Any]:
    origin = typing.get_origin(annotation)
    if annotation in _PRIMITIVES:
        return {"type": _PRIMITIVES[annotation]}
    if annotation is list or origin in (list, Sequence):
        item_args = typing.get_args(annotation)
        item = _json_type(item_args[0]) if item_args else {}
        return {"type": "array", "items": item or {}}
    if annotation is dict or origin is dict:
        return {"type": "object"}
    if inspect.isclass(annotation) and issubclass(annotation, enum.Enum):
        return {"type": "string", "enum": [m.value for m in annotation]}
    if origin is typing.Union:
        union_args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(union_args) == 1:
            return _json_type(union_args[0])
        return {"anyOf": [_json_type(a) for a in union_args]}
    return {}


def schema_from_callable(fn: Callable[..., Any]) -> dict[str, Any]:
    """Build an OpenAI function schema from a decorated function's signature."""
    meta = getattr(fn, "__rigging_tool__", {})
    try:  # PEP 563: annotations arrive as strings; resolve them once
        hints = typing.get_type_hints(fn)
    except Exception:
        hints = {}
    signature = inspect.signature(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in signature.parameters.items():
        annotation: Any = hints.get(name, param.annotation)
        entry = _json_type(annotation if annotation is not inspect.Parameter.empty else str)
        if param.default is not inspect.Parameter.empty:
            entry["default"] = param.default
        else:
            required.append(name)
        properties[name] = entry
    return {
        "type": "function",
        "function": {
            "name": meta.get("name") or getattr(fn, "__name__", "tool"),
            "description": meta.get("description") or (inspect.getdoc(fn) or "").split("\n\n")[0],
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


class FunctionTool:
    """Adapter: a decorated function that satisfies the `Tool` protocol."""

    def __init__(self, fn: Callable[..., Any]) -> None:
        meta = getattr(fn, "__rigging_tool__", {})
        self.fn = fn
        self.name: str = str(meta.get("name") or getattr(fn, "__name__", "tool"))
        self.description: str = str(meta.get("description") or (inspect.getdoc(fn) or ""))
        self.side_effecting: bool = bool(meta.get("side_effecting", False))
        self.json_schema = schema_from_callable(fn)

    def run(self, call: ToolCall) -> ToolResult:
        import time

        started = time.perf_counter()
        try:
            result = self.fn(**call.arguments)
        except TypeError as exc:  # bad args from the model
            return ToolResult.error(call, f"invalid arguments: {exc}")
        except Exception as exc:  # any tool failure, never a crash
            return ToolResult.error(call, f"{type(exc).__name__}: {exc}")
        text = result if isinstance(result, str) else repr(result)
        return ToolResult(call_id=call.id, name=self.name, content=text,
                          duration_ms=int((time.perf_counter() - started) * 1000))

    def __repr__(self) -> str:
        return f"<FunctionTool {self.name}{' side_effecting' if self.side_effecting else ''}>"


def wire_tools(tools: Sequence[Any]) -> list[dict[str, Any]]:
    return [t.json_schema for t in tools]
