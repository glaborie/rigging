from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from rigging.loop import assemble
from rigging.manifest import AgentSpec, McpServerCfg


class FakeSession:
    def __init__(self) -> None:
        self.initialized = False

    async def initialize(self) -> None:
        self.initialized = True

    async def list_tools(self) -> Any:
        schema = {"type": "object", "properties": {"q": {"type": "string"}}}

        class _Tool:
            name = "search"
            description = "Search the wiki"

            def __init__(self) -> None:
                self.inputSchema = schema

        listing_tools = [_Tool()]

        class _Listing:
            def __init__(self) -> None:
                self.tools = listing_tools

        return _Listing()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        content_text = f"{name}:{arguments.get('q', '')}"

        class _Text:
            type = "text"

            def __init__(self) -> None:
                self.text = content_text

        blocks = [_Text()]

        class _Result:
            def __init__(self) -> None:
                self.content = blocks

        return _Result()


@pytest.fixture()
def fake_collect(monkeypatch: pytest.MonkeyPatch) -> Any:
    session = FakeSession()

    @asynccontextmanager
    async def _factory():
        yield session

    real_collect = __import__("rigging.mcp", fromlist=["collect"]).collect

    def _with_session(cfgs: list[McpServerCfg], **kwargs: Any) -> list[Any]:
        kwargs.pop("session_factory", None)
        return real_collect(cfgs, session_factory=_factory, **kwargs)

    monkeypatch.setattr("rigging.mcp.collect", _with_session)
    return session


def test_manifest_mcp_tools_are_namespaced_and_resolved(fake_collect: Any) -> None:
    spec = AgentSpec(
        model="m",
        tools=["demo__search"],
        mcp_servers=[McpServerCfg(name="demo", transport="stdio", command="unused")],
    )
    agent = assemble(spec)
    names = [t.name for t in agent.tools]
    assert "demo__search" in names
    tool = next(t for t in agent.tools if t.name == "demo__search")
    result = tool.run(
        __import__("rigging.types", fromlist=["ToolCall"]).ToolCall(
            id="c1", name="demo__search", arguments={"q": "refund window"}
        )
    )
    assert "search:refund window" in result.content


def test_unknown_mcp_tool_name_fails_assembly(fake_collect: Any) -> None:
    from rigging.errors import RiggingError

    spec = AgentSpec(
        model="m",
        tools=["demo__missing"],
        mcp_servers=[McpServerCfg(name="demo", transport="stdio", command="unused")],
    )
    with pytest.raises(RiggingError, match="demo__missing"):
        assemble(spec)
