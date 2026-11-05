"""The MLflow exporter (ADR-0020): one run, four HTTP calls, no SDK.

Offline by rule: `httpx.MockTransport` answers the tracking API, so the test
asserts the wire contract without a server or the `mlflow` package.
"""

from __future__ import annotations

import json

import httpx

from rigging.observability import MLflowExporter


def _recorder() -> tuple[list[tuple[str, dict]], httpx.MockTransport]:
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        calls.append((request.url.path, body))
        if request.url.path.endswith("/runs/create"):
            return httpx.Response(200, json={"run": {"info": {"run_id": "ml-1"}}})
        if request.url.path.endswith("/experiments/get"):
            return httpx.Response(200, json={"experiment": {
                "experiment_id": request.url.params.get("experiment_id", "0"),
                "name": "Default", "lifecycle_stage": "active"}})
        return httpx.Response(200, json={})

    return calls, httpx.MockTransport(handler)


def _run(exporter: MLflowExporter, *, status: str = "ok") -> None:
    root = exporter.start_span("invoke_agent", {
        "gen_ai.request.model": "qwen3:8b",
        "gen_ai.agent.name": "demo",
        "session.id": "s_1",
        "rigging.run_id": "r_1",
        "rigging.spec_hash": "abc",
    })
    chat = exporter.start_span("chat", {"gen_ai.request.model": "qwen3:8b"})
    exporter.end_span(chat, {"gen_ai.usage.input_tokens": 10, "gen_ai.usage.output_tokens": 4})
    tool = exporter.start_span("execute_tool", {"gen_ai.tool.name": "sha256_hex"})
    exporter.end_span(tool, {})
    exporter.end_span(root, {"rigging.status": status, "rigging.steps": 2,
                             "rigging.cost_usd": 0.0012})
    exporter.close()


def test_one_run_maps_to_create_log_batch_and_update():
    calls, transport = _recorder()
    _run(MLflowExporter(transport=transport, experiment_id="0"))

    paths = [path for path, _ in calls]
    assert paths == ["/api/2.0/mlflow/runs/create",
                     "/api/2.0/mlflow/runs/log-batch",
                     "/api/2.0/mlflow/runs/update"]

    create = calls[0][1]
    tags = {t["key"]: t["value"] for t in create["tags"]}
    assert create["experiment_id"] == "0" and create["start_time"] > 0
    assert tags["session.id"] == "s_1" and tags["rigging.run_id"] == "r_1"

    batch = calls[1][1]
    metrics = {m["key"]: m["value"] for m in batch["metrics"]}
    params = {p["key"]: p["value"] for p in batch["params"]}
    assert batch["run_id"] == "ml-1"
    assert metrics["gen_ai.usage.input_tokens"] == 10
    assert metrics["gen_ai.usage.output_tokens"] == 4
    assert metrics["rigging.chat_calls"] == 1 and metrics["rigging.tool_calls"] == 1
    assert metrics["rigging.steps"] == 2 and metrics["rigging.cost_usd"] == 0.0012
    assert params["gen_ai.request.model"] == "qwen3:8b"

    update = calls[2][1]
    assert update == {"run_id": "ml-1", "status": "FINISHED", "end_time": update["end_time"]}


def test_benchmark_rep_tags_reach_mlflow():
    """RIGGING_TRACE_TAGS labels a run here too, or a 5-rep benchmark is 70
    indistinguishable runs named `rigging`."""
    calls, transport = _recorder()
    exporter = MLflowExporter(transport=transport)
    root = exporter.start_span("invoke_agent", {
        "gen_ai.request.model": "qwen3:8b",
        "rigging.trace_tags": "bench,qwen3:8b,rep-3",
    })
    exporter.end_span(root, {"rigging.status": "ok"})
    exporter.close()

    tags = {t["key"]: t["value"] for t in calls[0][1]["tags"]}
    assert tags["rigging.tags"] == "bench,qwen3:8b,rep-3"


def test_the_loop_writes_tags_a_non_langfuse_exporter_can_read(monkeypatch):
    import rigging.loop as loop

    monkeypatch.setattr(loop.settings, "trace_tags", "bench, m , rep-1")
    assert loop._trace_tags() == {"langfuse.trace.tags": ["bench", "m", "rep-1"],
                                  "rigging.trace_tags": "bench,m,rep-1"}
    monkeypatch.setattr(loop.settings, "trace_tags", "")
    assert loop._trace_tags() == {}


def test_a_run_that_did_not_end_ok_is_marked_failed():
    calls, transport = _recorder()
    _run(MLflowExporter(transport=transport), status="budget_exhausted")
    assert calls[-1][1]["status"] == "FAILED"
    tags = {t["key"]: t["value"] for t in calls[1][1]["tags"]}
    assert tags["rigging.status"] == "budget_exhausted"


