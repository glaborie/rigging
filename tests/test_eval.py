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
    assert spec.credential_profile == "openrouter", "the demo pins OpenRouter for tools"

    assert _with_profile(spec, "default").credential_profile == "default"
    assert spec.credential_profile == "openrouter", "the input spec is not mutated"
    assert _with_profile(spec, None).credential_profile == "openrouter"


def _canned_suite(tmp_path, monkeypatch, ids_and_verdicts):
    """A suite that runs no model: `run_case` is replaced with canned verdicts."""
    import rigging.eval as ev

    suite = tmp_path / "eval.yaml"
    cases = "".join(f"  - {{id: {case}, prompt: 'x'}}\n" for case, _ in ids_and_verdicts)
    suite.write_text(f"name: canned\nmanifest: agent.yaml\ncases:\n{cases}")

    monkeypatch.setattr(ev, "load_manifest", lambda *_a, **_k: object())
    verdicts = iter(ids_and_verdicts)

    def fake_run_case(case, spec, **_kwargs):
        case_id, passed = next(verdicts)
        return ev.CaseResult(id=case_id, passed=passed, latency_s=1.5)

    monkeypatch.setattr(ev, "run_case", fake_run_case)
    return suite


def test_suite_progress_is_one_stderr_line_per_case(tmp_path, monkeypatch, capsys):
    """A 14-case suite must not look hung: each verdict is printed as it lands."""
    import rigging.eval as ev

    suite = _canned_suite(tmp_path, monkeypatch, [("first", True), ("second", False)])
    results, _ = ev.run_suite(suite, db=tmp_path / "e.db", progress=True)

    err = capsys.readouterr().err
    assert "[1/2] first" in err and "PASS" in err.splitlines()[0]
    assert "[2/2] second" in err and "FAIL" in err.splitlines()[1]
    assert [r.passed for r in results] == [True, False]
    assert capsys.readouterr().out == "", "stdout stays the table, not progress"


def test_suite_progress_is_off_unless_asked(tmp_path, monkeypatch, capsys):
    """The library default writes nothing: only the CLI opts in."""
    import rigging.eval as ev

    suite = _canned_suite(tmp_path, monkeypatch, [("only", True)])
    ev.run_suite(suite, db=tmp_path / "e.db")
    assert capsys.readouterr().err == ""



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


def test_guardrail_flagged_counts_transforms_not_just_denies(tmp_path, monkeypatch):
    """prompt_injection flags with a Transform; a Deny-only check would miss it."""
    import types

    import rigging.eval as ev
    from rigging.manifest import AgentSpec, Limits

    def fake(spec, prompt, **kw):
        log = kw["log"]
        session = log.start_session(None, title="t", spec_hash="h")
        run_id = log.start_run(session_id=session, spec_hash="h", model="m")
        log.append(run_id, "guardrail_verdict", {"hook": "on_tool_result", "checks": [
            {"guardrail": "prompt_injection", "hook": "on_tool_result",
             "verdict": "Transform", "transform": "injection flagged"},
        ]})
        return types.SimpleNamespace(run_id=run_id, output="ok", status="ok",
                                     steps=1, cost_usd=None)

    monkeypatch.setattr(ev, "run_agent", fake)
    spec = AgentSpec(name="t", model="m", tools=[], limits=Limits(max_steps=2))
    db = tmp_path / "e.db"

    assert ev.run_case({"id": "c1", "prompt": "hi",
                        "expect": {"guardrail_flagged": True}},
                       spec, db=db, approval_allow=False).passed
    result = ev.run_case({"id": "c2", "prompt": "hi",
                          "expect": {"guardrail_flagged": False}},
                         spec, db=db, approval_allow=False)
    assert not result.passed
    assert "guardrail flag True != False" in result.failures[0]
