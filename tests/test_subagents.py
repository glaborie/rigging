"""Subagents are child runs (ADR-0019): same log, own budget, approvals intact.

Offline: a scripted FakeClient is shared by parent and child, so the response
order is the delegation order.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from rigging.approvals import AutoDenyApproval, ScriptedApproval
from rigging.errors import ManifestError
from rigging.events import EventLog
from rigging.loop import run_agent
from rigging.manifest import load_manifest
from rigging.testing import FakeClient


def _write(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data))
    return path


def _manifests(tmp_path: Path, *, parent_extra: dict | None = None,
               child_extra: dict | None = None) -> Path:
    _write(tmp_path / "helper.yaml", {
        "name": "helper", "model": "test", "system_prompt": "be terse",
        "limits": {"max_steps": 3}, **(child_extra or {}),
    })
    parent = {
        "name": "parent", "model": "test", "system_prompt": "delegate",
        "limits": {"max_steps": 4},
        "subagents": [{"name": "helper", "manifest": "helper.yaml",
                       "description": "Summarise things."}],
        **(parent_extra or {}),
    }
    return _write(tmp_path / "parent.yaml", parent)


def _spawn_call(name: str = "spawn_helper", task: str = "summarise x") -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "name": name, "arguments": {"task": task}}]}


def _tool_result(log: EventLog, run_id: str) -> dict:
    events = [e for e in log.events(run_id) if e["type"] == "tool_result"]
    assert events, "no tool_result event"
    return events[-1]["payload"]


def test_a_spawn_runs_a_child_in_the_same_log(tmp_path):
    spec = load_manifest(str(_manifests(tmp_path)))
    log = EventLog(tmp_path / "s.db")
    client = FakeClient([
        _spawn_call(),
        {"role": "assistant", "content": "child answer"},
        {"role": "assistant", "content": "final answer"},
    ])

    result = run_agent(spec, "go", log=log, client=client,
                       approval=ScriptedApproval(True))

    assert result.status == "ok" and result.output == "final answer"
    payload = _tool_result(log, result.run_id)
    child = json.loads(payload["content"])
    assert child["status"] == "ok" and child["output"] == "child answer"
    assert child["run_id"] != result.run_id

    rows = {r["run_id"]: r for r in log.runs()}
    assert rows[child["run_id"]]["parent_run_id"] == result.run_id
    assert rows[child["run_id"]]["session_id"] == rows[result.run_id]["session_id"]
    child_types = [e["type"] for e in log.events(child["run_id"])]
    assert child_types[0] == "run_started" and "run_finished" in child_types


class _OneBrokenCall(FakeClient):
    """Fails exactly one call, so a child can die while the parent continues."""

    def __init__(self, responses: list, fail_at: int) -> None:
        super().__init__(responses)
        self.fail_at = fail_at
        self.calls = 0

    def complete(self, request):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls == self.fail_at:
            raise RuntimeError("gateway down")
        return super().complete(request)


def test_a_failed_child_is_an_error_result_not_a_crash(tmp_path):
    # The child uses its one step on a tool call, then its final-answer call
    # fails, so the child ends budget_exhausted while the parent keeps going.
    spec = load_manifest(str(_manifests(
        tmp_path,
        child_extra={"tools": ["sha256_hex"], "limits": {"max_steps": 1}},
    )))
    log = EventLog(tmp_path / "s.db")
    client = _OneBrokenCall([
        _spawn_call(),
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c2", "name": "sha256_hex", "arguments": {"text": "x"}}]},
        {"role": "assistant", "content": "parent survived"},
    ], fail_at=3)

    result = run_agent(spec, "go", log=log, client=client,
                       approval=ScriptedApproval(True))

    assert result.status == "ok" and result.output == "parent survived"
    payload = _tool_result(log, result.run_id)
    assert payload["is_error"] is True
    assert json.loads(payload["content"])["status"] == "budget_exhausted"


def test_spawn_is_side_effecting_so_auto_deny_blocks_it(tmp_path):
    spec = load_manifest(str(_manifests(tmp_path)))
    log = EventLog(tmp_path / "s.db")
    client = FakeClient([
        _spawn_call(),
        {"role": "assistant", "content": "did it myself"},
    ])

    result = run_agent(spec, "go", log=log, client=client, approval=AutoDenyApproval())

    assert result.status == "ok"
    assert len(log.runs()) == 1, "the child must not run without approval"
    payload = _tool_result(log, result.run_id)
    assert payload["denied"] is True


def test_depth_cap_returns_a_structured_error(tmp_path):
    # parent -> helper -> helper2, with the default cap of 1.
    _write(tmp_path / "helper2.yaml", {"name": "helper2", "model": "test"})
    spec = load_manifest(str(_manifests(
        tmp_path,
        child_extra={"subagents": [{"name": "helper2", "manifest": "helper2.yaml"}]},
    )))
    log = EventLog(tmp_path / "s.db")
    client = FakeClient([
        _spawn_call(),
        _spawn_call(name="spawn_helper2", task="deeper"),
        {"role": "assistant", "content": "child gave up gracefully"},
        {"role": "assistant", "content": "done"},
    ])

    result = run_agent(spec, "go", log=log, client=client,
                       approval=ScriptedApproval(True))

    assert result.status == "ok"
    child = json.loads(_tool_result(log, result.run_id)["content"])
    # The child's own spawn was refused; the child still finished.
    child_payload = _tool_result(log, child["run_id"])
    assert child_payload["is_error"] is True
    assert "max subagent depth" in child_payload["content"]
    assert len(log.runs()) == 2, "no grandchild run"


def test_child_usage_is_reported_but_not_billed_to_the_parent(tmp_path):
    spec = load_manifest(str(_manifests(tmp_path)))
    log = EventLog(tmp_path / "s.db")
    client = FakeClient([
        _spawn_call(),
        {"role": "assistant", "content": "child answer"},
        {"role": "assistant", "content": "final answer"},
    ])

    result = run_agent(spec, "go", log=log, client=client,
                       approval=ScriptedApproval(True))

    child = json.loads(_tool_result(log, result.run_id)["content"])
    assert child["usage"] == {"input_tokens": 10, "output_tokens": 5,
                              "cached_tokens": 0, "reasoning_tokens": 0}
    # Two parent responses at 10/5: the child's tokens are its own (ADR-0019).
    assert (result.usage.input_tokens, result.usage.output_tokens) == (20, 10)


def test_a_missing_child_manifest_fails_fast(tmp_path):
    spec = load_manifest(str(_manifests(
        tmp_path, parent_extra={"subagents": [{"name": "ghost", "manifest": "nope.yaml"}]},
    )))
    with pytest.raises(ManifestError, match="manifest not found"):
        run_agent(spec, "go", log=EventLog(tmp_path / "s.db"), client=FakeClient([]))


def test_two_subagents_cannot_share_a_tool_name(tmp_path):
    spec = load_manifest(str(_manifests(
        tmp_path,
        parent_extra={"subagents": [
            {"name": "helper", "manifest": "helper.yaml"},
            {"name": "helper", "manifest": "helper.yaml"},
        ]},
    )))
    with pytest.raises(ManifestError, match="collides"):
        run_agent(spec, "go", log=EventLog(tmp_path / "s.db"), client=FakeClient([]),
                  approval=ScriptedApproval(True))


def test_child_events_pass_the_redactor(tmp_path):
    spec = load_manifest(str(_manifests(tmp_path)))
    log = EventLog(tmp_path / "s.db")
    secret = "sk-" + "a" * 24
    client = FakeClient([
        _spawn_call(),
        {"role": "assistant", "content": f"the key is {secret}"},
        {"role": "assistant", "content": "done"},
    ])

    result = run_agent(spec, "go", log=log, client=client,
                       approval=ScriptedApproval(True))
    child = json.loads(_tool_result(log, result.run_id)["content"])

    written = (tmp_path / "s.db").read_bytes()
    assert secret.encode() not in written
    assert "[REDACTED]" in json.dumps([e for e in log.events(child["run_id"])])
