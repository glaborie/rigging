"""Tools every agent gets a chance to use: skill loading and escalation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rigging.errors import ManifestError
from rigging.protocols import SkillResolver, Tool
from rigging.tools.schema import FunctionTool
from rigging.types import Handoff


def make_load_skill_tool(resolver: SkillResolver) -> Tool:
    """`load_skill(name)` — progressive disclosure's second half (ADR-0006)."""

    def load_skill(name: str) -> str:
        """Load a skill's full instructions. Call with a name from the skill index."""
        try:
            return resolver.load(name).body
        except KeyError:
            available = [m.name for m in resolver.discover()]
            return f"No skill {name!r}. Available: {', '.join(available)}"

    _tag(load_skill, "load_skill", "Load a skill's full instructions by name.")
    return FunctionTool(load_skill)


def make_escalate_tool(*, run_id_getter: Callable[[], str | None] = lambda: None) -> Tool:
    """`escalate_to_human` — a normal tool whose result is a structured handoff."""

    def escalate_to_human(reason: str, summary: str) -> str:
        """Hand the conversation to a human. Use when the request is out of policy,
        too vague to act on, or the customer asks for a person."""
        handoff = Handoff(reason=reason, summary=summary, transcript_run_id=run_id_getter())
        return f"HANDOFF {handoff.model_dump_json()}"

    _tag(escalate_to_human, "escalate_to_human", "Escalate to a human with a structured handoff.")
    return FunctionTool(escalate_to_human)


def make_search_tool(retriever: Any) -> Tool:
    """`search_policies(query)` — retrieval as an ordinary tool (ADR-0008)."""

    def search_policies(query: str, k: int = 5) -> str:
        """Search the policy corpus. Cite what you use."""
        chunks = retriever.search(query, k=k)
        if not chunks:
            return "No policy passages matched. Say so rather than guessing."
        return "\n\n".join(f"[{c.id}] ({c.source}, score {c.score:.2f})\n{c.text}" for c in chunks)

    _tag(search_policies, "search_policies", "Search policy documents for relevant passages.")
    return FunctionTool(search_policies)


def _tag(fn: Any, name: str, description: str, side_effecting: bool = False) -> None:
    fn.__rigging_tool__ = {"name": name, "description": description, "side_effecting": side_effecting}


def make_repo_tool(*_a: Any, **_k: Any) -> Tool:
    raise ManifestError("not implemented")
