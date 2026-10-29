"""MCP as a tool source (ADR-0013: consume-only, namespaced names).

Rigging is an MCP *client*, never a server: the harness consumes other people's
MCP servers the way a browser consumes websites. Tool names are namespaced
`<server>__<tool>` so two servers can expose `search` without a collision — and
so the manifest stays explicit about which tools an agent may use.

Behind the `[mcp]` extra. Stdio and streamable-HTTP transports. The MCP SDK is
imported lazily and only inside the transport layer, so the rest of this module
(is the protocol adapter) is testable offline with session doubles.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from rigging.errors import CapabilityNotFound, RiggingError
from rigging.manifest import McpServerCfg
from rigging.types import ToolCall, ToolResult

SessionFactory = Callable[[], Any]
"""Returns an async context manager yielding an initialised MCP session."""


def _require_sdk() -> None:
    try:
        import mcp  # noqa: F401
    except ModuleNotFoundError as exc:
        raise RiggingError("the MCP SDK is not installed; pip install 'rigging[mcp]'") from exc


class McpTool:
    """A discovered MCP tool wrapped as a harness `Tool` (namespaced name)."""

    def __init__(self, source: McpToolSource, spec: dict[str, Any]) -> None:
        self._source = source
        self._bare = spec["name"]
        self.name = f"{source.cfg.name}__{self._bare}"
        self.description = spec.get("description") or f"MCP tool {self._bare} from {source.cfg.name}"
        self.json_schema = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": _normalise_schema(spec.get("input_schema")),
            },
        }
        # A source's tools may write on the far side; until an MCP server
        # declares effect-ness, approval policy decides via the manifest.
        self.side_effecting = False

    def run(self, call: ToolCall) -> ToolResult:
        try:
            content = self._source.call(self._bare, call.arguments)
        except Exception as exc:  # any MCP failure is a tool message, never a crash
            return ToolResult.error(call, f"{type(exc).__name__}: {exc}")
        return ToolResult(call_id=call.id, name=self.name, content=content)

    def __repr__(self) -> str:
        return f"<McpTool {self.name} from {self._source.cfg.name}>"


def _normalise_schema(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    return {"type": "object", "properties": {}}


class McpToolSource:
    """Connects to one MCP server and exposes its tools as harness tools."""

    def __init__(
        self,
        cfg: McpServerCfg,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        if session_factory is None:
            _require_sdk()
        self.cfg = cfg
        self._session_factory = session_factory or self._default_session_factory
        self._tools: list[dict[str, Any]] = []

    # -- connection -------------------------------------------------------
    def session_cm(self) -> Any:
        """Async context manager yielding an initialised MCP session."""
        if self.cfg.transport == "stdio":
            if not self.cfg.command:
                raise RiggingError(f"MCP server {self.cfg.name!r}: stdio needs `command`")
            return self._stdio_cm()
        if not self.cfg.url:
            raise RiggingError(f"MCP server {self.cfg.name!r}: http needs `url`")
        return self._http_cm()

    def _default_session_factory(self) -> Any:
        return self.session_cm()

    def _stdio_cm(self) -> Any:
        from collections.abc import AsyncIterator
        from contextlib import asynccontextmanager

        command = self.cfg.command
        if not command:  # narrowed for mypy; session_cm() already validated
            raise RiggingError(f"MCP server {self.cfg.name!r}: stdio needs `command`")

        @asynccontextmanager
        async def _cm() -> AsyncIterator[Any]:
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client

            params = StdioServerParameters(command=command, args=self.cfg.args)
            async with (
                stdio_client(params) as (read, write),
                ClientSession(read, write) as session,
            ):
                yield session

        return _cm()

    def _http_cm(self) -> Any:
        from collections.abc import AsyncIterator
        from contextlib import asynccontextmanager

        url = self.cfg.url
        if not url:  # narrowed for mypy; session_cm() already validated
            raise RiggingError(f"MCP server {self.cfg.name!r}: http needs `url`")

        @asynccontextmanager
        async def _cm() -> AsyncIterator[Any]:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client

            async with (
                streamable_http_client(url) as (read, write),
                ClientSession(read, write) as session,
            ):
                yield session
                yield session

        return _cm()

    # -- discovery --------------------------------------------------------
    def discover(self) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            async with self._session_factory() as session:
                await session.initialize()
                listed = await session.list_tools()
                return [
                    {
                        "name": t.name,
                        "description": getattr(t, "description", None) or "",
                        "input_schema": getattr(t, "inputSchema", None),
                    }
                    for t in listed.tools
                ]

        self._tools = asyncio.run(_run())
        return self._tools

    # -- invocation -------------------------------------------------------
    def call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        async def _run() -> str:
            async with self._session_factory() as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                parts: list[str] = []
                for block in result.content:
                    text = getattr(block, "text", None)
                    parts.append(
                        text
                        if text is not None
                        else json.dumps({"type": getattr(block, "type", "unknown")})
                    )
                return "\n".join(parts)

        return asyncio.run(_run())

    # -- tool objects -----------------------------------------------------
    def tools(self, *, names: list[str] | None = None) -> list[McpTool]:
        """Wrap discovered tools; `names` are the bare MCP names (pre-namespace)."""
        if not self._tools:
            self.discover()
        wanted = set(names) if names else {t["name"] for t in self._tools}
        return [McpTool(self, spec) for spec in self._tools if spec["name"] in wanted]


def collect(
    cfgs: list[McpServerCfg],
    *,
    allow: list[str] | None = None,
    session_factory: SessionFactory | None = None,
) -> list[McpTool]:
    """Discover + wrap tools from every configured server.

    `allow` is the manifest's tool list: a namespaced name opts that tool in, and a
    bare `server__*` is not magic — explicitness is the point (ADR-0013).
    """
    tools: list[McpTool] = []
    for cfg in cfgs:
        source = McpToolSource(cfg, session_factory=session_factory)
        discovered = source.discover()
        bare_names = [
            t["name"]
            for t in discovered
            if not allow or f"{cfg.name}__{t['name']}" in allow
        ]
        if not bare_names:
            continue
        tools.extend(source.tools(names=bare_names))
    if allow:
        missing = [name for name in allow if name not in {t.name for t in tools}]
        if missing:
            available = [f"{cfg.name}__{t.name}" for cfg in cfgs for t in tools]
            raise CapabilityNotFound("mcp tool", missing, available)
    return tools
