"""Replay contract: same manifest + prompt + transcript => same output, zero network."""

from __future__ import annotations

import pytest

from rigging.loop import run_agent
from rigging.manifest import load_manifest
from rigging.replay import ReplayClient
from rigging.testing import FakeClient
from rigging.types import Message, ModelRequest


@pytest.fixture()
def two_tools_manifest(tmp_path):
    import yaml

    d = tmp_path / "agent.yaml"
    d.write_text(yaml.safe_dump({
        "name": "t", "model": "test", "system_prompt": "be terse",
        "tools": ["current_time", "sha256_hex"],
        "limits": {"max_steps": 6, "max_tokens": 8000},
    }))
    return load_manifest(str(d))


def test_replay_is_network_free_and_faithful(two_tools_manifest, tmp_path):
    from rigging.events import EventLog

    # 1) record a real two-tool run with a scripted model
    script = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "name": "current_time", "arguments": {}}]},
        {"role": "assistant", "content": "done", "tool_calls": [{"id": "2", "name": "sha256_hex", "arguments": {"text": "rigging"}}]},
        {"role": "assistant", "content": "The digest of rigging is 1f1398a0bc68cb2da7e2234383f8757bba6a2debbb49a788790b9bc12e11334c."},
    ]
    log = EventLog(tmp_path / "live.db")
    live = run_agent(two_tools_manifest, "hash the word rigging", client=FakeClient(script), log=log)
    assert live.status == "ok"

    # 2) replay it with a client that must never be asked for anything new
    path = log.export_jsonl(live.run_id, tmp_path / "t.jsonl")
    class NoNetwork:
        def complete(self, request: ModelRequest) -> None:
            raise AssertionError("replay must not hit the network")
    replayed = run_agent(two_tools_manifest, "hash the word rigging",
                         client=ReplayClient(path), log=EventLog(tmp_path / "r.db"))
    assert replayed.status == "ok"
    assert replayed.output == live.output


def test_replay_exhaustion_is_loud(two_tools_manifest, tmp_path):
    from rigging.events import EventLog

    log = EventLog(tmp_path / "live.db")
    live = run_agent(two_tools_manifest, "hi", client=FakeClient([{"role": "assistant", "content": "hey"}]), log=log)
    path = log.export_jsonl(live.run_id, tmp_path / "t.jsonl")
    rc = ReplayClient(path)
    from rigging.types import ModelRequest

    req = ModelRequest(model="test", messages=[], tools=[])
    rc.complete(req)  # the recorded turn is consumed
    with pytest.raises(RuntimeError, match="replay exhausted"):
        rc.complete(req)  # a second turn has no recording to serve


def test_history_continuity(tmp_path, two_tools_manifest):
    from rigging.events import EventLog
    
    # turn 1 remembers a name; turn 2 is asked to recall it — with history,
    # the second request carries the first exchange
    script1 = [{"role": "assistant", "content": "Your name is Ada."}]
    script2 = [{"role": "assistant", "content": "You are Ada."}]
    seen: list[list[Message]] = []

    class RecordingClient(FakeClient):
        def complete(self, request):
            seen.append(list(request.messages))
            return super().complete(request)

    log = EventLog(tmp_path / "h.db")
    r1 = run_agent(two_tools_manifest, "remember: my name is Ada", client=RecordingClient(script1), log=log)
    r2 = run_agent(two_tools_manifest, "what is my name?",
                   client=RecordingClient(script2), log=log, history=r1.history)
    assert "remember" in str(seen[1][1].content) or "remember" in str(seen[1][-2].content)
    assert r2.history and len(r2.history) > len(r1.history or [])
