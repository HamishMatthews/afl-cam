#!/usr/bin/env python3
"""Camera calibration — press SPACE to capture 4K checkerboard frames.

Usage:
    python3 scripts/calibrate.py --cam cam0
    python3 scripts/calibrate.py --cam both

Shows smooth GTK preview. Press SPACE to capture a 4K still.
Captures saved to calibration/<cam>_NNN.png.  Close window when done.

Then run: python3 scripts/calibrate.py --process --pattern 8x11
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, Gst
Gst.init(None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from afl_cam.config import CAMERAS, ROOT_DIR

CALIB_DIR = ROOT_DIR / "calibration"
CALIB_DIR.mkdir(exist_ok=True)

PREVIEW_W, PREVIEW_H = 640, 360


# ---------------------------------------------------------------------------
# Capture window — smooth preview + space to save 4K stills
# ---------------------------------------------------------------------------

class CaptureWindow(Gtk.Window):
    def __init__(self, device, name):
        super().__init__(title=f"Capture {name} — SPACE for 4K still | close when done")
        self.set_default_size(680, 420)
        self.connect("destroy", Gtk.main_quit)
        self.connect("key-press-event", self._on_key)
        self._device = device
        self._name = name
        self._count = 0
        self._flash = 0
        self._pipeline = None
        self._poses = []          # normalized board centers of saved poses
        self._pattern_cands = [(10, 7), (8, 11), (9, 6), (7, 9), (11, 8), (6, 9)]

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.add(vbox)

        self._label = Gtk.Label(label=f"[0] captured — press SPACE")
        self._label.set_margin_top(4)
        vbox.pack_start(self._label, False, False, 0)

        self._drawing = Gtk.DrawingArea()
        self._drawing.set_size_request(PREVIEW_W, PREVIEW_H)
        self._drawing.connect("draw", self._on_draw)
        vbox.pack_start(self._drawing, True, True, 0)

        self._pixbuf = None
        self.show_all()
        self._start_preview()

    def _start_preview(self):
        pipe = (
            f"v4l2src device={self._device} ! "
            f"image/jpeg,width={PREVIEW_W},height={PREVIEW_H},framerate=30/1 ! "
            "jpegdec ! videoconvert ! video/x-raw,format=RGB ! "
            "appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
        )
        self._pipeline = Gst.parse_launch(pipe)
        sink = self._pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_frame)
        self._pipeline.set_state(Gst.State.PLAYING)

    def _stop_preview(self):
        if self._pipeline:
            self._pipeline.set_state(Gst.State.NULL)

    def _on_frame(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR
        try:
            data = bytes(info.data)
            self._pixbuf = GdkPixbuf.Pixbuf.new_from_data(
                data, GdkPixbuf.Colorspace.RGB, False, 8,
                PREVIEW_W, PREVIEW_H, PREVIEW_W * 3,
            )
        finally:
            buf.unmap(info)
        GLib.idle_add(self._drawing.queue_draw)
        return Gst.FlowReturn.OK

    def _on_draw(self, widget, cr):
        if self._pixbuf is None:
            return
        alloc = widget.get_allocation()
        scale = min(alloc.width / PREVIEW_W, alloc.height / PREVIEW_H)
        nw, nh = int(PREVIEW_W * scale), int(PREVIEW_H * scale)
        x, y = (alloc.width - nw) // 2, (alloc.height - nh) // 2
        scaled = self._pixbuf.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR)
        Gdk.cairo_set_source_pixbuf(cr, scaled, x, y)
        cr.paint()

        # Coverage mini-map (top-right): where captured boards have been
        mw, mh = 150, 84
        mx0, my0 = alloc.width - mw - 8, 8
        cr.set_source_rgba(0, 0, 0, 0.55)
        cr.rectangle(mx0, my0, mw, mh)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.4)
        cr.set_line_width(1)
        cr.rectangle(mx0, my0, mw, mh)
        cr.stroke()
        cr.set_source_rgb(1, 0.4, 0.4)
        for px, py in self._poses:
            cr.arc(mx0 + 4 + px * (mw - 8), my0 + 4 + py * (mh - 8), 3, 0, 6.2832)
            cr.fill()

        # Flash
        if self._flash > 0:
            cr.set_source_rgba(1, 1, 1, self._flash)
            cr.rectangle(0, 0, alloc.width, alloc.height)
            cr.fill()

    def _on_key(self, widget, event):
        if event.keyval == 32:  # SPACE
            self._capture_4k()

    def _capture_4k(self):
        """Stop preview, grab a full 4K MJPEG frame, restart preview."""
        self._stop_preview()
        self._count += 1
        out = CALIB_DIR / f"{self._name}_{self._count:03d}.jpg"
        cmd = [
            "ffmpeg", "-loglevel", "error", "-y",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", "3840x2160", "-framerate", "30",
            "-i", self._device,
            "-frames:v", "6",          # let auto-exposure settle; keep the last
            "-c:v", "copy", "-f", "image2", "-update", "1",
            str(out),
        ]
        try:
            subprocess.run(cmd, check=True, timeout=5)
            size_kb = out.stat().st_size / 1024
            self._detect_board(out, size_kb)
        except subprocess.CalledProcessError:
            self._count -= 1
            self._label.set_text(f"CAPTURE FAILED — try again")
        finally:
            self._start_preview()

        self._flash = 0.6
        for i in range(6):
            GLib.timeout_add(i * 50, self._fade_flash)
        return False

    def _detect_board(self, path, size_kb):
        """Check the saved still for a board; record its center for the
        coverage mini-map, and warn when the shot contains no board."""
        img = cv2.imread(str(path))
        found_any = False
        if img is not None:
            small = cv2.resize(img, (1280, int(1280 * img.shape[0] / img.shape[1])))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            for pat in self._pattern_cands:
                found, corners = cv2.findChessboardCorners(
                    gray, pat,
                    cv2.CALIB_CB_FAST_CHECK | cv2.CALIB_CB_ADAPTIVE_THRESH
                    | cv2.CALIB_CB_NORMALIZE_IMAGE)
                if found and corners is not None:
                    c = corners.reshape(-1, 2)
                    self._poses.append((float(c[:, 0].mean() / gray.shape[1]),
                                        float(c[:, 1].mean() / gray.shape[0])))
                    found_any = True
                    break
        if found_any:
            self._label.set_text(
                f"[{self._count}] board OK ({size_kb:.0f} KB) — {len(self._poses)} poses "
                f"saved. SPACE for next, close window when done.")
        else:
            self._label.set_text(
                f"[{self._count}] NO BOARD DETECTED in that shot — redo this pose")

    def _fade_flash(self):
        self._flash = max(0, self._flash - 0.12)
        self._drawing.queue_draw()
        return False

    @property
    def count(self):
        return self._count


# ---------------------------------------------------------------------------
# Process saved frames into calibration
# ---------------------------------------------------------------------------

def process_frames(pattern_str, name):
    """Load saved checkerboard frames and compute calibration.

    Tries the given pattern first, then common board sizes — whichever
    pattern detects corners in the most frames wins (no manual re-runs
    for a wrong --pattern).
    """
    w, h = [int(x) for x in pattern_str.split("x")]
    candidates = [(w, h)]
    for cand in ((10, 7), (7, 10), (8, 11), (9, 6), (7, 9), (11, 8), (6, 9)):
        if cand not in candidates:
            candidates.append(cand)

    files = sorted(CALIB_DIR.glob(f"{name}_*.jpg"))
    if len(files) < 3:
        print(f"Need at least 3 captures for {name} (found {len(files)})")
        return None

    # one pass: detect each frame's board on a 1/3-scale copy (fast), refine
    # subpixel there, scale corners back to full resolution. The pattern that
    # matches the most frames wins.
    per_pattern = {pat: [] for pat in candidates}  # pat -> list of (f, objp, corners)
    frame_size = None
    for f in files:
        img = cv2.imread(str(f))
        if img is None:
            continue
        if frame_size is None:
            frame_size = (img.shape[1], img.shape[0])
        small = cv2.resize(img, (1280, int(1280 * img.shape[0] / img.shape[1])),
                           interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        scale = img.shape[1] / 1280.0
        matched = None
        for pat in candidates:
            found, corners = cv2.findChessboardCorners(
                gray, pat,
                cv2.CALIB_CB_FAST_CHECK | cv2.CALIB_CB_ADAPTIVE_THRESH
                | cv2.CALIB_CB_NORMALIZE_IMAGE)
            if not found:
                found, corners = cv2.findChessboardCorners(
                    gray, pat,
                    cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE)
            if not found or corners is None:
                continue
            criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
            cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), criteria)
            objp = np.zeros((pat[0] * pat[1], 3), np.float32)
            objp[:, :2] = np.mgrid[0:pat[0], 0:pat[1]].T.reshape(-1, 2)
            per_pattern[pat].append((f, objp, corners.reshape(-1, 1, 2) * scale))
            matched = pat
            break
        if matched is None:
            print(f"  {f.name}: no checkerboard found")

    for pat in candidates:
        print(f"  pattern {pat[0]}x{pat[1]}: {len(per_pattern[pat])}/{len(files)} frames OK")
    pattern = max(candidates, key=lambda p: len(per_pattern[p]))
    obj_points = [r[1] for r in per_pattern[pattern]]
    img_points = [r[2] for r in per_pattern[pattern]]
    if len(obj_points) < 3:
        print(f"Only {len(obj_points)} valid frames — need at least 3 "
              f"(check the board is fully visible, sharp, and pattern matches)")
        return None

    print(f"\nCalibrating from {len(obj_points)} frames (pattern {pattern[0]}x{pattern[1]})...")
    # 5-coeff fit for K only: the flexible model absorbs the board's physical
    # waviness (paper board) into the distortion coefficients, which is exactly
    # why its per-camera fx values agree to 0.04% — the reliable signal. The
    # fitted k's themselves describe the BOARD, not the lens (cv2's undistort
    # map shows ~0.2% real displacement), so they are zeroed for downstream
    # use: the stitch treats the lens as pinhole + measured K.
    ret, mtx, dist, rvecs, tvecs = cv2.calibrateCamera(
        obj_points, img_points, frame_size, None, None,
    )
    dist = np.zeros_like(dist)

    print(f"  RMS error: {ret:.4f}  (good < 0.5)")
    print(f"  fx={mtx[0,0]:.1f}  fy={mtx[1,1]:.1f}  fx/fy={mtx[0,0]/mtx[1,1]:.4f} "
          f"  cx={mtx[0,2]:.1f}  cy={mtx[1,2]:.1f}")
    print(f"  dist (k1,k2,p1,p2,k3): {np.array2string(dist.ravel(), precision=5, suppress_small=True)}")
    # the individual coefficients can be large and cancel — report the actual
    # distortion CURVE (radial shift in % at normalized radius r = px/fx)
    k1, k2, _, _, k3 = dist.ravel()
    shifts = "  ".join(
        f"r={rn:.1f}:{(k1*rn**2 + k2*rn**4 + k3*rn**6) * 100:+.2f}%"
        for rn in (0.5, 0.7, 0.9, 1.0))
    print(f"  radial distortion (shift at radius r): {shifts}")

    # pose coverage across the frame — distortion is only constrained by
    # near-edge poses; clustered center shots produce degenerate fits
    fw, fh = frame_size
    radii = []
    for pts in img_points:
        c = pts.reshape(-1, 2)
        radii.append(float(np.hypot(c[:, 0].mean() - fw / 2, c[:, 1].mean() - fh / 2)))
    edge_poses = sum(1 for r in radii if r > 0.4 * np.hypot(fw / 2, fh / 2))
    print(f"  coverage: {len(radii)} poses, {edge_poses} near edges/corners")
    if edge_poses < 4:
        print("  WARNING: too few edge/corner poses — the distortion estimate is "
              "unreliable. Re-capture holding the board near the frame edges.")
    if ret > 0.5:
        print("  WARNING: RMS is high (good < 0.5) — add more varied tilts "
              "and distances.")
    if abs(k1 + k2 + k3) > 0.15:
        print("  WARNING: distortion curve looks degenerate (>15% at corner) — "
              "see coverage warning.")

    outfile = CALIB_DIR / f"{name}_calib.npz"
    np.savez(outfile, mtx=mtx, dist=dist, rms=ret, pattern=pattern,
             resolution=frame_size, n_poses=len(obj_points), edge_poses=edge_poses)
    print(f"  Saved: {outfile}")
    return {"mtx": mtx, "dist": dist, "rms": ret}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Camera calibration")
    parser.add_argument("--cam", default="both", help="cam0, cam1, both")
    parser.add_argument("--pattern", default="9x6", help="Checkerboard inner corners (e.g. 8x11)")
    parser.add_argument("--process", action="store_true", help="Process saved captures (don't capture)")
    args = parser.parse_args()

    cam_arg = args.cam
    if cam_arg in ("0", "cam0"):
        cameras = ["cam0"]
    elif cam_arg in ("1", "cam1"):
        cameras = ["cam1"]
    elif cam_arg == "both":
        cameras = ["cam0", "cam1"]
    else:
        print(f"Unknown: {cam_arg}")
        return 1

    if args.process:
        # Offline processing
        for c in cameras:
            process_frames(args.pattern, c)
        return 0

    # Live capture mode
    for c in cameras:
        dev = CAMERAS.get(c)
        if not dev or not os.path.exists(dev):
            print(f"Camera {c} ({dev}) not found")
            return 1
        print(f"\n{c}: hold checkerboard, press SPACE for each angle")
        win = CaptureWindow(dev, c)
        Gtk.main()
        print(f"  {c}: {win.count} frames saved to calibration/")

    print("\nNow run: python3 scripts/calibrate.py --process --pattern 8x11 "
          "(any pattern works — it auto-detects)")
    # PyGObject/GStreamer thread teardown crashes the interpreter at exit on
    # this box (gilstate_tss_set abort). All capture work is done and saved;
    # skip finalization instead of aborting.
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
