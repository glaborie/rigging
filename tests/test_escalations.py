"""The human queue: `EventLog.escalations()` and `harness escalations`.

An escalation is an event, not a second store (ADR-0005). The projection joins
the run and session so a human gets the queue without reading raw event rows.
"""

from __future__ import annotations

import json

from rigging.cli import main
from rigging.events import EventLog


def _seed(db):
    log = EventLog(db)
    session_id = log.start_session(title="b2b-support")
    first = log.start_run(session_id=session_id, model="fake")
    log.append(first, "escalation", {
        "reason": "needs_approval", "summary": "First handoff",
        "facts": {"order_id": "ORD-1001"}, "transcript_run_id": first,
    })
    log.finish_run(first, "ok")
    second = log.start_run(session_id=session_id, model="fake")
    log.append(second, "escalation", {
        "reason": "customer_requested", "summary": "Second handoff",
        "facts": {"order_id": "ORD-1002"}, "transcript_run_id": second,
    })
    log.finish_run(second, "ok")
    log.close()
    return first, second


def test_projection_is_newest_first_and_joined_to_the_session(tmp_path):
    first, second = _seed(tmp_path / "e.db")
    log = EventLog(tmp_path / "e.db")
    rows = log.escalations()
    log.close()
    assert [row["run_id"] for row in rows] == [second, first]
    assert rows[0]["reason"] == "customer_requested"
    assert rows[0]["facts"] == {"order_id": "ORD-1002"}
    assert rows[0]["session_title"] == "b2b-support"
    assert rows[0]["session_id"].startswith("s_")
    assert rows[0]["model"] == "fake"


def test_cli_prints_a_table_and_json(tmp_path, capsys):
    first, second = _seed(tmp_path / "e.db")
    assert main(["escalations", "--db", str(tmp_path / "e.db")]) == 0
    out = capsys.readouterr().out
    assert second in out and "customer_requested" in out
    assert "drill in with" in out

    assert main(["escalations", "--db", str(tmp_path / "e.db"), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["run_id"] for row in rows] == [second, first]
    assert rows[0]["facts"] == {"order_id": "ORD-1002"}


def test_cli_empty_is_not_an_error(tmp_path, capsys):
    EventLog(tmp_path / "e.db").close()
    assert main(["escalations", "--db", str(tmp_path / "e.db")]) == 0
    captured = capsys.readouterr()
    assert "no escalations" in captured.err
    assert captured.out == ""
