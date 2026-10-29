"""Capability registry: decorators now, entry points later (ADR-0003).

The registry is the whole assembly mechanism: a manifest names capabilities, the
registry resolves them, and assembly fails fast with the full list of unknown
names rather than skipping quietly.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from rigging.errors import CapabilityNotFound
from rigging.protocols import DecisionModel, Guardrail, Retriever, Tool

T = TypeVar("T")

KINDS = ("tool", "guardrail", "retriever", "decision_model", "approval_policy", "exporter")


class Registry:
    """name -> implementation, per capability kind."""

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {kind: {} for kind in KINDS}

    # ---------------------------------------------------------------- mutation

    def add(self, kind: str, name: str, impl: Any, *, replace: bool = False) -> None:
        name = str(name)
        if kind not in self._items:
            raise ValueError(f"unknown capability kind: {kind}")
        if name in self._items[kind] and not replace:
            raise ValueError(f"{kind} {name!r} is already registered; pass replace=True to override")
        self._items[kind][name] = impl

    # ---------------------------------------------------------------- query

    def get(self, kind: str, name: str) -> Any:
        try:
            return self._items[kind][name]
        except KeyError:
            raise CapabilityNotFound(kind, [name], list(self._items[kind])) from None

    def names(self, kind: str) -> list[str]:
        return sorted(self._items[kind])

    def resolve_all(self, kind: str, names: Iterable[str]) -> list[Any]:
        """Resolve every name or raise once with the complete list of unknowns."""
        missing = [n for n in names if n not in self._items[kind]]
        if missing:
            raise CapabilityNotFound(kind, missing, list(self._items[kind]))
        return [self._items[kind][n] for n in names]

    def __contains__(self, item: tuple[str, str]) -> bool:
        kind, name = item
        return name in self._items.get(kind, {})

    def __repr__(self) -> str:
        inner = ", ".join(f"{k}={len(v)}" for k, v in self._items.items())
        return f"<Registry {inner}>"

    # ---------------------------------------------------------------- discovery

    def discover_entry_points(self, groups: dict[str, str] | None = None) -> list[str]:
        """Load third-party capability packages.

        `groups` maps entry-point group -> capability kind. The callable a package
        exposes must accept a Registry and register into it. Opt-in, and every
        discovery is logged by the caller (implicit magic must be visible).
        """
        from importlib.metadata import entry_points

        groups = groups or {
            "rigging.tools": "tool",
            "rigging.guardrails": "guardrail",
            "rigging.retrievers": "retriever",
            "rigging.decision_models": "decision_model",
        }
        loaded: list[str] = []
        for group, kind in groups.items():
            for ep in entry_points(group=group):
                register = ep.load()
                count_before = len(self._items[kind])
                register(self)
                if len(self._items[kind]) > count_before:
                    loaded.append(f"{kind}:{ep.name}")
        return loaded


DEFAULT_REGISTRY = Registry()


def _name(name: str | None, obj: Any, fallback: str) -> str:
    """Explicit name wins, then the decorated object's __name__, then the kind."""
    return str(name or getattr(obj, "__name__", fallback))


def tool(name: str | None = None, *, description: str | None = None,
         side_effecting: bool = False, registry: Registry | None = None) -> Callable[[T], T]:
    """Register a Tool. The function's signature becomes the JSON Schema.

    The decorator stamps attributes on the function; `rigging.tools.schema` turns
    those into a wire-shaped JSON Schema at assembly time.
    """
    reg = registry or DEFAULT_REGISTRY

    def deco(fn: T) -> T:
        resolved = _name(name, fn, "tool")
        target: Any = fn  # mypy: a TypeVar cannot be known to carry our attribute
        target.__rigging_tool__ = {"name": resolved, "description": description or (fn.__doc__ or "").strip().split("\n\n")[0], "side_effecting": side_effecting}
        reg.add("tool", resolved, fn)
        return fn

    return deco


def guardrail(name: str | None = None, *, registry: Registry | None = None) -> Callable[[T], T]:
    reg = registry or DEFAULT_REGISTRY

    def deco(cls: T) -> T:
        reg.add("guardrail", _name(name, cls, "guardrail"), cls)
        return cls

    return deco


def retriever(name: str | None = None, *, registry: Registry | None = None) -> Callable[[T], T]:
    reg = registry or DEFAULT_REGISTRY

    def deco(cls: T) -> T:
        reg.add("retriever", _name(name, cls, "retriever"), cls)
        return cls

    return deco


def decision_model(name: str | None = None, *, registry: Registry | None = None) -> Callable[[T], T]:
    reg = registry or DEFAULT_REGISTRY

    def deco(cls: T) -> T:
        reg.add("decision_model", _name(name, cls, "decision_model"), cls)
        return cls

    return deco


__all__ = [
    "DEFAULT_REGISTRY",
    "KINDS",
    "Registry",
    "decision_model",
    "guardrail",
    "retriever",
    "tool",
]

# Protocol conformance is asserted in tests/test_registry.py rather than here, so
# that this module never imports an implementation.
_ = (Tool, Guardrail, Retriever, DecisionModel)
