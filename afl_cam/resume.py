"""Recording that survives a crash: the persisted record intent + segments.

While a recording is live the daemon keeps `outputs/_live/intent.json`
(atomic tmp+rename). Only an explicit stop, the schedule or an auto-stop
clears it. If cam2stitch dies, the daemon restarts or the board reboots
with the intent still there, the daemon resumes into the SAME session:
the files the dead pipeline wrote are sealed as a numbered segment
(`stitch.raw.h264` -> `stitch.000.raw.h264`, ...) and cam2stitch is
started again on the same folder. Finalize muxes each segment and
concatenates them; the gaps are logged and written to `segments.json`.

Stdlib only (runs inside afl-camd).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("afl_cam.resume")

# the fixed names cam2stitch (master + preview) and arecord write per run
SEGMENT_FILES = ("stitch.raw.h264", "stitch.pts", "preview.raw.h264",
                 "preview.pts", "cam0.wav", "cam1.wav")


def save_intent(path: Path, intent: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as fh:
        json.dump(intent, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def load_intent(path: Path) -> Optional[dict]:
    try:
        intent = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(intent, dict) or not intent.get("dir"):
        return None
    return intent


def clear_intent(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _numbered(name: str, idx: int) -> str:
    stem, ext = name.split(".", 1)
    return f"{stem}.{idx:03d}.{ext}"


def _segment_index(dir_: Path) -> int:
    """Next free segment number in a session folder."""
    used = [int(m.group(1)) for p in dir_.iterdir()
            if (m := re.match(r"stitch\.(\d{3})\.(raw\.h264|pts)$", p.name))]
    return max(used) + 1 if used else 0


def seal_segment(dir_: Path) -> Optional[int]:
    """Rename the dead run's fixed-name files to the next numbered segment.
    Returns its number, or None when the run wrote no video."""
    if not (dir_ / "stitch.raw.h264").exists():
        return None
    idx = _segment_index(dir_)
    for name in SEGMENT_FILES:
        src = dir_ / name
        if src.exists():
            os.replace(src, dir_ / _numbered(name, idx))
    return idx


def segments(dir_: Path, raw_name: str) -> list[tuple[Path, Path]]:
    """(raw, pts) pairs in recording order: the numbered segments, then the
    last run's unnumbered files. Empty segments are skipped."""
    stem = raw_name.split(".", 1)[0]
    out = []
    numbered = sorted(dir_.glob(f"{stem}.[0-9][0-9][0-9].raw.h264"))
    for raw in numbered + [dir_ / raw_name]:
        if raw.exists() and raw.stat().st_size > 0:
            pts = raw.with_name(raw.name.replace(".raw.h264", ".pts"))
            out.append((raw, pts))
    return out


def concat(parts: list[Path], out: Path, extra: tuple = ()) -> tuple[bool, str]:
    """Join same-encoder mp4 parts losslessly (ffmpeg concat demuxer)."""
    lst = out.with_name(out.name + ".concat.txt")
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "concat", "-safe", "0", "-i", str(lst),
             "-c", "copy", *extra, str(out)],
            capture_output=True, text=True, timeout=600)
        ok, err = result.returncode == 0, result.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        ok, err = False, str(e)
    finally:
        lst.unlink(missing_ok=True)
    return ok and out.exists() and out.stat().st_size > 0, err


def mux_segments(pairs: list[tuple[Path, Path]], out: Path, mux,
                 extra: tuple = ()) -> tuple[bool, bool]:
    """Mux each (raw, pts) segment with `mux(raw, pts, out)`, then join
    them into `out` (one segment muxes straight to it). Returns
    (out written, every segment made it) — keep the raw files unless both."""
    if len(pairs) == 1:
        ok = mux(pairs[0][0], pairs[0][1], out)
        return ok, ok
    parts = []
    for i, (raw, pts) in enumerate(pairs):
        part = out.with_name(f"{out.stem}.part{i:03d}{out.suffix}")
        if mux(raw, pts, part):
            parts.append(part)
    ok = False
    if parts:
        ok, err = concat(parts, out, extra)
        if not ok:
            log.error("concat into %s failed: %s", out.name, err)
    for part in parts:
        part.unlink(missing_ok=True)
    return ok, ok and len(parts) == len(pairs)


def write_report(dir_: Path, restarts: int, gaps: list) -> None:
    """segments.json: how many times the recording resumed and the gap
    (seconds of game lost) each time."""
    (dir_ / "segments.json").write_text(json.dumps({
        "restarts": restarts,
        "gaps": gaps,
        "lost_s": round(sum(g["gap_s"] for g in gaps), 1),
        "written_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, indent=1))
