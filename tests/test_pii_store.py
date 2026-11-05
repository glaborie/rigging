"""`harness pii open` and the table it reads (ADR-0027).

Sealing is proven in `test_crypto.py`; this file covers the *read* side: that the
command finds what a tool wrote, that it distinguishes a sealed value from a
legacy plaintext one, and that a value sealed with another key reads as
unreadable rather than as nothing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("cryptography", reason="needs the [vault] extra")

from rigging import cli
from rigging.config import settings
from rigging.crypto import load_key, new_key, seal
from rigging.pii_store import (
    NOTIFICATIONS_CONTACT,
    ORDERS_CONTACT,
    open_contacts,
)

ANNA = "anna.meier@nordwind-ag.ch"
JOE = "joe@wh.com"


def _db(tmp_path: Path, key: bytes | None) -> Path:
    """Two orders (one order id, two lines) and two notifications.

    With a key, every contact is sealed exactly the way the writer seals it; with
    None, they stay plaintext, which is the state of a database seeded before the
    key existed.
    """
    path = tmp_path / "demo.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE orders (order_id TEXT, sku TEXT, contact TEXT, PRIMARY KEY (order_id, sku));
    CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, contact TEXT);
    """)

    def sealed(value: str, aad: str) -> str:
        return value if key is None else seal(value, key, aad=aad)

    conn.executemany("INSERT INTO orders VALUES (?,?,?)", [
        ("ORD-1001", "A", sealed(ANNA, ORDERS_CONTACT.aad_for("ORD-1001"))),
        ("ORD-1001", "B", sealed(ANNA, ORDERS_CONTACT.aad_for("ORD-1001"))),
        ("ORD-1002", "A", None),
    ])
    conn.executemany("INSERT INTO notifications(contact) VALUES (?)",
                     [(sealed(JOE, NOTIFICATIONS_CONTACT.aad_for()),)])
    conn.commit()
    conn.close()
    return path


def test_it_opens_what_the_writer_sealed(tmp_path):
    key = load_key(new_key())
    records = open_contacts(_db(tmp_path, key), key)
    assert [r.value for r in records] == [ANNA, ANNA, JOE]
    assert all(r.state == "opened" for r in records)
    assert records[0].row == "ORD-1001" and records[-1].row == "1"


def test_a_legacy_plaintext_row_is_marked_not_silently_equal(tmp_path):
    """`opens` and `was sealed` are different questions; the command answers both."""
    records = open_contacts(_db(tmp_path, None), load_key(new_key()))
    assert records[0].value == ANNA, "a plaintext row still reads"
    assert records[0].state == "plaintext"


def test_one_unreadable_row_does_not_hide_its_neighbours(tmp_path):
    db = _db(tmp_path, load_key(new_key()))
    records = open_contacts(db, load_key(new_key()))
    assert [r.state for r in records] == ["unreadable", "unreadable", "unreadable"]
    assert all(r.value is None for r in records)


def test_the_table_filter_and_limit_are_applied(tmp_path):
    key = load_key(new_key())
    db = _db(tmp_path, key)
    assert [r.column.name for r in open_contacts(db, key, table="notifications")] == \
        ["notifications.contact"]
    assert len(open_contacts(db, key, limit=1)) == 1


def test_a_table_missing_from_an_old_database_is_skipped(tmp_path):
    key = load_key(new_key())
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE orders (order_id TEXT, sku TEXT, contact TEXT)")
    conn.commit()
    conn.close()
    assert open_contacts(path, key) == []  # no notifications table, no crash


# ------------------------------------------------------------------ the command


def _run(argv: list[str], key: str, monkeypatch) -> int:
    monkeypatch.setattr(settings, "pii_key", key)
    return cli.main(argv)


def test_the_command_prints_the_plaintext(tmp_path, monkeypatch, capsys):
    key = new_key()
    db = _db(tmp_path, load_key(key))
    assert _run(["pii", "open", str(db)], key, monkeypatch) == 0
    out = capsys.readouterr().out
    assert ANNA in out and JOE in out
    assert "3 sealed, 0 plaintext" in out.splitlines()[-1]


def test_the_command_refuses_without_a_key(tmp_path, monkeypatch, capsys):
    db = _db(tmp_path, None)
    assert _run(["pii", "open", str(db)], "", monkeypatch) == 2
    assert "RIGGING_PII_KEY is empty" in capsys.readouterr().err


def test_the_command_refuses_a_missing_database(tmp_path, monkeypatch, capsys):
    assert _run(["pii", "open", str(tmp_path / "nope.db")], new_key(), monkeypatch) == 2
    assert "no database at" in capsys.readouterr().err


def test_the_command_says_so_when_there_is_nothing_to_show(tmp_path, monkeypatch, capsys):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    assert _run(["pii", "open", str(path)], new_key(), monkeypatch) == 0
    assert "no stored contacts" in capsys.readouterr().err
