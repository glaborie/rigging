"""Seed the b2b-support demo fixture database (deterministic, seed=7).

    uv run python examples/b2b-support/seed.py [--out examples/b2b-support/demo.db]

Writes three tables: products (SKU, MOQ, tier prices, lead time, stock),
orders (deterministic orders with deliverable refund scenarios), and the FTS5
`chunks` table built from policies/*.md — which the fts5 retriever searches
directly, so `make run` needs zero credentials and zero network (ADR-0008).
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
from pathlib import Path

HERE = Path(__file__).parent

PRODUCTS = [  # (sku, name, list_price, moq, lead_days, stock)
    ("CBL-CAT6-3M", "Cat6 patch cable 3m", 4.90, 10, 5, 1200),
    ("CBL-CAT6-10M", "Cat6 patch cable 10m", 12.50, 10, 5, 800),
    ("SW-8P-GBE", "8-port gigabit switch", 89.00, 2, 5, 340),
    ("SW-24P-GBE", "24-port gigabit switch", 289.00, 1, 15, 75),
    ("AP-WIFI6E", "WiFi 6E access point", 219.00, 2, 15, 210),
    ("UPS-1500VA", "UPS 1500VA line-interactive", 179.00, 1, 5, 90),
    ("RCK-12U", "12U wall rack", 145.00, 1, 15, 40),
    ("PDU-8-C13", "PDU 8x C13 metered", 119.00, 2, 5, 260),
    ("SFP-10G-LR", "SFP+ 10G LR transceiver", 129.00, 4, 5, 500),
    ("DAC-10G-1M", "10G DAC cable 1m", 32.00, 10, 5, 900),
    ("HDD-8TB-SATA", "8TB SATA HDD", 149.00, 4, 5, 310),
    ("SSD-1TB-NVME", "1TB NVMe SSD gen4", 89.00, 10, 5, 640),
    ("RAM-32G-D5", "32GB DDR5 ECC RDIMM", 219.00, 4, 15, 150),
    ("CPU-16C-X86", "16-core server CPU", 899.00, 1, 15, 25),
    ("PSU-750-PSU", "750W redundant PSU", 159.00, 2, 5, 180),
    ("FAN-N1U-HS", "1U high-static fan kit", 49.00, 10, 5, 420),
    ("KVM-IP-16", "16-port IP KVM", 449.00, 1, 15, 18),
    ("CAM-POE-4MP", "PoE camera 4MP", 139.00, 6, 5, 300),
    ("NVR-8CH-4K", "8-channel 4K NVR", 399.00, 1, 15, 55),
    ("TAG-RFID-PK", "RFID tags, pack of 100", 29.00, 5, 5, 800),
]

STATUSES = ["delivered", "shipped", "processing", "delivered", "delivered", "cancelled"]

ORDER_NOTES = [
    "", "customer asked to consolidate shipments", "priority handling requested",
    "replacement for damaged goods", "", "net-30 account", "",
]


def _money(x: float) -> float:
    return round(x, 2)


def build_db(out: Path, seed: int = 7) -> dict[str, int]:
    rng = random.Random(seed)
    if out.exists():
        out.unlink()
    db = sqlite3.connect(out)
    db.executescript("""
    CREATE TABLE products(sku TEXT PRIMARY KEY, name TEXT, list_price REAL,
                          moq INTEGER, lead_days INTEGER, stock INTEGER);
    CREATE TABLE orders(order_id TEXT PRIMARY KEY, sku TEXT, qty INTEGER,
                        unit_price REAL, status TEXT, delivered_at TEXT, note TEXT);
    """)
    db.executemany("INSERT INTO products VALUES (?,?,?,?,?,?)", PRODUCTS)

    # extra deterministic catalogue so lookups can hit volume tiers
    rows = []
    for i in range(20):
        sku = f"ACC-{100 + i}"
        price = _money(9.0 + (i * 7) % 90 + 0.5)
        rows.append((sku, f"Accessory line {i}", price, rng.choice([1, 2, 5, 10]),
                     rng.choice([5, 15]), rng.randrange(50, 900)))
    db.executemany("INSERT INTO products VALUES (?,?,?,?,?,?)", rows)

    orders = []
    for i in range(12):
        p = PRODUCTS[i % len(PRODUCTS)]
        qty = rng.choice([5, 12, 40, 60, 120, 250])
        tier = 1.0 if qty < 10 else 0.95 if qty < 50 else 0.90 if qty < 200 else 0.85
        status = STATUSES[i % len(STATUSES)]
        delivered = f"2026-{(i % 9) + 1:02d}-{(i % 27) + 1:02d}" if status == "delivered" else None
        orders.append((f"ORD-{1001 + i}", p[0], qty, _money(p[2] * tier), status, delivered,
                       ORDER_NOTES[i % len(ORDER_NOTES)]))
    # ORD-1002 is deliberately fresh and sealed/unopened: the demo prompt "refund
    # ORD-1002" must reach the approval gate, not die in the 30-day window check.
    orders[1] = (orders[1][0], orders[1][1], orders[1][2], orders[1][3],
                 "delivered", "2026-10-20", "sealed, unopened, in original packaging")
    db.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", orders)

    # policy chunks -> the FTS5 table the fts5 retriever searches
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5("
               "id UNINDEXED, text, source UNINDEXED, metadata UNINDEXED)")
    n = 0
    for path in sorted(HERE.glob("policies/*.md")):
        text = path.read_text(encoding="utf-8")
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        for j, para in enumerate(paras):
            db.execute("INSERT INTO chunks(id, text, source, metadata) VALUES (?,?,?,?)",
                       (f"{path.stem}#{j}", para, path.name, json.dumps({"policy": path.stem[:6]})))
            n += 1
    db.commit()
    counts = {"products": len(PRODUCTS) + len(rows), "orders": len(orders), "chunks": n}
    db.close()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="seed the b2b-support demo database")
    parser.add_argument("--out", default=str(HERE / "demo.db"))
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    counts = build_db(Path(args.out), args.seed)
    print("seeded", args.out, "->", ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
