"""rigging — a modular agent harness.

Public surface, stable enough to build against:

    from rigging import Registry, load_manifest, run_agent
    from rigging.protocols import Tool, Guardrail, Retriever, LLMClient
"""

from rigging.errors import (
    CapabilityNotFound,
    ManifestError,
    RiggingError,
    TransportError,
)
from rigging.loop import run_agent
from rigging.manifest import load_manifest
from rigging.registry import Registry, guardrail, retriever, tool
from rigging.types import (
    Chunk,
    Handoff,
    Message,
    ModelDelta,
    ModelRequest,
    ModelResponse,
    RunResult,
    ToolCall,
    ToolResult,
    Usage,
)

__version__ = "0.7.0"

__all__ = [
    "CapabilityNotFound",
    "Chunk",
    "Handoff",
    "ManifestError",
    "Message",
    "ModelDelta",
    "ModelRequest",
    "ModelResponse",
    "Registry",
    "RiggingError",
    "RunResult",
    "ToolCall",
    "ToolResult",
    "TransportError",
    "Usage",
    "guardrail",
    "load_manifest",
    "retriever",
    "run_agent",
    "tool",
]
