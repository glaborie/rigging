"""The one-off seal of pre-existing contacts (ADR-0027), and the seed's sealing.

Two paths can leave a contact plaintext after the feature ships: a fixture DB
seeded without a key, and rows written before a key existed. `scripts/seal_pii.py`
closes the second; `seed.build_db` closes the first when a key is configured.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import ModuleType

import pytest

pytest.importorskip("cryptography", reason="needs the [vault] extra")

from rigging.config import settings
from rigging.crypto import is_sealed, load_key, new_key, open_sealed

ROOT = Path(__file__).resolve().parents[1]


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("seal_pii_under_test",
                                                  ROOT / "scripts" / "seal_pii.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "demo.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE orders (order_id TEXT, sku TEXT, contact TEXT, PRIMARY KEY (order_id, sku));
    CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, contact TEXT,
                                message TEXT, sent_at TEXT);
    """)
    conn.executemany("INSERT INTO orders VALUES (?,?,?)",
                     [("ORD-1001", "A", "anna.meier@nordwind-ag.ch"),
                      ("ORD-1001", "B", "anna.meier@nordwind-ag.ch"),
                      ("ORD-1002", "A", None)])
    conn.executemany("INSERT INTO notifications(contact, message, sent_at) VALUES (?,?,?)",
                     [("joe@wh.com", "hi", "2026-11-05"), ("sealed", "x", "2026-11-05")])
    conn.commit()
    conn.close()
    return path


def test_it_seals_plaintext_and_skips_ciphertext(tmp_path):
    key = load_key(new_key())
    db = _db(tmp_path)
    already = "enc:v1:not-really-a-value"
    conn = sqlite3.connect(db)
    conn.execute("UPDATE notifications SET contact = ? WHERE message = 'x'", (already,))
    conn.commit()
    conn.close()

    counts = _script().seal_database(db, key)
    assert counts == {"orders": 2, "notifications": 1}

    conn = sqlite3.connect(db)
    contacts = [r[0] for r in conn.execute("SELECT contact FROM orders WHERE contact IS NOT NULL")]
    conn.close()
    assert all(is_sealed(c) for c in contacts)
    assert contacts[0] == contacts[1], "the same address seals to the same AAD input"
    assert open_sealed(contacts[0], key, aad="orders.contact:ORD-1001") == \
        "anna.meier@nordwind-ag.ch"


def test_it_is_idempotent(tmp_path):
    key = load_key(new_key())
    db = _db(tmp_path)
    script = _script()
    script.seal_database(db, key)
    assert script.seal_database(db, key) == {"orders": 0, "notifications": 0}


def test_it_needs_a_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(settings, "pii_key", "")
    monkeypatch.delenv("RIGGING_PII_KEY", raising=False)
    db = _db(tmp_path)
    assert _script().main([str(db)]) == 2
    assert "RIGGING_PII_KEY is empty" in capsys.readouterr().err


def test_the_seed_seals_when_a_key_is_set(tmp_path, monkeypatch):
    import sys

    sys.path.insert(0, str(ROOT / "examples" / "b2b-support"))
    import seed

    path = tmp_path / "demo.db"
    seed.build_db(path, seed=7)
    plain = sqlite3.connect(path).execute(
        "SELECT contact FROM orders LIMIT 1").fetchone()[0]
    assert "@" in plain and not is_sealed(plain), "no key: the fixture stays readable"

    monkeypatch.setattr(settings, "pii_key", new_key())
    keyed = tmp_path / "demo-keyed.db"
    seed.build_db(keyed, seed=7)
    row = sqlite3.connect(keyed).execute(
        "SELECT order_id, contact FROM orders WHERE contact IS NOT NULL LIMIT 1").fetchone()
    assert is_sealed(row[1]) and "@" not in row[1]
    assert open_sealed(row[1], load_key(settings.pii_key),
                       aad=f"orders.contact:{row[0]}").startswith("anna.meier@")
