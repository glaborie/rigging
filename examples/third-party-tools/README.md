# Third-party tool registration example

This directory demonstrates how a third-party package can register tools,
guardrails, retrievers, and decision models via Python entry points.

## The pattern

A third-party package adds these lines to its `pyproject.toml`:

```toml
[project.entry-points."rigging.tools"]
my_tools = "my_package:register"

[project.entry-points."rigging.guardrails"]
my_guard = "my_package:register_guardrails"
```

And exports a registration function:

```python
# my_package/__init__.py
from rigging import tool, guardrail, Registry

def register(reg: Registry) -> None:
    @tool("my_tool", description="Does something useful", side_effecting=True)
    def my_tool(x: str) -> str:
        return f"processed: {x}"
```

## How it works

1. User installs the package: `pip install my_package`
2. When `harness run` or `run_agent()` is called without an explicit registry,
   `discover_entry_points()` is called automatically during assembly
3. The discovered capabilities are logged to stderr
4. The manifest can now reference `my_tool` by name

## Verification

Run the harness tools list:

```bash
uv run harness tools
```

This shows all registered capabilities. Third-party tools appear alongside
core tools like `current_time` and `sha256_hex`.