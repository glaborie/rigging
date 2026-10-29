from __future__ import annotations

from pathlib import Path

from rigging.events import EventLog
from rigging.redact import redact_text


def test_redacts_secret_shapes() -> None:
    text = "mail a@b.co key sk-or-v1-abcdefghijklmnopqrst token AKIAIOSFODNN7EXAMPLE"
    cleaned = redact_text(text)
    assert "a@b.co" not in cleaned
    assert "sk-or-v1-abcdefghijklmnopqrst" not in cleaned
    assert "AKIAIOSFODNN7EXAMPLE" not in cleaned


def test_event_log_is_a_security_boundary(tmp_path: Path) -> None:
    secret = "sk-or-v1-0123456789abcdefghij"
    db = tmp_path / "r.db"
    with EventLog(db) as log:
        run_id = log.start_run(model="fake")
        log.append(run_id, "tool_result", {"content": f"key={secret}"})

    assert secret.encode() not in db.read_bytes()
    with EventLog(db) as log:
        payload = next(log.events(run_id, type_="tool_result"))["payload"]
    assert payload["content"] == "key=[REDACTED]"
