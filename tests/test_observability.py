"""Exporters and eval: the observability contract, without a network."""

from __future__ import annotations

import base64

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


def test_spans_carry_the_prompt_and_answer(tmp_path, monkeypatch):
    """The UI's input/output columns come from langfuse.observation.*, not gen_ai.*."""
    import json

    exporter = RecordingExporter()
    _patched_exporters(monkeypatch, exporter)
    spec = _spec(tmp_path, observability={"exporters": ["console"]})

    run_agent(spec, "hash it", log=EventLog(tmp_path / "c.db"),
              client=FakeClient([{"role": "assistant", "content": "done"}]))

    root = exporter.spans[0][1]
    assert json.loads(root["langfuse.observation.input"]) == "hash it"
    assert json.loads(root["langfuse.trace.input"]) == "hash it"
    chat_input = json.loads(dict(exporter.spans)["chat"]["langfuse.observation.input"])
    assert {"role": "user", "content": "hash it"} in chat_input

    ended = dict(exporter.ended)
    assert json.loads(ended["invoke_agent"]["langfuse.observation.output"]) == "done"
    assert json.loads(ended["invoke_agent"]["langfuse.trace.output"]) == "done"
    assert json.loads(ended["chat"]["langfuse.observation.output"]) == "done"


def test_capture_content_false_keeps_content_out_of_spans(tmp_path, monkeypatch):
    exporter = RecordingExporter()
    _patched_exporters(monkeypatch, exporter)
    spec = _spec(tmp_path, observability={"exporters": ["console"], "capture_content": False})

    run_agent(spec, "secret", log=EventLog(tmp_path / "d.db"),
              client=FakeClient([{"role": "assistant", "content": "ok"}]))

    for _, attrs in exporter.spans + exporter.ended:
        assert not any(k.startswith("langfuse.observation.") for k in attrs)


def test_exporter_capture_content_false_drops_content(monkeypatch):
    from rigging.observability import LangfuseExporter

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    exporter = LangfuseExporter(capture_content=False)
    assert exporter._attributes({
        "langfuse.observation.input": '"x"',
        "gen_ai.request.model": "m",
    }) == {"gen_ai.request.model": "m"}


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


# ------------------------------------------------- exporters close on every exit

def test_exporters_close_when_the_output_is_denied(tmp_path, monkeypatch):
    """A denied run still produced spans; dropping the batch would lose them."""
    from rigging.manifest import GuardrailRef, Guardrails
    from rigging.protocols import Allow, Deny
    from rigging.registry import Registry

    class NoApologies:
        name = "no_apologies"
        hooks = ("on_output",)

        def on_input(self, messages): return Allow()
        def on_tool_call(self, call, tool): return Allow()
        def on_tool_result(self, call, content): return Allow()
        def on_output(self, text, messages): return Deny("no")

    reg = Registry()
    reg.add("guardrail", "no_apologies", NoApologies())
    exporter = RecordingExporter()
    _patched_exporters(monkeypatch, exporter)
    spec = _spec(tmp_path, observability={"exporters": ["console"]})
    spec.guardrails = Guardrails(on_output=[GuardrailRef(name="no_apologies")])

    result = run_agent(spec, "hi", log=EventLog(tmp_path / "d.db"), registry=reg,
                       client=FakeClient([{"role": "assistant", "content": "sorry"}]))

    assert result.status == "denied"
    assert exporter.closed is True
    # The root span must be *ended*, not just flushed: a run-level backend would
    # otherwise leave the run RUNNING forever.
    assert exporter.ended[-1][0] == "invoke_agent"
    assert exporter.ended[-1][1]["rigging.status"] == "denied"


def test_exporters_close_when_the_budget_is_exhausted(tmp_path, monkeypatch):
    exporter = RecordingExporter()
    _patched_exporters(monkeypatch, exporter)
    spec = _spec(tmp_path, tools=["sha256_hex"], limits={"max_steps": 1},
                 observability={"exporters": ["console"]})

    # One tool call uses the only step; the final-answer call has no scripted
    # response left, so the run ends as budget_exhausted.
    result = run_agent(spec, "hash", log=EventLog(tmp_path / "b.db"), client=FakeClient([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "1", "name": "sha256_hex", "arguments": {"text": "x"}}]},
    ]))

    assert result.status == "budget_exhausted"
    assert exporter.closed is True
    assert exporter.ended[-1][0] == "invoke_agent"
    assert exporter.ended[-1][1]["rigging.status"] == "budget_exhausted"

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


# ------------------------------------------------------------------ langfuse host

def test_langfuse_host_is_honoured(monkeypatch):
    from rigging.observability import LangfuseExporter

    monkeypatch.setenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    exporter = LangfuseExporter()
    assert exporter.endpoint == "https://cloud.langfuse.com/api/public/otel/v1/traces"


def test_langfuse_host_wins_over_the_legacy_base_url(monkeypatch):
    from rigging.observability import LangfuseExporter

    monkeypatch.setenv("LANGFUSE_HOST", "https://host.example")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://legacy.example")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    assert LangfuseExporter().endpoint == "https://host.example/api/public/otel/v1/traces"


def test_langfuse_legacy_base_url_still_works(monkeypatch):
    import rigging.observability as obs

    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://legacy.example/")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setattr(obs, "settings", _no_dotenv())  # no LANGFUSE_HOST in .env
    assert obs.LangfuseExporter().endpoint == "https://legacy.example/api/public/otel/v1/traces"


class _no_dotenv:
    """Stand-in for Settings with no .env keys, so a test sees the environment only."""

    langfuse_host = ""
    langfuse_public_key = ""
    langfuse_secret_key = ""


def test_langfuse_credentials_are_required(monkeypatch):
    import pytest

    import rigging.observability as obs

    monkeypatch.setenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setattr(obs, "settings", _no_dotenv())
    with pytest.raises(ValueError, match="LANGFUSE_PUBLIC_KEY"):
        obs.LangfuseExporter()


def test_settings_reads_langfuse_keys_from_the_env_file(tmp_path, monkeypatch):
    """.env is the documented home for secrets; `harness run` must not need sourcing."""
    from rigging.config import Settings

    for key in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "LANGFUSE_HOST=https://cloud.langfuse.com\n"
        "LANGFUSE_PUBLIC_KEY=pk-from-file\n"
        "LANGFUSE_SECRET_KEY=sk-from-file\n"
    )

    s = Settings(_env_file=str(env))
    assert s.langfuse_host == "https://cloud.langfuse.com"
    assert s.langfuse_public_key == "pk-from-file"
    assert s.langfuse_secret_key == "sk-from-file"


def test_exporter_falls_back_to_dotenv_settings(monkeypatch):
    """No exported shell vars, keys only in .env: the exporter still builds."""
    import types

    import rigging.observability as obs

    for key in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
                "LANGFUSE_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(obs, "settings", types.SimpleNamespace(
        langfuse_host="https://cloud.langfuse.com",
        langfuse_public_key="pk-from-file",
        langfuse_secret_key="sk-from-file",
    ))

    exporter = obs.LangfuseExporter()
    assert exporter.endpoint == "https://cloud.langfuse.com/api/public/otel/v1/traces"
    assert base64.b64decode(exporter._auth).decode() == "pk-from-file:sk-from-file"
