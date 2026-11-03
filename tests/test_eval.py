"""Eval config: `--export` traces a suite without editing the shared manifest."""

from __future__ import annotations

import pathlib

from rigging.eval import _with_exporters
from rigging.manifest import load_manifest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_export_override_replaces_the_manifest_list():
    spec = load_manifest(str(ROOT / "examples/b2b-support/agent.yaml"))
    assert spec.observability.exporters == [], "the demo manifest stays key-free"

    traced = _with_exporters(spec, ["langfuse"])
    assert traced.observability.exporters == ["langfuse"]
    assert spec.observability.exporters == [], "the input spec is not mutated"

    assert _with_exporters(spec, None).observability.exporters == []


def test_profile_override_picks_the_gateway():
    from rigging.eval import _with_profile

    spec = load_manifest(str(ROOT / "examples/b2b-support/agent.yaml"))
    assert spec.credential_profile == "ollama", "the demo manifest pins Ollama"

    assert _with_profile(spec, "default").credential_profile == "default"
    assert spec.credential_profile == "ollama", "the input spec is not mutated"
    assert _with_profile(spec, None).credential_profile == "ollama"


def test_run_case_records_the_verdict_as_an_event(tmp_path, monkeypatch):
    """The verdict is joined later by run_id, so it must live in the log."""
    import types

    import rigging.eval as ev
    from rigging.events import EventLog
    from rigging.manifest import AgentSpec, Limits

    def fake(spec, prompt, **kw):
        log = kw["log"]
        session = log.start_session(None, title="t", spec_hash="h")
        run_id = log.start_run(session_id=session, spec_hash="h", model="m")
        return types.SimpleNamespace(run_id=run_id, output="hello", status="ok",
                                     steps=1, cost_usd=None)

    monkeypatch.setattr(ev, "run_agent", fake)

    spec = AgentSpec(name="t", model="m", tools=[], limits=Limits(max_steps=2))
    db = tmp_path / "e.db"
    passed = ev.run_case({"id": "c1", "prompt": "hi",
                          "expect": {"must_include": ["hello"]}},
                         spec, db=db, approval_allow=False)
    failed = ev.run_case({"id": "c2", "prompt": "hi",
                          "expect": {"must_include": ["nope"]}},
                         spec, db=db, approval_allow=False)

    log = EventLog(db)
    first = next(log.events(passed.run_id, type_="eval_verdict"))["payload"]
    second = next(log.events(failed.run_id, type_="eval_verdict"))["payload"]
    assert first == {"case": "c1", "passed": True, "failures": []}
    assert second["passed"] is False and second["failures"]
    assert passed.passed is True and failed.passed is False
