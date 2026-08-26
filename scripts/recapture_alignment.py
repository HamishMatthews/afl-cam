"""Re-freeze the live-stitch alignment after the cameras move.

The live GPU stitch never computes alignment at record time — it loads the
frozen files in <out_dir> (params.bin, w0/w1.bin, map0/map1.bin) written by
dump_camera_params.py. When the rig has shifted (or been re-aimed), run this
script: it captures one settled still per camera and regenerates the files.
The next recording uses the new alignment automatically.

Usage:
    python3 scripts/recapture_alignment.py [out_dir] [--canvas-width W]

    out_dir          default: calibration/stitch_live_full (native 5304-wide)
                     use calibration/stitch_live for the 3840 canvas
                     (--canvas-width 3840)
"""

import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from afl_cam.capture import camera_device  # noqa: E402

DUMP = REPO / "scripts" / "dump_camera_params.py"
# The dump script needs OpenCV 5 (cv2.Stitcher.cameras API) — the project
# venv is py3.14 with no wheels for it, so use the dedicated venv.
CALIB_PYTHON = "/home/hamish/venvs/cv2-new/bin/python3"


def main() -> int:
    argv = sys.argv[1:]
    canvas_width = None
    if "--canvas-width" in argv:
        i = argv.index("--canvas-width")
        canvas_width = int(argv[i + 1])
        del argv[i:i + 2]
    out_dir = Path(argv[0]) if argv else REPO / "calibration" / "stitch_live_full"
    if len(argv) > 1:
        raise SystemExit(__doc__.split("Usage:")[1].split("\n")[0])

    dev0 = camera_device("cam0")
    dev1 = camera_device("cam1")
    if not dev0 or not dev1:
        raise SystemExit("both cameras must be connected (and not recording)")

    with tempfile.TemporaryDirectory(prefix="aflcam-align-") as tmp:
        tmp = Path(tmp)
        stills = {}
        for label, dev in (("cam0", dev0), ("cam1", dev1)):
            out = tmp / f"{label}.jpg"
            # 4 s lets auto-exposure settle; -update 1 keeps the last frame
            result = subprocess.run(
                ["ffmpeg", "-v", "error",
                 "-f", "v4l2", "-input_format", "mjpeg",
                 "-video_size", "3840x2160", "-framerate", "30",
                 "-i", str(dev), "-t", "4", "-update", "1", "-y", str(out)],
                capture_output=True, text=True, timeout=30)
            if result.returncode != 0 or not out.exists():
                raise SystemExit(f"still capture failed for {label}: "
                                 f"{result.stderr.strip()}")
            stills[label] = out
            print(f"{label}: captured {out.stat().st_size // 1024} KB from {dev}")

        cmd = [CALIB_PYTHON, str(DUMP), str(stills["cam0"]), str(stills["cam1"]),
               str(out_dir)]
        if canvas_width:
            cmd += ["--canvas-width", str(canvas_width)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        print(result.stdout)
        if result.returncode != 0:
            raise SystemExit(f"params generation failed:\n{result.stderr[-800:]}")
    print(f"alignment frozen into {out_dir} — the next recording uses it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
