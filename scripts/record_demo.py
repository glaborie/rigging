"""Record scripts/demo.sh in a real pty and write an asciinema v2 cast.

Stdlib only, on purpose: a demo recorder is not worth a dependency. The cast is
the honest artifact (real bytes, real timings, replayable with `asciinema play`);
docs/demo.gif is rendered from it by scripts/cast_to_gif.py.

    uv run python scripts/record_demo.py            # -> docs/demo.cast
    uv run python scripts/record_demo.py --check    # print the cast, write nothing
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "docs" / "demo.cast"
COLS, ROWS = 112, 34


def record(command: list[str], cols: int, rows: int) -> list[tuple[float, str]]:
    master, slave = pty.openpty()
    # Give the child a real window size, or it will not wrap and the renderer
    # will have to guess where the terminal would have broken lines.
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    env = dict(os.environ, TERM="xterm-256color", COLUMNS=str(cols), LINES=str(rows),
               PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, env=env,
                            cwd=ROOT, start_new_session=True)
    os.close(slave)

    events: list[tuple[float, str]] = []
    began = time.monotonic()
    while True:
        ready, _, _ = select.select([master], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(master, 65536)
            except OSError:  # EIO once the child closes the slave
                break
            if not chunk:
                break
            events.append((round(time.monotonic() - began, 3), chunk.decode("utf-8", "replace")))
        elif proc.poll() is not None:
            break
    proc.wait()
    os.close(master)
    return events


def write_cast(path: Path, events: list[tuple[float, str]], cols: int, rows: int) -> None:
    header = {
        "version": 2,
        "width": cols,
        "height": rows,
        "timestamp": int(time.time()),
        "env": {"SHELL": "/bin/bash", "TERM": "xterm-256color"},
        "title": "rigging — 60-second demo",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(header) + "\n")
        for when, data in events:
            fh.write(json.dumps([when, "o", data]) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--cols", type=int, default=COLS)
    parser.add_argument("--rows", type=int, default=ROWS)
    parser.add_argument("--check", action="store_true", help="record and print, write nothing")
    args = parser.parse_args()

    script = ROOT / "scripts" / "demo.sh"
    events = record(["bash", str(script)], args.cols, args.rows)
    if not events:
        print("nothing was recorded — is scripts/demo.sh executable?", file=sys.stderr)
        return 1
    if args.check:
        sys.stdout.write("".join(data for _, data in events))
        return 0
    write_cast(args.out, events, args.cols, args.rows)
    duration = events[-1][0]
    print(f"wrote {args.out.relative_to(ROOT)} — {len(events)} events, {duration:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
