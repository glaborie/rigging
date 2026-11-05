"""Where the harness's stored PII lives, and what binds each value (ADR-0027).

`crypto.py` seals one string and knows nothing about tables; this module knows the
three things a reader or a writer must agree on: which columns hold a contact,
which column identifies the row, and the AAD template that ties a ciphertext to
its place (`orders.contact:<order id>`, `notifications.contact`).

Both users of the primitive read this table — `harness pii open` to decrypt, and
`scripts/seal_pii.py` to migrate pre-existing rows. Keeping the templates in one
place is not tidiness: a reader and a writer that disagree about the AAD produce a
value nobody can open, and the failure only shows up at read time.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from rigging.crypto import is_sealed, open_sealed
from rigging.errors import CryptoError


@dataclass(frozen=True)
class SealedColumn:
    """One column that may hold a sealed contact, and the AAD that binds it."""

    table: str
    column: str
    #: Identifies a row in `table` — `order_id` for orders, `id` for notifications.
    row_column: str
    #: `{row}` is filled with the row identity; the writer and the reader share it.
    aad: str

    def aad_for(self, row_key: str = "") -> str:
        """The AAD for one row. A template without `{row}` ignores the argument.

        Writers that know the row identity before the row exists (an order id is
        chosen, a notification id is not) and readers that have the row both go
        through here, so neither can invent a different binding.
        """
        return self.aad.format(row=row_key)

    @property
    def name(self) -> str:
        return f"{self.table}.{self.column}"


ORDERS_CONTACT = SealedColumn("orders", "contact", "order_id", "orders.contact:{row}")
NOTIFICATIONS_CONTACT = SealedColumn("notifications", "contact", "id", "notifications.contact")

#: Every place a tool writes a contact (ADR-0027). Add a column here, not in a
#: call site: the demo tools seal with the same AAD these templates produce.
SEALED_COLUMNS: tuple[SealedColumn, ...] = (ORDERS_CONTACT, NOTIFICATIONS_CONTACT)


@dataclass(frozen=True)
class StoredPII:
    """One value read back from disk, with enough context to judge it."""

    column: SealedColumn
    row: str
    #: Exactly what is in the file: `enc:v1:...` after sealing, plaintext otherwise.
    stored: str
    #: The plaintext, or None when the value does not open with this key.
    value: str | None

    @property
    def sealed(self) -> bool:
        return is_sealed(self.stored)

    @property
    def state(self) -> str:
        """`sealed` alone does not mean readable — the two are worth telling apart."""
        if not self.sealed:
            return "plaintext"
        return "opened" if self.value is not None else "unreadable"


def open_contacts(db: Path, key: bytes, *, table: str | None = None,
                  limit: int | None = None) -> list[StoredPII]:
    """Read the stored contacts in `db` and open what can be opened.

    A column missing from an older database is skipped, not an error: `orders` was
    not always there, and an unauthorised read is not the place to enforce schema.
    A value that does not authenticate keeps `value=None` — one bad row must not
    hide the good ones next to it.
    """
    records: list[StoredPII] = []
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        for spec in SEALED_COLUMNS:
            if table is not None and spec.table != table:
                continue
            try:
                rows = conn.execute(
                    f"SELECT * FROM {spec.table} WHERE {spec.column} IS NOT NULL").fetchall()
            except sqlite3.OperationalError:
                continue
            for row in rows:
                stored = row[spec.column]
                if not isinstance(stored, str):
                    continue
                try:
                    value: str | None = open_sealed(
                        stored, key, aad=spec.aad_for(str(row[spec.row_column])))
                except CryptoError:
                    value = None
                records.append(StoredPII(spec, str(row[spec.row_column]), stored, value))
                if limit is not None and len(records) >= limit:
                    return records
    finally:
        conn.close()
    return records
