# ADR-0013: MCP servers are a tool source, never an MCP server

**Status:** Accepted

## Context

MCP is where tool ecosystems live, and the user's prior disappointment with an
SDK that only did MCP is a strong signal about what a harness should and should not
be. Both consuming and serving are possible; only one is the harness's job.

## Decision

`mcp_servers: [{name, command|url, transport}]` in the manifest, loaded in v0.4
through the registry as an adapter: tools listed over MCP and registered with
namespaced names (`github__create_issue`), JSON Schema mapped to `Tool.json_schema`.
Consume only. No MCP-specific code in the loop.

## Consequences

The registry proves itself against a third-party protocol with ~200 lines, and the
demo can borrow real tools. Cost: an extra dependency (`mcp`) behind an extra, and
namespacing rules to keep collisions out.

## Alternatives rejected

Serving rigging's tools as an MCP server: rejected as feature creep — it belongs
to the service milestone if ever. Skipping MCP: cheaper, but loses the strongest
available demonstration of the registry design.
