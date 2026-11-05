#!/usr/bin/env python3
"""Seal plaintext PII that is already sitting in a demo database (ADR-0027).

Two things stay plaintext after the feature ships, on purpose and by history:

  * fixture rows from a `make seed` that ran without `RIGGING_PII_KEY`;
  * rows written before the key existed at all.

This rewrites them in place with the same AAD the readers use — `orders.contact`
per order id, `notifications.contact` per column — so `lookup_order` keeps
returning the address while the file stops carrying it. Idempotent: a value that
already starts with `enc:v1:` is left alone.

    RIGGING_PII_KEY=... uv run python scripts/seal_pii.py examples/b2b-support/demo.db

SQLite keeps the old bytes in free pages after an UPDATE, so `--vacuum` is what
makes "the file no longer contains the address" true rather than nearly true.
Take a copy of the database first if the rows matter: a wrong key here means the
sealed value cannot be opened again, by this script or by a tool.
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

from rigging.config import settings
from rigging.crypto import CryptoError, is_sealed, load_key, seal
from rigging.pii_store import SEALED_COLUMNS


def seal_database(db: Path, key: bytes, *, vacuum: bool = False) -> dict[str, int]:
    """Seal every plaintext contact in `db`. Returns counts per table.

    A key mismatch on an existing ciphertext is not this script's problem: sealed
    values are skipped, never re-sealed, so running it twice cannot double-wrap a
    row or turn an unreadable value into a readable one.
    """
    counts: dict[str, int] = {}
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        for spec in SEALED_COLUMNS:
            try:
                rows = conn.execute(
                    f"SELECT * FROM {spec.table} "
                    f"WHERE {spec.column} IS NOT NULL").fetchall()
            except sqlite3.OperationalError:
                continue  # the table is optional in an older demo DB
            sealed = 0
            for row in rows:
                value = row[spec.column]
                if not isinstance(value, str) or is_sealed(value):
                    continue
                conn.execute(
                    f"UPDATE {spec.table} SET {spec.column} = ? WHERE {spec.row_column} = ?",
                    (seal(value, key, aad=spec.aad_for(str(row[spec.row_column]))),
                     row[spec.row_column]))
                sealed += 1
            counts[spec.table] = sealed
            if sealed:
                print(f"{spec.name}: sealed {sealed} value(s)", file=sys.stderr)
        conn.commit()
    finally:
        conn.close()
    if vacuum:
        conn = sqlite3.connect(db)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()
        print("vacuumed: free pages no longer hold the old bytes", file=sys.stderr)
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="seal plaintext PII in a demo database")
    ap.add_argument("db", help="path to the SQLite database (e.g. examples/b2b-support/demo.db)")
    ap.add_argument("--vacuum", action="store_true",
                    help="rewrite the file so UPDATE'd plaintext does not survive in free pages")
    a = ap.parse_args(argv)
    db = Path(a.db)
    if not db.exists():
        print(f"no database at {db}", file=sys.stderr)
        return 2
    raw = os.environ.get("RIGGING_PII_KEY") or settings.pii_key
    if not raw.strip():
        print("RIGGING_PII_KEY is empty: nothing to seal with. Set it in the environment "
              "or .env (see .env.example).", file=sys.stderr)
        return 2
    try:
        key = load_key(raw)
    except CryptoError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    counts = seal_database(db, key, vacuum=a.vacuum)
    if not sum(counts.values()):
        print("nothing to do: every contact is already sealed", file=sys.stderr)
    print(f"sealed: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
