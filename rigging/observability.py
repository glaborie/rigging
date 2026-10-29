"""Exporters: OTel `gen_ai.*` spans, Langfuse first (ADR-0012).

Two implementations, one protocol:
  * `ConsoleExporter` — no dependencies, useful for wiring and tests.
  * `langfuse_exporter()` — OTLP/HTTP with `gen_ai.*` semantic conventions,
    behind the `[langfuse]` extra. Works against Langfuse Cloud or self-hosted.

Langfuse specific gotchas, learned the hard way (they cost hours once):
  * the OTLP path is `/api/public/otel/v1/traces` on self-hosted and
    `/api/public/otel/v1/traces` on Cloud EU/US too — not `/v1/traces`;
  * auth is HTTP Basic with `public_key:secret_key`, base64-encoded;
  * send `x-langfuse-ingestion-version: 4` or traces land in the legacy pipeline;
  * a span's gen_ai.* attributes are what produce model/usage columns in the UI;
    `session.id` on the root span is what groups multi-turn conversations.
"""

from __future__ import annotations

import base64
import os
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


class LangfuseExporter:
    """OTLP/HTTP exporter with OTel `gen_ai.*` attributes."""

    name = "langfuse"

    def __init__(self, *, endpoint: str | None = None, public_key: str | None = None,
                 secret_key: str | None = None, service_name: str = "rigging",
                 capture_content: bool = True) -> None:
        self.capture_content = capture_content
        base = endpoint or os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")
        base = base.rstrip("/")
        if not base.endswith("/api/public/otel/v1/traces"):
            base = f"{base}/api/public/otel/v1/traces"
        self.endpoint = base
        pk = public_key or os.environ.get("LANGFUSE_PUBLIC_KEY", "")
        sk = secret_key or os.environ.get("LANGFUSE_SECRET_KEY", "")
        if not pk or not sk:
            raise ValueError("LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are not set")
        self._auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()
        self._provider: Any = None
        self._tracer: Any = None
        self._service_name = service_name
        self._stack: list[Any] = []  # span stack: children must nest under the run

    def _tracer_obj(self) -> Any:
        if self._tracer is None:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
                from opentelemetry.sdk.resources import Resource
                from opentelemetry.sdk.trace import TracerProvider
                from opentelemetry.sdk.trace.export import BatchSpanProcessor
            except ModuleNotFoundError as exc:  # pragma: no cover - needs the extra
                raise RuntimeError("install rigging[langfuse] for the OTLP exporter") from exc
            self._provider = TracerProvider(resource=Resource.create(
                {"service.name": self._service_name, "service.version": "rigging"},
            ))
            self._provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
                endpoint=self.endpoint,
                headers={"Authorization": f"Basic {self._auth}",
                         "x-langfuse-ingestion-version": "4"},
            )))
            self._tracer = self._provider.get_tracer("rigging")
        return self._tracer

    def start_span(self, name: str, attributes: dict[str, Any]) -> Any:
        # Nest under the enclosing span, or Langfuse shows a trace per span instead
        # of one trace with observations. `session.id` on the root groups turns.
        from opentelemetry import trace as ot_trace

        tracer = self._tracer_obj()
        ctx = ot_trace.set_span_in_context(self._stack[-1]) if self._stack else None
        span = tracer.start_span(name, context=ctx,
                                 attributes={k: v for k, v in attributes.items() if v is not None})
        self._stack.append(span)
        return span

    def end_span(self, span: Any, attributes: dict[str, Any] | None = None) -> None:
        for key, value in (attributes or {}).items():
            if value is not None:
                span.set_attribute(key, value)
        span.end()
        if self._stack and self._stack[-1] is span:
            self._stack.pop()

    def close(self) -> None:
        if self._provider is not None:
            self._provider.shutdown()  # flushes the batch processor


def langfuse_exporter(*, endpoint: str | None = None, headers: dict[str, str] | None = None,
                      service_name: str = "rigging", capture_content: bool = True,
                      public_key: str | None = None, secret_key: str | None = None) -> LangfuseExporter:
    """Factory kept for the documented manifest exporter name."""
    return LangfuseExporter(endpoint=endpoint, public_key=public_key, secret_key=secret_key,
                            service_name=service_name, capture_content=capture_content)


EXPORTERS: dict[str, Any] = {"console": ConsoleExporter, "langfuse": langfuse_exporter}
