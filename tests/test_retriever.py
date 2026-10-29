from __future__ import annotations

from rigging.retriever.fts5 import FTS5Retriever
from rigging.types import Chunk


def test_fts5_index_and_search() -> None:
    r = FTS5Retriever()
    n = r.index([
        Chunk(id="p1", text="Returns are accepted within 30 days of delivery.", source="returns.md"),
        Chunk(id="p2", text="Refunds require a returns authorisation number.", source="refunds.md"),
    ])
    assert n == 2
    hits = r.search("returns", k=2)
    assert hits and hits[0].id in {"p1", "p2"}
    assert r.search("zzzz-no-such-term") is not None
