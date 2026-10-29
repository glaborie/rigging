from __future__ import annotations

import json
from typing import Any

import pytest

from rigging.errors import RiggingError
from rigging.manifest import McpServerCfg
from rigging.types import ToolCall, ToolResult


class FakeSession:
    """MCP-session double: list_tools/call_tool over a canned table."""

    def __init__(self, tools: list[dict[str, Any]], results: dict[str, str] | None = None) -> None:
        self.tools = tools
        self.results = results or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.initialized = False

    async def initialize(self) -> None:
        self.initialized = True

    async def list_tools(self) -> Any:
        class _Tool:
            def __init__(self, spec: dict[str, Any]) -> None:
                self.name = spec["name"]
                self.description = spec.get("description")
                self.inputSchema = spec.get("input_schema")

        class _Listing:
            def __init__(self, tools: list[dict[str, Any]]) -> None:
                self.tools = [_Tool(t) for t in tools]

        return _Listing(self.tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))

        class _Text:
            def __init__(self, text: str) -> None:
                self.text = text
                self.type = "text"

        class _Result:
            def __init__(self, text: str) -> None:
                self.content = [_Text(text)]

        if name not in self.results:
            raise RuntimeError(f"unknown tool: {name}")
        return _Result(self.results[name])


def _source(tools: list[dict[str, Any]], session: FakeSession) -> Any:
    from contextlib import asynccontextmanager

    from rigging.mcp import McpToolSource

    @asynccontextmanager
    async def _factory():
        yield session

    return McpToolSource(
        McpServerCfg(name="demo", transport="stdio", command="unused"),
        session_factory=_factory,
    )


def test_discover_lists_tools_from_the_session() -> None:
    session = FakeSession([
        {"name": "search", "description": "Search the wiki",
         "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}},
    ])
    source = _source([], session)
    specs = source.discover()
    assert [s["name"] for s in specs] == ["search"]
    assert specs[0]["input_schema"]["properties"]["q"]["type"] == "string"


def test_tools_are_namespaced_server__tool_and_satisfy_the_tool_protocol() -> None:
    session = FakeSession(
        [{"name": "search", "description": "Search the wiki", "input_schema": {"type": "object"}}],
        {"search": "wiki page"},
    )
    source = _source([], session)
    tools = source.tools()
    assert [t.name for t in tools] == ["demo__search"]
    assert tools[0].json_schema["function"]["name"] == "demo__search"
    assert tools[0].side_effecting is False
    result = tools[0].run(ToolCall(id="c1", name="demo__search", arguments={"q": "refund"}))
    assert isinstance(result, ToolResult)
    assert result.content == "wiki page"
    assert session.calls == [("search", {"q": "refund"})]


def test_tool_errors_become_error_tool_results_not_exceptions() -> None:
    session = FakeSession([{"name": "search", "input_schema": {}}])  # no results entry
    source = _source([], session)
    tool = source.tools()[0]
    result = tool.run(ToolCall(id="c1", name="demo__search", arguments={}))
    assert result.is_error is True
    assert "unknown tool" in result.content


def test_collect_filters_by_the_manifest_allow_list() -> None:
    from contextlib import asynccontextmanager

    from rigging.mcp import collect

    @asynccontextmanager
    async def _factory():
        yield session

    session = FakeSession(
        [
            {"name": "search", "input_schema": {}},
            {"name": "danger", "input_schema": {}},
        ],
        {"search": "ok"},
    )
    tools = collect(
        [McpServerCfg(name="demo", transport="stdio", command="unused")],
        allow=["demo__search"],
        session_factory=_factory,
    )
    assert [t.name for t in tools] == ["demo__search"]


def test_stdio_requires_a_command_and_http_requires_a_url() -> None:
    from rigging.mcp import McpToolSource

    with pytest.raises(RiggingError, match="stdio needs"):
        McpToolSource(McpServerCfg(name="x", transport="stdio")).session_cm()
    with pytest.raises(RiggingError, match="http needs"):
        McpToolSource(McpServerCfg(name="x", transport="http")).session_cm()


def test_sdk_missing_raises_a_helpful_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "mcp" or name.startswith("mcp."):
            raise ModuleNotFoundError("No module named 'mcp'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    from rigging.mcp import McpToolSource

    with pytest.raises(RiggingError, match=r"rigging\[mcp\]"):
        McpToolSource(
            McpServerCfg(name="x", transport="stdio", command="echo"),
            session_factory=None,
        )


@pytest.mark.live
def test_live_stdio_echo_server_round_trip() -> None:
    """Opt-in: needs the [mcp] extra and python on PATH."""
    from rigging.mcp import McpToolSource

    source = McpToolSource(
        McpServerCfg(name="echo", transport="stdio", command="python3", args=["-c", _ECHO_SERVER])
    )
    tools = source.tools()
    assert tools, "echo server exposed no tools"
    named = {t.name for t in tools}
    assert "echo__shout" in named
    tool = next(t for t in tools if t.name == "echo__shout")
    result = tool.run(ToolCall(id="c1", name="echo__shout", arguments={"text": "hi"}))
    assert result.content == "HI"


_ECHO_SERVER = (
    "import mcp.server.mcpserver\n"
    "mcp = mcp.server.mcpserver.MCPServer('echo')\n"
    "@mcp.tool()\n"
    "def shout(text: str) -> str:\n"
    "    '''Uppercase the text.'''\n"
    "    return text.upper()\n"
    "if __name__ == '__main__':\n"
    "    mcp.run()\n"
)


def test_wire_payload_carries_the_mcp_input_schema() -> None:
    schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    session = FakeSession([{"name": "search", "description": "Search", "input_schema": schema}])
    source = _source([], session)
    wire = source.tools()[0].json_schema
    assert wire["function"]["parameters"] == json.loads(json.dumps(schema))
