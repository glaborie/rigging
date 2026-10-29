# ADR-0008: Retrieval is a tool backed by a Retriever protocol

**Status:** Accepted

## Context

The demo agent needs policy documents and order data. Retrieval could become a
first-class harness concept (a `knowledge:` manifest section with automatic
context injection) or stay outside the loop as a tool. The user's existing
multimodal search engine should be pluggable later without harness changes.

## Decision

`Retriever` protocol: `search(query, k) -> list[Chunk]`, plus `requires_indexing`
and `index(documents)`. Qdrant is the default implementation (embeddings via Ollama
`nomic-embed-text`, 768d, port 6345 in compose to avoid the host's other Qdrant
instances); SQLite FTS5 is the reference implementation and the offline test path.
The agent sees `search_policies(query)` as a normal tool. `harness index` is a core
CLI command because making retrieval work is part of being a harness.

## Consequences

The loop stays ignorant of embeddings, and swapping retrievers is one class. Cost:
the model must decide to search (no guaranteed context), which is a prompt-design
problem — and a good one, measured by eval cases that require citation.

## Alternatives rejected

`knowledge:` section with auto-injection: burns context on every turn and hides
retrieval from observability. Policies inlined in a skill: fine for 20KB, but does
not demonstrate the protocol. Vector DB in core: rejected, it is an implementation.
