#!/usr/bin/env python3
"""Camera calibration capture — press SPACE to capture 4K checkerboard frames.

Usage:
    python3 scripts/calibrate.py --cam cam0
    python3 scripts/calibrate.py --cam both

Shows smooth GTK preview. Press SPACE to capture a 4K still.
Captures saved to calibration/<cam>_NNN.jpg.  Close window when done.

Then fit K and the lens distortion (runs anywhere, no GTK or cameras):
    python -m afl_cam.lens fit calibration
which rewrites afl_cam/intrinsics/cam{0,1}.json (tracked; commit the change)
and reports the board coverage. Hold the 10x7-corner board flat (a
full-screen checkerboard on a laptop is fine) across the whole frame, the
left/right edges especially (the overlap sits there), tilted up to ~30 deg.
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import cv2

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
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Camera calibration capture")
    parser.add_argument("--cam", default="both", help="cam0, cam1, both")
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

    for c in cameras:
        dev = CAMERAS.get(c)
        if not dev or not os.path.exists(dev):
            print(f"Camera {c} ({dev}) not found")
            return 1
        print(f"\n{c}: hold checkerboard, press SPACE for each angle")
        win = CaptureWindow(dev, c)
        Gtk.main()
        print(f"  {c}: {win.count} frames saved to calibration/")

    print("\nNow run: python -m afl_cam.lens fit calibration")
    # PyGObject/GStreamer thread teardown crashes the interpreter at exit on
    # this box (gilstate_tss_set abort). All capture work is done and saved;
    # skip finalization instead of aborting.
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
