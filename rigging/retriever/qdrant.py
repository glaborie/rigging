"""Qdrant retriever — the default implementation (ADR-0008).

Status: v0.3. Behind the `[qdrant]` extra: `pip install rigging[qdrant]`.

Design constraints already decided:
  * embeddings come from Ollama (`nomic-embed-text`, 768d) so the demo needs no API key
  * collection metadata stores the manifest fingerprint, so re-indexing is automatic
  * QDRANT_URL env var; compose publishes 6345 to stay clear of the host's other stacks
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

import httpx

from rigging.errors import RiggingError
from rigging.types import Chunk

EMBED_DIM = 768  # nomic-embed-text


def _embed(base_url: str, model: str, texts: list[str]) -> list[list[float]]:
    """Batch embeddings from a local Ollama — the demo stays credential-free."""
    resp = httpx.post(f"{base_url}/api/embed", json={"model": model, "input": texts}, timeout=60)
    resp.raise_for_status()
    return [list(vec) for vec in resp.json()["embeddings"]]


class QdrantRetriever:
    name = "qdrant"
    requires_indexing = True

    def __init__(self, url: str = "http://localhost:6345", collection: str = "rigging_policies",
                 embed_model: str = "nomic-embed-text",
                 embed_url: str | None = None) -> None:
        self.url, self.collection, self.embed_model = url, collection, embed_model
        self.embed_url = embed_url or os.environ.get("OLLAMA_URL", "http://localhost:11434")
        self._client: Any = None

    def _qdrant(self) -> Any:
        if self._client is None:
            try:
                from qdrant_client import QdrantClient  # behind the [qdrant] extra
            except ModuleNotFoundError as exc:
                raise RiggingError("qdrant-client not installed; pip install \'rigging[qdrant]\'") from exc
            self._client = QdrantClient(url=self.url)
        return self._client

    def index(self, documents: Sequence[Chunk]) -> int:
        if not documents:
            return 0
        client = self._qdrant()
        from qdrant_client import models

        if not client.collection_exists(self.collection):
            client.create_collection(self.collection, vectors_config=models.VectorParams(
                size=EMBED_DIM, distance=models.Distance.COSINE))
        vectors = _embed(self.embed_url, self.embed_model, [d.text for d in documents])
        client.upsert(self.collection, points=[models.PointStruct(
            id=abs(hash(d.id)) % (2**63), vector=vec, payload={"text": d.text, "source": d.source,
                                                               "metadata": d.metadata})
            for d, vec in zip(documents, vectors, strict=True)])
        return len(documents)

    def search(self, query: str, k: int = 5) -> list[Chunk]:
        client = self._qdrant()
        vec = _embed(self.embed_url, self.embed_model, [query])[0]
        hits = client.query_points(self.collection, query=vec, limit=k).points
        return [Chunk(id=str(h.id), text=h.payload.get("text", ""), source=h.payload.get("source", ""),
                      score=float(h.score), metadata=h.payload.get("metadata") or {}) for h in hits]
