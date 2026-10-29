"""The REPL is a thin client of the loop: scripted input/output, no terminal."""

from __future__ import annotations

from rigging.chat import chat
from rigging.events import EventLog
from rigging.manifest import load_manifest
from rigging.testing import FakeClient


def _manifest(tmp_path):
    import yaml

    p = tmp_path / "a.yaml"
    p.write_text(yaml.safe_dump({"name": "t", "model": "test", "system_prompt": "be terse"}))
    return load_manifest(str(p))


def test_chat_session_and_transcript(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    log = EventLog(tmp_path / "c.db")
    lines = iter(["remember this word: zanzibar", "/transcript", "/exit"])
    outs: list[str] = []

    # wire a scripted client through monkeypatching run_agent's default client
    import rigging.chat as chat_mod

    class C(FakeClient):
        def __init__(self) -> None:
            super().__init__([{"role": "assistant", "content": "noted: zanzibar"}])

    real_run = chat_mod.run_agent

    def scripted_run(spec, prompt, **kw):
        kw["client"] = C()
        return real_run(spec, prompt, **kw)

    monkeypatch.setattr(chat_mod, "run_agent", scripted_run)
    code = chat(manifest, log=log, input_fn=lambda _: next(lines), output_fn=outs.append)
    assert code == 0
    joined = "\n".join(outs)
    assert "zanzibar" in joined          # /transcript shows turn 1's history
    assert "[you]" in joined and "[agent]" in joined

    # the REPL produced one run in one session with full events
    runs = log.runs()
    assert len(runs) == 1 and runs[0]["status"] == "ok"
