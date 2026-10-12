"""Separate-mode re-stitch from files: two per-camera recordings + .pts
sidecars in, one stitched video out, frames paired by timestamp.

Synthetic recordings: each frame is a flat grey level that encodes its
index, so the output shows which cam0/cam1 frames were paired. The real
warp is swapped for a side-by-side stub (geometry is not under test)."""

import shutil
import subprocess

import cv2
import numpy as np
import pytest

from afl_cam import stitcher
from afl_cam.pairing import pair_frames

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None,
                                reason="needs ffmpeg")
W, H = 128, 64


def level(i):
    return 16 + 4 * i


def write_recording(path, n):
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{W}x{H}", "-r", "30",
         "-i", "pipe:0", "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p",
         "-f", "h264", str(path)], stdin=subprocess.PIPE)
    for i in range(n):
        proc.stdin.write(np.full((H, W), level(i), np.uint8).tobytes())
    proc.stdin.close()
    assert proc.wait() == 0


class SideBySide:
    def __init__(self, **_):
        self.reference_pano = None
        self.output_shape = (H, 2 * W)

    def calibrate(self, f0, f1):
        return True

    def stitch(self, f0, f1):
        return np.hstack([f0, f1])


def test_restitch_pairs_frames_by_timestamp(tmp_path, monkeypatch):
    monkeypatch.setattr(stitcher, "VideoStitcher", SideBySide)
    # cam0 at 30 fps; cam1 starts 0.2 s later at 20 fps
    pts0 = np.arange(45) * 33_333 + 1_000_000
    pts1 = np.arange(25) * 50_000 + 1_200_000
    for cam, pts in (("cam0", pts0), ("cam1", pts1)):
        write_recording(tmp_path / f"{cam}.h264", len(pts))
        (tmp_path / f"{cam}.pts").write_text("\n".join(map(str, pts)) + "\n")

    out = tmp_path / "out"
    out.mkdir()
    result = stitcher.stitch_video_files(
        tmp_path / "cam0.h264", tmp_path / "cam1.h264", out,
        output_width=2 * W)

    s0, e_cut, pair = pair_frames(pts0, pts1)
    assert result["frame_count"] == e_cut - s0
    assert result["frame_lag"] == s0

    cap = cv2.VideoCapture(str(out / "stitch_video.mp4"))
    for i in range(s0, e_cut):
        ok, frame = cap.read()
        assert ok, f"output ends early at cam0 frame {i}"
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(float)
        assert abs(gray[:, :W].mean() - level(i)) < 2.5
        assert abs(gray[:, W:].mean() - level(int(pair[i]))) < 2.5

    side = (out / "stitch_video.pts").read_text().split()
    assert [int(t) for t in side] == [
        int((pts0[i] + pts1[pair[i]]) // 2) for i in range(s0, e_cut)]


def test_refuses_recordings_without_pts(tmp_path):
    write_recording(tmp_path / "cam0.h264", 5)
    write_recording(tmp_path / "cam1.h264", 5)
    with pytest.raises(stitcher.StitchError, match="pts"):
        stitcher.stitch_video_files(tmp_path / "cam0.h264",
                                    tmp_path / "cam1.h264", tmp_path)
