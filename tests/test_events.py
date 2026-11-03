"""The log's own guarantees: named runs, and projections across a session.

The web view names a run before starting its thread and rebuilds history from the
log instead of a server-side dict (ADR-0023), so both behaviours live here rather
than in the service.
"""

from __future__ import annotations

from rigging.events import EventLog


def test_a_named_run_is_opened_once(tmp_path):
    """Two writers, one run: the row and `run_started` are written once."""
    db = tmp_path / "e.db"
    first = EventLog(db)
    run_id = first.start_run(session_id="s_1", model="fake", run_id="r_fixed")
    assert run_id == "r_fixed"
    first.append(run_id, "input_received", {"content": "hi"})
    first.close()

    # The worker thread opens its own log and confirms the same run.
    second = EventLog(db)
    assert second.start_run(session_id="s_1", model="fake", run_id="r_fixed") == "r_fixed"
    second.append(run_id, "model_response", {"content": "hello"})

    kinds = [e["type"] for e in second.events(run_id)]
    assert kinds == ["run_started", "input_received", "model_response"]
    assert [e["seq"] for e in second.events(run_id)] == [1, 2, 3]
    second.close()


def test_session_messages_join_turns_in_order(tmp_path):
    log = EventLog(tmp_path / "e.db")
    sid = log.start_session(title="t")
    first = log.start_run(session_id=sid, model="fake")
    log.append(first, "input_received", {"content": "first question"})
    log.append(first, "model_response", {"content": "first answer", "tool_calls": []})
    second = log.start_run(session_id=sid, model="fake")
    log.append(second, "input_received", {"content": "second question"})

    assert [(m.role, m.content) for m in log.session_messages(sid)] == [
        ("user", "first question"),
        ("assistant", "first answer"),
        ("user", "second question"),
    ]
    log.close()


def test_session_messages_ignore_child_runs(tmp_path):
    """A subagent shares the session id but is not the user's conversation."""
    log = EventLog(tmp_path / "e.db")
    sid = log.start_session(title="t")
    parent = log.start_run(session_id=sid, model="fake")
    log.append(parent, "input_received", {"content": "parent"})
    child = log.start_run(session_id=sid, model="fake", parent_run_id=parent)
    log.append(child, "input_received", {"content": "child only"})

    assert [m.content for m in log.session_messages(sid)] == ["parent"]
    log.close()
