"""Re-stitch a separate-mode session from its per-camera files.

    python -m afl_cam.restitch <session_id>

The daemon runs this as a subprocess (it needs OpenCV; the daemon stays
stdlib) and forwards each progress line to the phone. Writes
<session>/stitch_video.mp4 with the live map set (stitcher.py) and keeps
the session's stitch_status in the catalog current.
"""

import logging
import sys
from pathlib import Path

from .config import DB_PATH, ROOT_DIR
from .db import RecordingDB


def session_inputs(folder: Path):
    """(cam0, cam1) recordings of a separate-mode session, or None each."""
    def first(label):
        for ext in ("mp4", "mkv", "avi"):
            path = folder / f"{label}.{ext}"
            if path.is_file() and path.stat().st_size > 0:
                return path
        return None
    return first("cam0"), first("cam1")


def main(argv=None) -> int:
    logging.basicConfig(level=logging.WARNING)
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or not argv[0].isdigit():
        print("usage: python -m afl_cam.restitch <session_id>", file=sys.stderr)
        return 2
    session_id = int(argv[0])
    db = RecordingDB(DB_PATH)
    try:
        session = db.get_session(session_id)
        if not session or not session.get("folder"):
            print(f"no session {session_id}")
            return 1
        folder = Path(session["folder"])
        if not folder.is_absolute():
            folder = ROOT_DIR / folder
        cam0, cam1 = session_inputs(folder)
        if cam0 is None or cam1 is None:
            missing = "cam0" if cam0 is None else "cam1"
            db.update_session_stitch_status(session_id, "failed",
                                            f"missing {missing} recording")
            print(f"missing {missing} recording — record in separate mode first")
            return 1
        db.update_session_stitch_status(session_id, "stitching")
        from .stitcher import StitchError, stitch_video_files
        try:
            result = stitch_video_files(cam0, cam1, folder,
                                        progress=lambda m: print(m, flush=True))
        except (StitchError, RuntimeError, OSError) as e:
            db.update_session_stitch_status(session_id, "failed", str(e))
            print(f"failed: {e}")
            return 1
        db.update_session_stitch_status(session_id, "stitched")
        print(f"stitched {result['frame_count']} frames → "
              f"{Path(result['video_path']).name}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
