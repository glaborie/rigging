"""Exporters: OTel `gen_ai.*` spans, Langfuse first (ADR-0012).

Three implementations, one protocol:
  * `ConsoleExporter` — no dependencies, useful for wiring and tests.
  * `langfuse_exporter()` — OTLP/HTTP with `gen_ai.*` semantic conventions,
    behind the `[langfuse]` extra. Works against Langfuse Cloud or self-hosted.
  * `MLflowExporter` — one rigging run per MLflow run over the tracking REST API
    (ADR-0020), so it needs no SDK and no extra.

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
import time
from typing import Any

from rigging.config import settings


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
        # Precedence: explicit argument → real environment → .env (via Settings) →
        # LANGFUSE_BASE_URL (older name) → cloud default. The Settings hop is what
        # makes `harness run` work without `set -a; . ./.env` in the shell.
        base = (endpoint
                or os.environ.get("LANGFUSE_HOST")
                or settings.langfuse_host
                or os.environ.get("LANGFUSE_BASE_URL")
                or "https://cloud.langfuse.com")
        base = base.rstrip("/")
        if not base.endswith("/api/public/otel/v1/traces"):
            base = f"{base}/api/public/otel/v1/traces"
        self.endpoint = base
        pk = public_key or os.environ.get("LANGFUSE_PUBLIC_KEY") or settings.langfuse_public_key
        sk = secret_key or os.environ.get("LANGFUSE_SECRET_KEY") or settings.langfuse_secret_key
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


class MLflowExporter:
    """One rigging run becomes one MLflow run (ADR-0020).

    The root span creates the run and finishes it; child spans are aggregated into
    metrics rather than stored, because MLflow is an experiment tracker, not a
    trace viewer. Speaks the documented `2.0` tracking REST API over httpx, so
    there is no `mlflow` dependency to install.
    """

    name = "mlflow"

    def __init__(self, *, tracking_uri: str | None = None, experiment_id: str | None = None,
                 token: str | None = None, run_name: str = "rigging",
                 transport: Any = None, timeout: float = 10.0) -> None:
        self.tracking_uri = (
            tracking_uri or settings.mlflow_tracking_uri or "http://localhost:5000"
        ).rstrip("/")
        self.experiment_id = str(experiment_id or settings.mlflow_experiment_id or "0")
        self.run_name = run_name
        self._token = token or settings.mlflow_tracking_token
        self._timeout = timeout
        self._transport = transport
        self._client: Any = None
        self._run_id: str | None = None
        self._started_ms = int(time.time() * 1000)
        self._chat_calls = 0
        self._tool_calls = 0
        self._input_tokens = 0
        self._output_tokens = 0

    # ------------------------------------------------------------------ http

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        import httpx

        if self._client is None:
            headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
            self._client = httpx.Client(base_url=self.tracking_uri, headers=headers,
                                        timeout=self._timeout, transport=self._transport)
        response = self._client.post(path, json=payload)
        if response.status_code != 200:
            raise RuntimeError(
                f"mlflow {path} failed: status={response.status_code} body={response.text[:200]}"
            )
        body: dict[str, Any] = response.json() if response.content else {}
        return body

    # -------------------------------------------------------------- exporter

    def start_span(self, name: str, attributes: dict[str, Any]) -> Any:
        span = {"name": name, "attributes": dict(attributes), "started": time.time()}
        if self._run_id is None:
            tags = [
                {"key": "session.id", "value": str(attributes.get("session.id", ""))},
                {"key": "rigging.run_id", "value": str(attributes.get("rigging.run_id", ""))},
                {"key": "gen_ai.agent.name", "value": str(attributes.get("gen_ai.agent.name", ""))},
                {"key": "gen_ai.request.model", "value": str(attributes.get("gen_ai.request.model", ""))},
            ]
            body = self._post("/api/2.0/mlflow/runs/create", {
                "experiment_id": self.experiment_id,
                "run_name": self.run_name,
                "start_time": self._started_ms,
                "tags": [t for t in tags if t["value"]],
            })
            self._run_id = body.get("run", {}).get("info", {}).get("run_id")
            if not self._run_id:
                raise RuntimeError(f"mlflow runs/create returned no run_id: {body}")
        return span

    def end_span(self, span: Any, attributes: dict[str, Any] | None = None) -> None:
        merged = {**span.get("attributes", {}), **(attributes or {})}
        name = span.get("name", "")
        if name != "invoke_agent":
            if name == "chat":
                self._chat_calls += 1
            elif name == "execute_tool":
                self._tool_calls += 1
            self._input_tokens += int(merged.get("gen_ai.usage.input_tokens") or 0)
            self._output_tokens += int(merged.get("gen_ai.usage.output_tokens") or 0)
            return
        self._finish(merged, span.get("started", time.time()))

    def _finish(self, attributes: dict[str, Any], started: float) -> None:
        if self._run_id is None:
            return
        now_ms = int(time.time() * 1000)
        status = str(attributes.get("rigging.status", "ok"))
        metrics = [
            {"key": "gen_ai.usage.input_tokens", "value": float(self._input_tokens), "timestamp": now_ms},
            {"key": "gen_ai.usage.output_tokens", "value": float(self._output_tokens), "timestamp": now_ms},
            {"key": "rigging.steps", "value": float(attributes.get("rigging.steps") or 0), "timestamp": now_ms},
            {"key": "rigging.latency_s", "value": round(time.time() - started, 3), "timestamp": now_ms},
            {"key": "rigging.chat_calls", "value": float(self._chat_calls), "timestamp": now_ms},
            {"key": "rigging.tool_calls", "value": float(self._tool_calls), "timestamp": now_ms},
        ]
        cost = attributes.get("rigging.cost_usd")
        if cost is not None:
            metrics.append({"key": "rigging.cost_usd", "value": float(cost), "timestamp": now_ms})
        params = [
            {"key": "gen_ai.request.model", "value": str(attributes.get("gen_ai.request.model", ""))},
            {"key": "rigging.spec_hash", "value": str(attributes.get("rigging.spec_hash", ""))},
        ]
        self._post("/api/2.0/mlflow/runs/log-batch", {
            "run_id": self._run_id,
            "metrics": metrics,
            "params": [p for p in params if p["value"]],
            "tags": [{"key": "rigging.status", "value": status}],
        })
        self._post("/api/2.0/mlflow/runs/update", {
            "run_id": self._run_id,
            "status": "FINISHED" if status == "ok" else "FAILED",
            "end_time": now_ms,
        })

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


EXPORTERS: dict[str, Any] = {
    "console": ConsoleExporter,
    "langfuse": langfuse_exporter,
    "mlflow": MLflowExporter,
}
