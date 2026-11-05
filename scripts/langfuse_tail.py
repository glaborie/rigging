"""Read recent Langfuse observations — the "did my span land?" check.

Langfuse v4 reads observations directly: `GET /api/public/v2/observations` with a
bounded start-time window, cursor pagination and explicit field groups. The v1
`GET /api/public/traces` endpoint is removed on 2026-11-16 and is not used here.
Credentials come from `Settings`, which means `.env` works without exporting
anything.

Usage (normally via `make trace`):
    uv run python scripts/langfuse_tail.py --wait 60
    uv run python scripts/langfuse_tail.py --since-file /tmp/marker --wait 90
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from rigging.config import settings

DEFAULT_HOST = "https://cloud.langfuse.com"


def _host() -> str:
    return (settings.langfuse_host or DEFAULT_HOST).rstrip("/")


def _credentials() -> tuple[str, str]:
    pk, sk = settings.langfuse_public_key, settings.langfuse_secret_key
    if not pk or not sk:
        sys.exit("LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are empty in .env")
    return pk, sk


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def fetch(start: dt.datetime, end: dt.datetime, limit: int = 50) -> list[dict[str, Any]]:
    pk, sk = _credentials()
    response = httpx.get(
        f"{_host()}/api/public/v2/observations",
        params={
            "fromStartTime": _iso(start),
            "toStartTime": _iso(end),
            "limit": limit,
            # v2 returns only the requested groups; usage is not in core/basic.
            "fields": "core,basic,usage",
        },
        auth=(pk, sk),
        timeout=30.0,
    )
    if response.status_code != 200:
        sys.exit(f"langfuse read failed: {response.status_code} {response.text[:200]}")
    return response.json().get("data", [])


def show(rows: list[dict[str, Any]]) -> None:
    for row in sorted(rows, key=lambda r: r.get("startTime", ""), reverse=True):
        usage = row.get("usage") or {}
        tokens = ""
        if usage.get("input") or usage.get("output"):
            tokens = f" | tokens {usage.get('input', 0)}/{usage.get('output', 0)}"
        print(
            f"  {row.get('startTime', '?')}  {row.get('type', '?'):<10} "
            f"{row.get('name', '?'):<14} trace {(row.get('traceId') or '')[:8]}"
            f"{tokens}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, default=30, help="lookback window (default 30)")
    parser.add_argument("--since-file", type=Path,
                        help="only count observations at/after this file's mtime")
    parser.add_argument("--wait", type=int, default=0,
                        help="poll up to N seconds for a new observation to appear")
    parser.add_argument("--all", action="store_true", help="also print older rows in the window")
    args = parser.parse_args()

    marker = args.since_file.stat().st_mtime if args.since_file and args.since_file.exists() else None
    cutoff = dt.datetime.fromtimestamp(marker, dt.UTC) if marker else None
    start = (cutoff or dt.datetime.now(dt.UTC)) - dt.timedelta(minutes=args.minutes)
    deadline = time.monotonic() + args.wait
    announced = False

    while True:
        end = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=2)
        rows = fetch(start, end)
        fresh = [r for r in rows if cutoff is None or _parse(r["startTime"]) >= cutoff]
        if fresh:
            print(f"langfuse {_host()}: {len(fresh)} observation(s)")
            show(fresh)
            if args.all:
                older = [r for r in rows if r not in fresh]
                if older:
                    print("older in this window:")
                    show(older)
            return 0
        if time.monotonic() >= deadline:
            print(f"langfuse {_host()}: no observation since {_iso(cutoff or start)}")
            print("ingestion can lag a few seconds; re-run with --wait 60 to poll longer.")
            return 1
        if not announced:
            print(f"waiting up to {args.wait}s for a span to land…")
            announced = True
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
