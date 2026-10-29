"""Event-sourced run state (ADR-0005).

`run_events` is the source of truth. Messages, verdicts, usage and transcripts are
projections over it. One write path, so nothing can drift.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

from rigging.redact import redact_event
from rigging.types import Message, ToolCall, Usage

EventType = Literal[
    "run_started",
    "session_started",
    "input_received",
    "guardrail_verdict",
    "budget_check",
    "model_request",
    "model_delta",
    "reasoning_delta",
    "model_response",
    "tool_call",
    "approval_requested",
    "approval_verdict",
    "tool_result",
    "escalation",
    "output_verdict",
    "registry_discovered",
    "replay_miss",
    "sse_malformed",
    "error",
    "run_finished",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  title      TEXT,
  spec_hash  TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  run_id        TEXT PRIMARY KEY,
  session_id    TEXT,
  started_at    REAL NOT NULL,
  finished_at   REAL,
  status        TEXT NOT NULL DEFAULT 'running',
  spec_hash     TEXT,
  model         TEXT,
  parent_run_id TEXT,
  FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE TABLE IF NOT EXISTS run_events (
  run_id        TEXT NOT NULL,
  seq           INTEGER NOT NULL,
  ts            REAL NOT NULL,
  type          TEXT NOT NULL,
  payload       TEXT NOT NULL,
  span_id       TEXT,
  parent_run_id TEXT,
  PRIMARY KEY (run_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_events_type ON run_events(type);
CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id);
"""


class EventLog:
    """SQLite-backed append-only log. Every payload passes `redact_event`."""

    def __init__(self, path: str | Path = "rigging.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.commit()
        self._seq: dict[str, int] = {}

    # ------------------------------------------------------------------ writes

    def start_session(self, session_id: str | None = None, *, title: str = "", spec_hash: str = "") -> str:
        session_id = session_id or f"s_{uuid.uuid4().hex[:12]}"
        self._db.execute(
            "INSERT OR IGNORE INTO sessions(session_id, created_at, title, spec_hash) VALUES (?,?,?,?)",
            (session_id, time.time(), title, spec_hash),
        )
        self._db.commit()
        return session_id

    def start_run(self, *, session_id: str | None = None, spec_hash: str = "", model: str = "",
                  parent_run_id: str | None = None) -> str:
        run_id = f"r_{uuid.uuid4().hex[:12]}"
        self._db.execute(
            "INSERT INTO runs(run_id, session_id, started_at, status, spec_hash, model, parent_run_id)"
            " VALUES (?,?,?,?,?,?,?)",
            (run_id, session_id, time.time(), "running", spec_hash, model, parent_run_id),
        )
        self._db.commit()
        self.append(run_id, "run_started", {"model": model, "spec_hash": spec_hash})
        return run_id

    def append(self, run_id: str, type_: EventType | str, payload: dict[str, Any],
               *, span_id: str | None = None, parent_run_id: str | None = None) -> int:
        seq = self._seq.get(run_id, 0) + 1
        self._seq[run_id] = seq
        self._db.execute(
            "INSERT INTO run_events(run_id, seq, ts, type, payload, span_id, parent_run_id)"
            " VALUES (?,?,?,?,?,?,?)",
            (run_id, seq, time.time(), type_, json.dumps(redact_event(payload), default=str),
             span_id, parent_run_id),
        )
        self._db.commit()
        return seq

    def finish_run(self, run_id: str, status: str) -> None:
        self._db.execute("UPDATE runs SET finished_at=?, status=? WHERE run_id=?",
                         (time.time(), status, run_id))
        self._db.commit()

    # ------------------------------------------------------------------ reads

    def events(self, run_id: str, *, type_: str | None = None) -> Iterator[dict[str, Any]]:
        sql = "SELECT * FROM run_events WHERE run_id=? {}".format("AND type=?" if type_ else "")
        args = (run_id, type_) if type_ else (run_id,)
        for row in self._db.execute(sql + " ORDER BY seq", args):
            yield {
                "seq": row["seq"], "ts": row["ts"], "type": row["type"],
                "payload": json.loads(row["payload"]), "span_id": row["span_id"],
            }

    def messages(self, run_id: str) -> list[Message]:
        """Projection: the conversation as the model saw it."""
        out: list[Message] = []
        for ev in self.events(run_id):
            if ev["type"] == "input_received":
                out.append(Message(role="user", content=ev["payload"].get("content", "")))
            elif ev["type"] == "model_response":
                p = ev["payload"]
                out.append(Message(
                    role="assistant",
                    content=p.get("content", ""),
                    reasoning=p.get("reasoning"),
                    tool_calls=[ToolCall(**c) for c in p.get("tool_calls", [])],
                ))
            elif ev["type"] == "tool_result":
                p = ev["payload"]
                out.append(Message(role="tool", name=p.get("name"),
                                   tool_call_id=p.get("call_id"), content=p.get("content", "")))
        return out

    def usage(self, run_id: str) -> Usage:
        total = Usage()
        for ev in self.events(run_id, type_="model_response"):
            u = ev["payload"].get("usage") or {}
            total = total + Usage(**u)
        return total

    def export_jsonl(self, run_id: str, path: str | Path | None = None) -> Path:
        """The replayable transcript. `--replay` consumes exactly this file."""
        path = Path(path or f"runs/{run_id}.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            row = self._db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            fh.write(json.dumps({"type": "run", "run": dict(row) if row else {}}) + "\n")
            for ev in self.events(run_id):
                fh.write(json.dumps(ev, default=str) + "\n")
        return path

    def runs(self, *, session_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM runs {} ORDER BY started_at DESC LIMIT ?".format(
            "WHERE session_id=?" if session_id else "")
        args = (session_id, limit) if session_id else (limit,)
        return [dict(r) for r in self._db.execute(sql, args)]

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> EventLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
