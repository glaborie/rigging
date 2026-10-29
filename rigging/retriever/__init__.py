"""Retriever implementations (ADR-0008). Qdrant is the default, FTS5 the reference."""

from rigging.registry import DEFAULT_REGISTRY
from rigging.retriever.fts5 import FTS5Retriever
from rigging.retriever.qdrant import QdrantRetriever

DEFAULT_REGISTRY.add("retriever", FTS5Retriever.name, FTS5Retriever, replace=True)
DEFAULT_REGISTRY.add("retriever", QdrantRetriever.name, QdrantRetriever, replace=True)

__all__ = ["FTS5Retriever", "QdrantRetriever"]
