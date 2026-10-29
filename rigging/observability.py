"""Exporters: OTel `gen_ai.*` spans, Langfuse first (ADR-0012).

Status: v0.4. `ConsoleExporter` is trivially usable today and exists mainly so the
`Exporter` protocol has a working implementation to test the loop against.
"""

from __future__ import annotations

from typing import Any


class ConsoleExporter:
    """Prints span start/end. Deliberately boring; useful for wiring and tests."""

    name = "console"

    def start_span(self, name: str, attributes: dict[str, Any]) -> Any:
        print(f"[span] {name} {attributes}")
        return {"name": name, "attributes": dict(attributes)}

    def end_span(self, span: Any, attributes: dict[str, Any] | None = None) -> None:
        print(f"[span:end] {span['name']} {attributes or {}}")

    def close(self) -> None:
        return None


def langfuse_exporter(*, endpoint: str, headers: dict[str, str], service_name: str = "rigging") -> Any:
    """OTLP/HTTP exporter with `gen_ai.*` attributes; requires the `[langfuse]` extra."""
    from rigging.errors import RiggingError

    raise RiggingError("langfuse exporter lands in v0.4 (extra: rigging[langfuse])")
