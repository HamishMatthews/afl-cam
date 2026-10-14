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

# The fixed names cam2stitch (master + preview) and arecord write per run.
# Each wav carries a `.start` stamp (kernel monotonic us, the clock the
# `.pts` sidecars use) so finalize can line the mic up with the video's
# first frame — see daemon._start_audio.
AUDIO_MARKER = ".start"
SEGMENT_FILES = ("stitch.raw.h264", "stitch.pts", "preview.raw.h264",
                 "preview.pts", "cam0.wav", "cam0.wav" + AUDIO_MARKER,
                 "cam1.wav", "cam1.wav" + AUDIO_MARKER)


def audio_for(raw: Path, cam: str = "cam0") -> Path:
    """The mic file arecord wrote alongside this segment's raw video."""
    m = re.match(r".+\.(\d{3})\.raw\.(h264|hevc)$", raw.name)
    return raw.with_name(f"{cam}.{m.group(1)}.wav" if m else f"{cam}.wav")


def audio_marker(wav: Path) -> Path:
    """When arecord was spawned for this wav (kernel monotonic us)."""
    return wav.with_name(wav.name + AUDIO_MARKER)


def drop_audio(raw: Path, cams: tuple = ("cam0", "cam1")) -> None:
    """Delete the mic takes beside this segment's video.

    Called once the mux that consumed them succeeded (the caller decides
    that, and honours AFL_CAM_KEEP_WAV): live sessions keep only cam0's
    audio in the muxed file, so cam1's is deleted with it rather than
    left in the session folder for the rest of the season.
    """
    for cam in cams:
        wav = audio_for(raw, cam)
        wav.unlink(missing_ok=True)
        audio_marker(wav).unlink(missing_ok=True)


def save_intent(path: Path, intent: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as fh:
        json.dump(intent, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    # the rename too: without it a power cut can keep the old entry
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _read_intent(path: Path) -> Optional[dict]:
    try:
        intent = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(intent, dict) or not intent.get("dir"):
        return None
    return intent


def load_intent(path: Path) -> Optional[dict]:
    """The record intent, or the heartbeat being written when the board
    lost power (the .tmp beside it)."""
    return _read_intent(path) \
        or _read_intent(path.with_name(path.name + ".tmp"))


def intent_unreadable(path: Path) -> bool:
    """An intent file is there but holds no intent. A power cut mid-
    heartbeat leaves it like this (2026-10-14: 260 NUL bytes, the .tmp
    too): the recording never stopped, but the file can't say which."""
    return path.exists() and load_intent(path) is None


def intent_from_files(dir_: Path, session_id: int,
                      recording_id: int) -> dict:
    """Rebuild a record intent from what the session folder holds, for an
    intent the power cut wiped: the last write is the last heartbeat, and
    the .pts sidecars say how much was recorded."""
    alive_at, elapsed, segs = 0.0, 0.0, 0
    for pts in dir_.glob("stitch*.pts"):
        try:
            alive_at = max(alive_at, pts.stat().st_mtime)
            stamps = pts.read_text().split()
            elapsed += (int(stamps[-1]) - int(stamps[0])) / 1e6
        except (OSError, ValueError, IndexError):
            continue
        segs += 1
    raw = dir_ / "stitch.raw.h264"
    if raw.exists():
        alive_at = max(alive_at, raw.stat().st_mtime)
    return {"dir": str(dir_), "session_id": session_id,
            "recording_id": recording_id, "elapsed_before": elapsed,
            "seg_start": alive_at, "restarts": max(0, segs - 1),
            "gaps": [], "alive_at": alive_at}


def clear_intent(path: Path) -> None:
    # the .tmp too: load_intent falls back to it
    for p in (path, path.with_name(path.name + ".tmp")):
        p.unlink(missing_ok=True)


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
