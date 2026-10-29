"""The interactive REPL (v0.2). One session, many runs, continuous memory.

Every turn is a real `run_agent` invocation with its own run_id and full event
trail — the REPL is a client of the loop, not a second implementation of it.
Escalation is the model calling `escalate_to_human`; the REPL surfaces the
handoff when the run carries one.
"""

from __future__ import annotations

import sys
from typing import Any

from rigging.loop import run_agent
from rigging.manifest import AgentSpec
from rigging.types import Message, ModelDelta, RunResult

HELP = """
/transcript   print the conversation so far
/new          start over (same session, fresh history)
/escalate     show the handoff from the last run, if any
/exit         quit (Ctrl-D works too)
anything else is sent to the agent"""

BANNER = "rigging chat — {spec} · {model}\ncommands:{help}"


def chat(spec: AgentSpec, *, log: Any, session_id: str | None = None,
         stream: Any = None, approval: Any = None, input_fn: Any = None,
         output_fn: Any = None) -> int:
    inp = input_fn or input
    out = output_fn or (lambda s: print(s, file=sys.stderr))
    history: list[Message] = []
    sid = log.start_session(session_id, title=spec.name, spec_hash=spec.spec_hash)
    last: RunResult | None = None

    out(BANNER.format(spec=spec.name, model=spec.model, help=HELP))
    while True:
        try:
            line = inp("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            out("")
            return 0
        if not line:
            continue
        if line == "/exit":
            return 0
        if line == "/help":
            out(HELP)
            continue
        if line == "/new":
            history = []
            out("history cleared")
            continue
        if line == "/transcript":
            for m in history:
                tag = {"system": "sys", "user": "you", "assistant": "agent", "tool": "tool"}.get(m.role, m.role)
                out(f"[{tag}] {str(m.content)[:400]}")
            continue
        if line == "/escalate":
            out("no handoff in the last run" if not (last and last.handoff)
                else f"HANDOFF reason={last.handoff.reason} run={last.handoff.transcript_run_id}\n{last.handoff.summary}")
            continue

        result = run_agent(spec, line, log=log, session_id=sid,
                           stream=stream, approval=approval, history=history)
        if result.status != "ok":
            out(f"! run {result.run_id} ended: {result.status}" + (f" — {result.error}" if result.error else ""))
        if result.handoff:
            out(f"(escalated: {result.handoff.reason})")
        out(f"agent> {result.output}")
        if result.history:
            history = result.history
        last = result
