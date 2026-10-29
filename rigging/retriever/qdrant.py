"""Qdrant retriever — the default implementation (ADR-0008).

Status: v0.3. Behind the `[qdrant]` extra: `pip install rigging[qdrant]`.

Design constraints already decided:
  * embeddings come from Ollama (`nomic-embed-text`, 768d) so the demo needs no API key
  * collection metadata stores the manifest fingerprint, so re-indexing is automatic
  * QDRANT_URL env var; compose publishes 6345 to stay clear of the host's other stacks
"""

from __future__ import annotations

from collections.abc import Sequence

from rigging.errors import RiggingError
from rigging.types import Chunk

NOT_IMPLEMENTED = "QdrantRetriever lands in v0.3; use the fts5 retriever until then."


class QdrantRetriever:
    name = "qdrant"
    requires_indexing = True

    def __init__(self, url: str = "http://localhost:6345", collection: str = "rigging_policies",
                 embed_model: str = "nomic-embed-text") -> None:
        self.url, self.collection, self.embed_model = url, collection, embed_model

    def search(self, query: str, k: int = 5) -> list[Chunk]:
        raise RiggingError(NOT_IMPLEMENTED)

    def index(self, documents: Sequence[Chunk]) -> int:
        raise RiggingError(NOT_IMPLEMENTED)
