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
            "-vframes", "1",
            "-c:v", "copy", str(out),
        ]
        try:
            subprocess.run(cmd, check=True, timeout=5)
            size_kb = out.stat().st_size / 1024
            self._label.set_text(f"[{self._count}] captured — {size_kb:.0f} KB")
        except subprocess.CalledProcessError:
            self._count -= 1
            self._label.set_text(f"CAPTURE FAILED — try again")
        finally:
            self._start_preview()

        self._flash = 0.6
        for i in range(6):
            GLib.timeout_add(i * 50, self._fade_flash)
        return False

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
    """Load saved checkerboard frames and compute calibration."""
    w, h = [int(x) for x in pattern_str.split("x")]
    pattern = (w, h)

    files = sorted(CALIB_DIR.glob(f"{name}_*.jpg"))
    if len(files) < 3:
        print(f"Need at least 3 captures for {name} (found {len(files)})")
        return None

    objp = np.zeros((pattern[0] * pattern[1], 3), np.float32)
    objp[:, :2] = np.mgrid[0:pattern[0], 0:pattern[1]].T.reshape(-1, 2)

    obj_points, img_points = [], []
    frame_size = None

    print(f"\nProcessing {len(files)} frames for {name}...")
    for f in files:
        img = cv2.imread(str(f))
        if img is None:
            continue
        if frame_size is None:
            frame_size = (img.shape[1], img.shape[0])
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        found, corners = cv2.findChessboardCorners(gray, pattern, None)
        if not found:
            found, corners = cv2.findChessboardCorners(
                gray, pattern, cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE,
            )
        if not found:
            print(f"  {f.name}: no checkerboard found")
            continue

        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
        cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), criteria)
        obj_points.append(objp)
        img_points.append(corners)
        print(f"  {f.name}: OK")

    if len(obj_points) < 3:
        print(f"Only {len(obj_points)} valid frames — need at least 3")
        return None

    print(f"\nCalibrating from {len(obj_points)} frames...")
    ret, mtx, dist, rvecs, tvecs = cv2.calibrateCamera(
        obj_points, img_points, frame_size, None, None,
    )

    print(f"  RMS error: {ret:.4f}  (good < 0.5)")
    print(f"  fx={mtx[0,0]:.1f}  fy={mtx[1,1]:.1f}  cx={mtx[0,2]:.1f}  cy={mtx[1,2]:.1f}")
    print(f"  dist: {np.array2string(dist.ravel(), precision=4, suppress_small=True)}")

    outfile = CALIB_DIR / f"{name}_calib.npz"
    np.savez(outfile, mtx=mtx, dist=dist, rms=ret, pattern=pattern, resolution=frame_size)
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

    print("\nNow run: python3 scripts/calibrate.py --process --pattern 8x11")


if __name__ == "__main__":
    sys.exit(main())