def test_the_run_is_created_once_for_many_spans():
    calls, transport = _recorder()
    exporter = MLflowExporter(transport=transport)
    for _ in range(3):
        exporter.start_span("chat", {})
    assert [p for p, _ in calls].count("/api/2.0/mlflow/runs/create") == 1


def test_a_token_is_sent_as_a_bearer_header():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json={"run": {"info": {"run_id": "ml-1"}}})

    exporter = MLflowExporter(transport=httpx.MockTransport(handler), token="tkn")
    exporter.start_span("invoke_agent", {})
    exporter.close()
    assert seen == ["Bearer tkn"]


def test_a_server_error_is_raised_not_swallowed():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    exporter = MLflowExporter(transport=httpx.MockTransport(handler))
    try:
        exporter.start_span("invoke_agent", {})
    except RuntimeError as exc:
        assert "runs/create failed" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a 500 must surface; the loop is what swallows it")


def test_validation_accepts_a_live_experiment():
    calls, transport = _recorder()
    MLflowExporter(transport=transport, experiment_id="1").validate()
    path, _ = calls[0]
    assert path == "/api/2.0/mlflow/experiments/get"


def test_validation_rejects_an_experiment_that_does_not_exist():
    """`runs/create` would fail mid-run, where the loop swallows it: fail first."""
    import pytest

    from rigging.errors import ExporterConfigError

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error_code": "RESOURCE_DOES_NOT_EXIST",
                                         "message": "No Experiment with id=99 exists"})

    exporter = MLflowExporter(transport=httpx.MockTransport(handler), experiment_id="99")
    with pytest.raises(ExporterConfigError) as exc:
        exporter.validate()
    assert "no mlflow experiment with id '99'" in str(exc.value)
    assert "experiments create" in str(exc.value), "the message says how to fix it"


def test_validation_rejects_a_deleted_experiment():
    """MLflow never reuses an id, so a re-created experiment is a *new* id."""
    import pytest

    from rigging.errors import ExporterConfigError

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"experiment": {
            "experiment_id": "1", "name": "rigging-bench", "lifecycle_stage": "deleted"}})

    exporter = MLflowExporter(transport=httpx.MockTransport(handler), experiment_id="1")
    with pytest.raises(ExporterConfigError) as exc:
        exporter.validate()
    assert "is deleted" in str(exc.value) and "never reuses an id" in str(exc.value)


def test_validation_reports_an_unreachable_server():
    import pytest

    from rigging.errors import ExporterConfigError

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    exporter = MLflowExporter(transport=httpx.MockTransport(handler),
                              tracking_uri="http://127.0.0.1:9")
    with pytest.raises(ExporterConfigError) as exc:
        exporter.validate()
    assert "unreachable at http://127.0.0.1:9" in str(exc.value)


def test_the_registry_entry_validates_before_returning():
    """`--export mlflow` and a manifest both resolve through this factory."""
    import pytest

    import rigging.observability as obs
    from rigging.errors import ExporterConfigError

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error_code": "RESOURCE_DOES_NOT_EXIST"})

    with pytest.raises(ExporterConfigError):
        obs.mlflow_exporter(transport=httpx.MockTransport(handler), experiment_id="7")

    _calls, transport = _recorder()
    exporter = obs.mlflow_exporter(transport=transport, experiment_id="0")
    assert isinstance(exporter, MLflowExporter)


def test_a_manifest_selects_mlflow_through_the_registry(tmp_path, monkeypatch):
    """The whole path: manifest -> EXPORTERS -> loop -> HTTP."""
    import yaml

    import rigging.observability as obs
    from rigging.events import EventLog
    from rigging.loop import run_agent
    from rigging.manifest import load_manifest
    from rigging.testing import FakeClient

    calls, transport = _recorder()
    exporter = obs.MLflowExporter(transport=transport)
    monkeypatch.setitem(obs.EXPORTERS, "mlflow", lambda **_kw: exporter)

    manifest = tmp_path / "agent.yaml"
    manifest.write_text(yaml.safe_dump({
        "name": "t", "model": "test", "system_prompt": "be terse",
        "limits": {"max_steps": 2}, "observability": {"exporters": ["mlflow"]},
    }))
    result = run_agent(load_manifest(str(manifest)), "hi", log=EventLog(tmp_path / "e.db"),
                       client=FakeClient([{"role": "assistant", "content": "hello"}]))

    assert result.status == "ok"
    paths = [path for path, _ in calls]
    assert paths[-2:] == ["/api/2.0/mlflow/runs/log-batch", "/api/2.0/mlflow/runs/update"]
    metrics = {m["key"]: m["value"] for m in calls[-2][1]["metrics"]}
    assert metrics["gen_ai.usage.input_tokens"] == 10
    assert calls[-1][1]["status"] == "FINISHED"
