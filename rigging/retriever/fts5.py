"""SQLite FTS5: the reference Retriever.

Not a fallback afterthought — it is the implementation that proves the protocol,
the offline test path, and the honest answer for a corpus under a few hundred KB
(BM25 over 8 policy documents beats embeddings on quality and determinism).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from rigging.types import Chunk


class FTS5Retriever:
    name = "fts5"

    def __init__(self, path: str = ":memory:", table: str = "chunks") -> None:
        self.path = path
        self.table = table
        self.requires_indexing = True
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING fts5("
            "id UNINDEXED, text, source UNINDEXED, metadata UNINDEXED)"
        )
        self._db.commit()

    def index(self, documents: Sequence[Chunk]) -> int:
        self._db.executemany(
            f"INSERT INTO {self.table}(id, text, source, metadata) VALUES (?,?,?,?)",
            [(d.id, d.text, d.source, str(d.metadata)) for d in documents],
        )
        self._db.commit()
        return len(documents)

    def search(self, query: str, k: int = 5) -> list[Chunk]:
        safe = " OR ".join(w for w in query.replace('"', " ").split() if w.isalnum()) or "policy"
        rows = self._db.execute(
            f"SELECT id, text, source, bm25({self.table}) AS score FROM {self.table} "
            f"WHERE {self.table} MATCH ? ORDER BY score LIMIT ?", (safe, k)
        ).fetchall()
        return [Chunk(id=r["id"], text=r["text"], source=r["source"], score=-float(r["score"])) for r in rows]
