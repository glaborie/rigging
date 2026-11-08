"""ADR-0029: an approval that outlives the run, and the invariant that comes first.

The one test that matters: **no side effect before approval.** Everything else in
this file exists to keep that sentence true after a resume, a denial, an expiry,
a second answer, or an edited log.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from rigging.config import settings
from rigging.errors import RiggingError
from rigging.events import EventLog
from rigging.loop import resume_run, run_agent
from rigging.manifest import AgentSpec, ApprovalCfg, Limits
from rigging.registry import Registry
from rigging.testing import FakeClient
from rigging.types import Message, ModelResponse, ToolCall, Usage


def _resp(content: str = "", calls: list[ToolCall] | None = None) -> ModelResponse:
    return ModelResponse(
        message=Message(role="assistant", content=content, tool_calls=calls or []),
        usage=Usage(input_tokens=10, output_tokens=5), finish_reason="stop", model="fake",
    )


def _refund(amount: int = 10, call_id: str = "c1") -> ModelResponse:
    return _resp(calls=[ToolCall(id=call_id, name="refund", arguments={"amount": amount})])


def _registry(counts: dict[str, int]) -> Registry:
    """Tools whose side effects are counted, so "did it run" is a number."""
    reg = Registry()

    def echo(text: str) -> str:
        """Echo text."""
        counts["echo"] = counts.get("echo", 0) + 1
        return text

    def refund(amount: int) -> str:
        """Refund money."""
        counts["refund"] = counts.get("refund", 0) + 1
        return f"refunded {amount}"

    for fn, name, side in ((echo, "echo", False), (refund, "refund", True)):
        fn.__rigging_tool__ = {"name": name, "description": fn.__doc__,  # type: ignore[attr-defined]
                               "side_effecting": side}
        reg.add("tool", name, fn)
    return reg


def _spec(**kw: Any) -> AgentSpec:
    base: dict[str, Any] = dict(
        name="t", model="fake", tools=["refund"],
        approval=ApprovalCfg(policy="deferred"), limits=Limits(max_steps=4),
    )
    base.update(kw)
    return AgentSpec(**base)


def _events(log: EventLog, run_id: str) -> list[str]:
    return [ev["type"] for ev in log.events(run_id)]


# ------------------------------------------------------------------ suspension

def test_a_deferred_approval_stops_before_the_tool_runs(tmp_path: Path) -> None:
    """The invariant, first: nothing executed, one question recorded."""
    counts: dict[str, int] = {}
    client = FakeClient([_refund(), _resp("done")])
    with EventLog(tmp_path / "r.db") as log:
        result = run_agent(_spec(), "refund 10", registry=_registry(counts),
                           client=client, log=log)
        types = _events(log, result.run_id)
        row = log.get_run(result.run_id)

    assert result.status == "pending_approval"
    assert counts == {}                     # the whole point: no side effect
    assert result.pending is not None
    assert result.pending.tool == "refund"
    assert result.pending.arguments == {"amount": 10}
    assert result.pending.approval_id.startswith("ap_")
    assert "approval_pending" in types
    assert "tool_result" not in types
    assert len(client.requests) == 1        # the model was not asked again
    assert row is not None and row["status"] == "pending_approval"
    assert row["finished_at"] is not None   # the run ended, it did not hang


def test_the_pending_approval_is_listed_until_it_is_answered(tmp_path: Path) -> None:
    counts: dict[str, int] = {}
    client = FakeClient([_refund(), _resp("done")])
    with EventLog(tmp_path / "r.db") as log:
        first = run_agent(_spec(), "refund 10", registry=_registry(counts),
                          client=client, log=log)
        waiting = log.approvals()
        pending = first.pending
        assert pending is not None
        resume_run(_spec(), first.run_id, approved=True, approver="alice",
                   registry=_registry(counts), client=client, log=log)
        after = log.approvals()

    assert [row["approval_id"] for row in waiting] == [pending.approval_id]
    assert waiting[0]["run_id"] == first.run_id
    assert waiting[0]["expired"] is False
    assert after == []                      # answered questions leave the queue


# ------------------------------------------------------------------ resume

def test_approving_runs_the_tool_exactly_once(tmp_path: Path) -> None:
    counts: dict[str, int] = {}
    client = FakeClient([_refund(), _resp("done")])
    reg = _registry(counts)
    with EventLog(tmp_path / "r.db") as log:
        first = run_agent(_spec(), "refund 10", registry=reg, client=client, log=log)
        second = resume_run(_spec(), first.run_id, approved=True, approver="alice",
                            registry=reg, client=client, log=log)
        types = _events(log, first.run_id)
        results = list(log.events(first.run_id, type_="tool_result"))
        resumed = list(log.events(first.run_id, type_="run_resumed"))

    assert second.status == "ok"
    assert second.output == "done"
    assert counts["refund"] == 1            # once, not twice
    assert len(results) == 1
    assert results[0]["payload"]["content"] == "refunded 10"
    assert "run_resumed" in types
    assert resumed[0]["payload"]["approver"] == "alice"
    assert resumed[0]["payload"]["approved"] is True
    assert len(client.requests) == 2


def test_resume_continues_the_transcript_instead_of_replaying_it(tmp_path: Path) -> None:
    """A tool that already ran is in the log; the resumed run must not run it again."""
    counts: dict[str, int] = {}
    two_calls = _resp(calls=[
        ToolCall(id="a", name="echo", arguments={"text": "hi"}),
        ToolCall(id="b", name="refund", arguments={"amount": 10}),
    ])
    client = FakeClient([two_calls, _resp("done")])
    reg = _registry(counts)
    with EventLog(tmp_path / "r.db") as log:
        spec = _spec(tools=["echo", "refund"], system_prompt="be brief")
        first = run_agent(spec, "go", registry=reg, client=client, log=log)
        second = resume_run(spec, first.run_id, approved=True, approver="alice",
                            registry=reg, client=client, log=log)
        results = [ev["payload"]["name"]
                   for ev in log.events(first.run_id, type_="tool_result")]

    assert second.status == "ok"
    assert counts["echo"] == 1              # the sibling call was not replayed
    assert counts["refund"] == 1
    assert results == ["echo", "refund"]    # declared order, one result each

    history = client.requests[1].messages
    assert [m.role for m in history] == ["system", "user", "assistant", "tool", "tool"]
    assert sum(1 for m in history if m.role == "user") == 1   # no second prompt
    assert sum(1 for m in history if m.role == "system") == 1


def test_denying_resumes_with_a_denial_and_no_side_effect(tmp_path: Path) -> None:
    counts: dict[str, int] = {}
    client = FakeClient([_refund(), _resp("understood")])
    reg = _registry(counts)
    with EventLog(tmp_path / "r.db") as log:
        first = run_agent(_spec(), "refund 10", registry=reg, client=client, log=log)
        second = resume_run(_spec(), first.run_id, approved=False, approver="bob",
                            note="out of policy", registry=reg, client=client, log=log)
        verdicts = list(log.events(first.run_id, type_="approval_verdict"))

    assert second.status == "ok"
    assert counts == {}
    assert any("denied by approval policy: out of policy" in (m.content or "")
               for m in client.requests[1].messages if m.role == "tool")
    assert verdicts[-1]["payload"]["approver"] == "bob"
    assert verdicts[-1]["payload"]["approved"] is False


# ------------------------------------------------------------------ refusals

def test_one_approval_gets_one_verdict(tmp_path: Path) -> None:
    """Two clients racing must not produce two refunds (ADR-0029)."""
    counts: dict[str, int] = {}
    client = FakeClient([_refund(), _resp("done")])
    reg = _registry(counts)
    with EventLog(tmp_path / "r.db") as log:
        first = run_agent(_spec(), "refund 10", registry=reg, client=client, log=log)
        pending = first.pending
        assert pending is not None
        resume_run(_spec(), first.run_id, approved=True, approver="a",
                   registry=reg, client=client, log=log)

        # the transaction guard itself: the second answer loses, and writes nothing
        assert log.decide_approval(first.run_id, pending.approval_id, approved=True,
                                   approver="b") is None
        with pytest.raises(RiggingError, match="not 'pending_approval'"):
            resume_run(_spec(), first.run_id, approved=True, approver="b",
                       registry=reg, client=client, log=log)
        verdicts = list(log.events(first.run_id, type_="approval_verdict"))

    assert len(verdicts) == 1
    assert counts["refund"] == 1


def test_resuming_a_finished_run_is_refused(tmp_path: Path) -> None:
    counts: dict[str, int] = {}
    client = FakeClient([_resp("no tools at all")])
    with EventLog(tmp_path / "r.db") as log:
        result = run_agent(_spec(), "hi", registry=_registry(counts), client=client, log=log)
        with pytest.raises(RiggingError, match="not 'pending_approval'"):
            resume_run(_spec(), result.run_id, approved=True, approver="a",
                       registry=_registry(counts), client=client, log=log)


def test_an_expired_approval_denies_even_when_approved(tmp_path: Path,
                                                       monkeypatch: Any) -> None:
    """Silence is a denial: a stale yes must not authorise anything (ADR-0023/0029)."""
    monkeypatch.setattr(settings, "approval_ttl_s", 0.0)
    counts: dict[str, int] = {}
    client = FakeClient([_refund(), _resp("ok")])
    reg = _registry(counts)
    with EventLog(tmp_path / "r.db") as log:
        first = run_agent(_spec(), "refund 10", registry=reg, client=client, log=log)
        second = resume_run(_spec(), first.run_id, approved=True, approver="a",
                            registry=reg, client=client, log=log)
        verdict = list(log.events(first.run_id, type_="approval_verdict"))[-1]["payload"]

    assert counts == {}
    assert second.status == "ok"
    assert verdict["approved"] is False
    assert verdict["note"].startswith("expired")


def test_an_edited_request_cannot_turn_into_another_approval(tmp_path: Path) -> None:
    """The approval is bound to the arguments it was issued against."""
    counts: dict[str, int] = {}
    client = FakeClient([_refund(10), _resp("done")])
    db = tmp_path / "r.db"
    with EventLog(db) as log:
        first = run_agent(_spec(), "refund 10", registry=_registry(counts),
                          client=client, log=log)

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT payload FROM run_events WHERE run_id=? AND type='approval_pending'",
                       (first.run_id,)).fetchone()
    payload = json.loads(row[0])
    payload["arguments"]["amount"] = 10000      # somebody edits the ask
    conn.execute("UPDATE run_events SET payload=? WHERE run_id=? AND type='approval_pending'",
                 (json.dumps(payload), first.run_id))
    conn.commit()
    conn.close()

    with EventLog(db) as log, pytest.raises(RiggingError, match="no longer matches"):
        resume_run(_spec(), first.run_id, approved=True, approver="a",
                   registry=_registry(counts), client=client, log=log)
    assert counts == {}
