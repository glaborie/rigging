"""Exporters and eval: the observability contract, without a network."""

from __future__ import annotations

import yaml

from rigging.events import EventLog
from rigging.loop import run_agent
from rigging.manifest import load_manifest
from rigging.testing import FakeClient


class RecordingExporter:
    name = "recording"

    def __init__(self) -> None:
        self.spans: list[tuple[str, dict]] = []
        self.ended: list[tuple[str, dict]] = []
        self.closed = False

    def start_span(self, name, attributes):
        self.spans.append((name, attributes))
        return {"name": name}

    def end_span(self, span, attributes=None):
        self.ended.append((span["name"], attributes or {}))

    def close(self):
        self.closed = True


def _spec(tmp_path, **extra):
    data = {"name": "t", "model": "test", "system_prompt": "be terse", "limits": {"max_steps": 4}}
    data.update(extra)
    p = tmp_path / "a.yaml"
    p.write_text(yaml.safe_dump(data))
    return load_manifest(str(p))


def _patched_exporters(monkeypatch, obj):
    import rigging.observability as obs

    monkeypatch.setitem(obs.EXPORTERS, "console", lambda **kw: obj)


def test_manifest_exporter_receives_run_model_and_tool_spans(tmp_path, monkeypatch):
    exporter = RecordingExporter()
    _patched_exporters(monkeypatch, exporter)
    spec = _spec(tmp_path, tools=["sha256_hex"], observability={"exporters": ["console"]})

    result = run_agent(spec, "hash it", log=EventLog(tmp_path / "s.db"), client=FakeClient([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "1", "name": "sha256_hex", "arguments": {"text": "x"}}]},
        {"role": "assistant", "content": "done"},
    ]))

    names = [n for n, _ in exporter.spans]
    assert names == ["invoke_agent", "chat", "execute_tool", "chat"], names
    root_attrs = exporter.spans[0][1]
    assert root_attrs["gen_ai.request.model"] == "test"
    assert root_attrs["session.id"] and root_attrs["rigging.run_id"] == result.run_id
    tool_attrs = dict(exporter.spans)[ "execute_tool"]
    assert tool_attrs["gen_ai.tool.name"] == "sha256_hex"
    ended_root = exporter.ended[-1][1]  # the root span ends last
    assert ended_root["rigging.status"] == "ok" and ended_root["rigging.steps"] == 2
    assert exporter.closed is True


def test_no_spans_without_the_manifest_asking(tmp_path, monkeypatch):
    exporter = RecordingExporter()
    _patched_exporters(monkeypatch, exporter)
    spec = _spec(tmp_path)
    run_agent(spec, "hi", log=EventLog(tmp_path / "n.db"),
              client=FakeClient([{"role": "assistant", "content": "hello"}]))
    assert exporter.spans == [] and exporter.closed is False


def test_a_broken_exporter_never_breaks_a_run(tmp_path, monkeypatch):
    class Broken:
        name = "broken"

        def start_span(self, name, attributes):
            raise RuntimeError("boom")

        def end_span(self, span, attributes=None):
            raise RuntimeError("boom")

        def close(self):
            raise RuntimeError("boom")

    _patched_exporters(monkeypatch, Broken())
    spec = _spec(tmp_path, observability={"exporters": ["console"]})
    result = run_agent(spec, "hi", log=EventLog(tmp_path / "b.db"),
                       client=FakeClient([{"role": "assistant", "content": "hello"}]))
    assert result.status == "ok" and result.output == "hello"


def test_eval_predicates_over_the_event_log(tmp_path, monkeypatch):
    import rigging.eval as eval_mod

    manifest = tmp_path / "agent.yaml"
    manifest.write_text(yaml.safe_dump({
        "name": "e", "model": "test", "system_prompt": "be terse",
        "tools": ["sha256_hex"], "limits": {"max_steps": 4},
    }))
    suite = tmp_path / "suite.yaml"
    suite.write_text(yaml.safe_dump({
        "name": "t", "manifest": "agent.yaml", "approval": "deny",
        "cases": [
            {"id": "calls-tool", "prompt": "hash x",
             "expect": {"status": "ok", "tools_called": ["sha256_hex"],
                        "must_include": ["digest"]}},
            {"id": "wrong-tool", "prompt": "hash x",
             "expect": {"tools_called": ["refund_order"]}},  # must fail
        ],
    }))

    script = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "1", "name": "sha256_hex", "arguments": {"text": "x"}}]},
        {"role": "assistant", "content": "The digest is computed."},
    ]
    real_run = eval_mod.run_agent

    def fake_run(spec, prompt, **kw):
        kw["client"] = FakeClient(list(script))
        return real_run(spec, prompt, **kw)

    monkeypatch.setattr(eval_mod, "run_agent", fake_run)
    results, _ = eval_mod.run_suite(suite, db=tmp_path / "ev.db")
    by_id = {r.id: r for r in results}
    assert by_id["calls-tool"].passed, by_id["calls-tool"].failures
    assert not by_id["wrong-tool"].passed
    assert "was not called" in by_id["wrong-tool"].failures[0]
