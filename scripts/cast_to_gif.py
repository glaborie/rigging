"""Render an asciinema v2 cast to a GIF with ffmpeg.

No Python imaging dependency: ffmpeg's `drawtext` draws one row per line and
`enable='gte(t,T)'` reveals each row when it actually arrived, so the GIF keeps
the recording's timing. Long silent gaps (a model thinking) are capped so a
60-second demo stays watchable.

    uv run python scripts/cast_to_gif.py                    # docs/demo.cast -> docs/demo.gif
    uv run python scripts/cast_to_gif.py --max-gap 0.5 --fps 6
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
BG = "0x101418"
FG = "0xd7dee7"
ACCENT = "0x7dd3a7"
STAGGER_S = 0.06  # minimum gap between rows revealed together
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]")


def read_cast(path: Path) -> tuple[int, int, list[tuple[float, str]]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    events = []
    for line in lines[1:]:
        when, kind, data = json.loads(line)
        if kind == "o":
            events.append((float(when), data))
    return int(header["width"]), int(header["height"]), events


def screen(events: list[tuple[float, str]], cols: int, rows: int,
           max_gap: float) -> tuple[list[str], list[float], float]:
    """Replay the byte stream into final rows, each with the time it appeared."""
    grid: list[list[str]] = [[] for _ in range(rows * 2)]  # room to scroll
    arrival = [0.0] * len(grid)
    row = col = 0
    pending_wrap = False  # a real terminal wraps lazily: CR cancels the wrap
    clock = 0.0
    previous = 0.0

    def new_row(at: float) -> None:
        nonlocal row
        row += 1
        if row >= len(grid):
            grid.append([])
            arrival.append(at)

    for when, data in events:
        clock += min(max(when - previous, 0.0), max_gap)
        previous = when
        for ch in ANSI.sub("", data):
            if ch == "\n":
                pending_wrap = False
                col = 0
                new_row(clock)
            elif ch == "\r":
                pending_wrap = False
                col = 0
            elif ch == "\b":
                col = max(0, col - 1)
                pending_wrap = False
            elif ch >= " " or ch == "\t":
                if pending_wrap:
                    col, pending_wrap = 0, False
                    new_row(clock)
                while len(grid[row]) < col:
                    grid[row].append(" ")
                if col < len(grid[row]):
                    grid[row][col] = ch
                else:
                    grid[row].append(ch)
                if arrival[row] == 0.0 and ch.strip():
                    arrival[row] = clock
                col += 1
                pending_wrap = col >= cols
    lines = ["".join(r).rstrip() for r in grid]
    visible = [i for i, line in enumerate(lines) if line.strip()]
    if not visible:
        return [], [], clock
    # A real terminal scrolls: keep the last screenful, not the whole session.
    last = visible[-1]
    first = max(visible[0], last - (rows - 1))
    # Rows that arrived in one burst (a whole JSON trail) get a small stagger so
    # the GIF reads as output appearing, not as a wall of text.
    kept = lines[first:last + 1]
    times = arrival[first:last + 1]
    staggered: list[float] = []
    previous = 0.0
    for when in times:
        when = max(when, previous + STAGGER_S)
        staggered.append(when)
        previous = when
    return kept, staggered, max(clock, previous)


def render(rows: list[str], times: list[float], duration: float, out: Path,
           fps: int, fontsize: int) -> None:
    advance = fontsize * 0.6
    line_height = fontsize + 7
    width = int(max((len(r) for r in rows), default=40) * advance) + 48
    height = len(rows) * line_height + 40
    width += width % 2
    height += height % 2

    with tempfile.TemporaryDirectory() as tmp:
        filters = []
        for i, (line, when) in enumerate(zip(rows, times, strict=True)):
            if not line:
                continue
            path = Path(tmp) / f"row{i}.txt"
            path.write_text(line, encoding="utf-8")
            color = ACCENT if line.lstrip().startswith("$") else FG
            filters.append(
                f"drawtext=fontfile={FONT}:textfile={path}:fontsize={fontsize}"
                f":fontcolor={color}:x=24:y={20 + i * line_height}:enable='gte(t,{when:.2f})'"
            )
        # No stats_mode=diff: with a uniform first frame it yields a 1-colour palette
        # and every later frame renders blank. Default stats_mode analyses all frames.
        graph = ",".join(filters) + ",split[a][b];[a]palettegen[p];[b][p]paletteuse=dither=bayer"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-t", f"{duration:.2f}", "-i", f"color=c={BG}:s={width}x{height}:r={fps}",
            "-filter_complex", graph, out.as_posix(),
        ]
        subprocess.run(cmd, check=True)
    shown = out.relative_to(ROOT) if out.is_relative_to(ROOT) else out
    print(f"wrote {shown} — {width}x{height}, {duration:.1f}s, {out.stat().st_size // 1024} KiB")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cast", type=Path, default=ROOT / "docs" / "demo.cast")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "demo.gif")
    parser.add_argument("--max-gap", type=float, default=1.0, help="cap silent gaps (seconds)")
    parser.add_argument("--fps", type=int, default=8)
    parser.add_argument("--fontsize", type=int, default=13)
    parser.add_argument("--tail", type=float, default=2.5, help="seconds to hold the last frame")
    args = parser.parse_args()

    if not args.cast.exists():
        print(f"no cast at {args.cast} — run scripts/record_demo.py first", file=sys.stderr)
        return 1
    cols, rows, events = read_cast(args.cast)
    lines, times, clock = screen(events, cols, rows, args.max_gap)
    if not lines:
        print("the cast is empty", file=sys.stderr)
        return 1
    render(lines, times, clock + args.tail, args.out, args.fps, args.fontsize)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
