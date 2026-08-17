"""GTK3 GUI for afl-cam — live preview, recording controls, playback browser."""

import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib

from .config import (
    CAMERAS, RESOLUTIONS, DEFAULT_RESOLUTION,
    PREVIEW_WIDTH, PREVIEW_HEIGHT, CAPTURES_DIR, DB_PATH, ROOT_DIR, PLATFORM,
)
from .capture import (camera_device, camera_present, set_auto_exposure,
                      start_audio_capture, stop_audio_procs)
from .db import RecordingDB
from .engine import make_preview, make_recorder
from .sessions import new_session_dir, session_dir_is_safe, remove_session_dir

log = logging.getLogger("afl_cam.ui")


def _lazy_gst():
    """Import GStreamer bindings on first use (Linux playback only)."""
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    return Gst


# ---------------------------------------------------------------------------
# Preview panel — one per camera
# ---------------------------------------------------------------------------

class PreviewPanel(Gtk.Frame):
    """Displays live preview for one camera."""

    def __init__(self, label: str):
        super().__init__(label=label)
        self._label = label
        self._pixbuf: GdkPixbuf.Pixbuf | None = None
        self._lock = Lock()

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_start(4)
        box.set_margin_end(4)
        box.set_margin_top(4)
        box.set_margin_bottom(4)

        self._drawing = Gtk.DrawingArea()
        self._drawing.set_size_request(PREVIEW_WIDTH, PREVIEW_HEIGHT)
        self._drawing.connect("draw", self._on_draw)
        box.pack_start(self._drawing, True, True, 0)

        self._status_label = Gtk.Label(label="offline")
        self._status_label.set_halign(Gtk.Align.CENTER)
        box.pack_start(self._status_label, False, False, 0)

        self.add(box)

    def set_label(self, label: str):
        self._label = label
        self.set_label_widget(Gtk.Label(label=label))

    def update_frame(self, data: bytes, width: int, height: int):
        """Called from engine thread — schedule GTK update via idle_add."""
        GLib.idle_add(self._update_frame_gtk, data, width, height)

    def _update_frame_gtk(self, data: bytes, width: int, height: int) -> bool:
        with self._lock:
            try:
                # Copy data: GStreamer buffer may be freed after callback
                import copy
                owned = copy.copy(data) if isinstance(data, bytes) else bytes(data)
                self._pixbuf = GdkPixbuf.Pixbuf.new_from_data(
                    owned, GdkPixbuf.Colorspace.RGB, False, 8,
                    width, height, width * 3,
                )
            except Exception as e:
                log.warning("Failed to create pixbuf: %s", e)
                return False
        self._drawing.queue_draw()
        return False  # don't re-call

    def _on_draw(self, widget, cr):
        with self._lock:
            if self._pixbuf is None:
                return
            alloc = widget.get_allocation()
            if alloc.width <= 0 or alloc.height <= 0:
                return
            # Letterbox: scale maintaining 16:9 aspect ratio
            pb_w = self._pixbuf.get_width()
            pb_h = self._pixbuf.get_height()
            scale = min(alloc.width / pb_w, alloc.height / pb_h)
            new_w = int(pb_w * scale)
            new_h = int(pb_h * scale)
            x = (alloc.width - new_w) // 2
            y = (alloc.height - new_h) // 2
            scaled = self._pixbuf.scale_simple(
                new_w, new_h, GdkPixbuf.InterpType.BILINEAR,
            )
        # Fill background black
        cr.set_source_rgb(0, 0, 0)
        cr.paint()
        # Draw letterboxed preview
        Gdk.cairo_set_source_pixbuf(cr, scaled, x, y)
        cr.paint()

    def set_status(self, text: str):
        GLib.idle_add(self._status_label.set_text, text)


# ---------------------------------------------------------------------------
# Control bar — resolution, duration, record/stop, swap
# ---------------------------------------------------------------------------

class ControlBar(Gtk.Box):
    """Recording controls."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.set_margin_start(8)
        self.set_margin_end(8)
        self.set_margin_top(6)
        self.set_margin_bottom(6)

        # Swap button
        self.swap_btn = Gtk.Button(label="Swap")
        self.swap_btn.set_tooltip_text("Rotate left/right camera previews")
        self.pack_start(self.swap_btn, False, False, 0)

        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        self.pack_start(sep, False, False, 0)

        # Resolution dropdown
        self.pack_start(Gtk.Label(label="Resolution:"), False, False, 0)
        self.res_combo = Gtk.ComboBoxText()
        for key, res in RESOLUTIONS.items():
            self.res_combo.append(key, res["label"])
        self.res_combo.set_active_id(DEFAULT_RESOLUTION)
        self.pack_start(self.res_combo, False, False, 0)

        # Duration with manual +/- buttons
        self.pack_start(Gtk.Label(label="Duration (s):"), False, False, 0)
        self.dur_entry = Gtk.Entry()
        self.dur_entry.set_width_chars(5)
        self.dur_entry.set_text("0")
        self.dur_entry.set_tooltip_text("0 = record until stopped manually")
        self.pack_start(self.dur_entry, False, False, 0)

        minus_btn = Gtk.Button(label="-")
        minus_btn.connect("clicked", self._on_dur_adjust, -1)
        self.pack_start(minus_btn, False, False, 0)

        plus_btn = Gtk.Button(label="+")
        plus_btn.connect("clicked", self._on_dur_adjust, 1)
        self.pack_start(plus_btn, False, False, 0)

        # Separator
        sep2 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        self.pack_start(sep2, False, False, 0)

        # Record / Stop button
        self.record_btn = Gtk.Button(label="Record")
        self.record_btn.get_style_context().add_class("suggested-action")
        self.pack_start(self.record_btn, False, False, 0)

        # Status
        sep3 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        self.pack_start(sep3, False, False, 0)
        self.status_label = Gtk.Label(label="Ready")
        self.pack_start(self.status_label, False, False, 0)

    def _on_dur_adjust(self, btn, delta):
        try:
            val = int(self.dur_entry.get_text() or "0")
        except ValueError:
            val = 0
        val = max(0, min(7200, val + delta * 10))  # step by 10s
        self.dur_entry.set_text(str(val))

    def get_duration(self) -> float:
        try:
            return float(self.dur_entry.get_text() or "0")
        except ValueError:
            return 0.0

    def get_resolution(self) -> str:
        return self.res_combo.get_active_id() or DEFAULT_RESOLUTION

    def set_recording(self, active: bool):
        if active:
            self.record_btn.set_label("Stop Recording")
            self.record_btn.get_style_context().remove_class("suggested-action")
            self.record_btn.get_style_context().add_class("destructive-action")
            self.res_combo.set_sensitive(False)
            self.dur_entry.set_sensitive(False)
        else:
            self.record_btn.set_label("Record")
            self.record_btn.get_style_context().remove_class("destructive-action")
            self.record_btn.get_style_context().add_class("suggested-action")
            self.res_combo.set_sensitive(True)
            self.dur_entry.set_sensitive(True)


# ---------------------------------------------------------------------------
# Recording list — tree view of past recordings
# ---------------------------------------------------------------------------

class RecordingList(Gtk.ScrolledWindow):
    """Browseable list of recording sessions (grouped cam0/cam1 + stitch status)."""

    def __init__(self):
        super().__init__()
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.set_min_content_height(150)

        # date, cam/res, resolution, dur, size, status/stitch, id
        self._store = Gtk.TreeStore(str, str, str, str, str, str, int)

        self._tree = Gtk.TreeView(model=self._store)
        columns = [
            ("Date", 0), ("Camera", 1), ("Resolution", 2),
            ("Duration", 3), ("Size", 4), ("Status / Stitch", 5),
        ]
        for title, col_idx in columns:
            renderer = Gtk.CellRendererText()
            col = Gtk.TreeViewColumn(title, renderer, text=col_idx)
            col.set_resizable(True)
            col.set_min_width(60)
            self._tree.append_column(col)

        self.add(self._tree)

    def refresh(self):
        """Reload from database: one parent row per session, camera children."""
        self._store.clear()
        try:
            db = RecordingDB(DB_PATH)
            sessions = db.list_sessions(limit=200)
            for s in sessions:
                dur = f"{s['total_duration']:.0f}s" if s.get("total_duration") else "?"
                size = db.file_size_fmt(s.get("total_bytes"))
                parent = self._store.append(None, [
                    f"#{s['id']}  {s['created_at'][:16] if s.get('created_at') else '?'}",
                    f"{s['recording_count']} cam(s)",
                    "",
                    dur,
                    size,
                    s.get("stitch_status", "none"),
                    s["id"],
                ])
                for r in db.session_recordings(s["id"]):
                    rdur = f"{r['duration_sec']:.0f}s" if r.get('duration_sec') else "?"
                    rsize = db.file_size_fmt(r.get("file_size_bytes"))
                    self._store.append(parent, [
                        "",
                        r.get("camera", "?"),
                        r.get("resolution", "?"),
                        rdur,
                        rsize,
                        r.get("status", "?"),
                        r["id"],
                    ])
            db.close()
        except Exception as e:
            log.warning("Failed to refresh recording list: %s", e)

    def get_selected_session_id(self) -> int | None:
        """Session id of the selected row (walks up from a camera child)."""
        sel = self._tree.get_selection()
        model, tree_iter = sel.get_selected()
        if tree_iter is None:
            return None
        parent = model.iter_parent(tree_iter)
        if parent is not None:
            tree_iter = parent
        return model[tree_iter][6]

    def get_selected_recording_id(self) -> int | None:
        """Recording id, only when a camera child row is selected."""
        sel = self._tree.get_selection()
        model, tree_iter = sel.get_selected()
        if tree_iter is not None and model.iter_parent(tree_iter) is not None:
            return model[tree_iter][6]
        return None

    def select_session(self, session_id: int):
        """Highlight the session row with the given id (after a refresh)."""
        model = self._tree.get_model()
        it = model.get_iter_first()
        while it is not None:
            if model[it][6] == session_id and model.iter_parent(it) is None:
                self._tree.get_selection().select_iter(it)
                self._tree.scroll_to_cell(model.get_path(it), None, False, 0, 0)
                break
            it = model.iter_next(it)


# ---------------------------------------------------------------------------
# Playback dialog — in-app video player
# ---------------------------------------------------------------------------

class PlaybackDialog(Gtk.Dialog):
    """Plays a recording using playbin + gtksink (native GTK widget, works on Wayland)."""

    def __init__(self, parent, filepath: str):
        super().__init__(
            title=f"Playing: {Path(filepath).name}",
            transient_for=parent,
            modal=True,
            destroy_with_parent=True,
        )
        self.set_default_size(960, 600)
        self._pipeline = None
        self._duration_ns = 0
        self._position_update_id = None
        self._seeking = False
        self._playing = False

        # --- Video area ---
        self._video_box = Gtk.Box()
        self._video_box.set_size_request(640, 360)
        self.vbox.pack_start(self._video_box, True, True, 0)

        # --- Controls ---
        ctrl = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        ctrl.set_margin_start(8)
        ctrl.set_margin_end(8)
        ctrl.set_margin_top(4)
        ctrl.set_margin_bottom(8)

        self._play_btn = Gtk.Button(label="Play")
        self._play_btn.connect("clicked", self._on_play_pause)
        ctrl.pack_start(self._play_btn, False, False, 0)

        self._pos_label = Gtk.Label(label="0:00")
        ctrl.pack_start(self._pos_label, False, False, 0)

        self._scrubber = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1000, 10)
        self._scrubber.set_draw_value(False)
        self._scrubber.connect("change-value", self._on_scrub_change)
        ctrl.pack_start(self._scrubber, True, True, 0)

        self._dur_label = Gtk.Label(label="0:00")
        ctrl.pack_start(self._dur_label, False, False, 0)

        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", lambda b: self._stop_and_destroy())
        ctrl.pack_start(close_btn, False, False, 0)

        self.vbox.pack_start(ctrl, False, False, 0)
        self.show_all()
        self._start_pipeline(filepath)

    # -- pipeline (playbin + gtksink, Linux) --

    def _start_pipeline(self, filepath):
        self._Gst = _lazy_gst()
        Gst = self._Gst
        self._pipeline = Gst.ElementFactory.make("playbin", "player")
        if self._pipeline is None:
            return

        self._pipeline.set_property("uri", f"file://{filepath}")

        # Use gtksink — renders natively into a GTK widget (X11 + Wayland)
        self._gtksink = Gst.ElementFactory.make("gtksink", "gtsink")
        if self._gtksink is None:
            log.error("gtksink not available")
            return

        self._pipeline.set_property("video-sink", self._gtksink)
        self._pipeline.set_property("audio-sink", Gst.ElementFactory.make("fakesink", None))

        # Embed the gtksink widget into our video_box
        widget = self._gtksink.props.widget
        self._video_box.pack_start(widget, True, True, 0)
        widget.show()

        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)
        bus.connect("message::eos", self._on_eos)
        bus.connect("message::state-changed", self._on_state_changed)

        # Start paused so user sees first frame
        self._pipeline.set_state(Gst.State.PAUSED)

    # -- bus handlers --

    def _on_state_changed(self, bus, msg):
        if msg.src != self._pipeline:
            return
        old, new, pending = msg.parse_state_changed()
        if new == self._Gst.State.PLAYING:
            self._playing = True
            GLib.idle_add(self._play_btn.set_label, "Pause")
            self._start_position_updates()
        elif new == self._Gst.State.PAUSED and old != self._Gst.State.READY:
            self._playing = False
            GLib.idle_add(self._play_btn.set_label, "Play")
            self._stop_position_updates()
        elif new == self._Gst.State.NULL:
            self._stop_position_updates()

    def _on_error(self, bus, msg):
        err, debug = msg.parse_error()
        log.error("Playback: %s", err)
        GLib.idle_add(self._stop_and_destroy)

    def _on_eos(self, bus, msg):
        # Loop back to start
        if self._pipeline:
            self._pipeline.seek_simple(self._Gst.Format.TIME,
                                       self._Gst.SeekFlags.FLUSH | self._Gst.SeekFlags.KEY_UNIT, 0)
            self._pipeline.set_state(self._Gst.State.PAUSED)

    # -- position tracking --

    def _start_position_updates(self):
        if self._position_update_id is None:
            self._position_update_id = GLib.timeout_add(250, self._update_position)

    def _stop_position_updates(self):
        if self._position_update_id is not None:
            GLib.source_remove(self._position_update_id)
            self._position_update_id = None

    def _update_position(self) -> bool:
        if not self._pipeline or self._seeking:
            return True
        ok, dur = self._pipeline.query_duration(self._Gst.Format.TIME)
        if ok and dur > 0:
            self._duration_ns = dur
            dur_sec = dur / self._Gst.SECOND
            m, s = int(dur_sec) // 60, int(dur_sec) % 60
            GLib.idle_add(self._dur_label.set_text, f"{m}:{s:02d}")
        ok, pos = self._pipeline.query_position(self._Gst.Format.TIME)
        if ok and self._duration_ns > 0:
            pos_sec = pos / self._Gst.SECOND
            m, s = int(pos_sec) // 60, int(pos_sec) % 60
            GLib.idle_add(self._pos_label.set_text, f"{m}:{s:02d}")
            frac = pos / self._duration_ns
            GLib.idle_add(self._scrubber.set_value, frac * 1000)
        return True

    # -- controls --

    def _on_play_pause(self, btn):
        if not self._pipeline:
            return
        if self._playing:
            self._pipeline.set_state(self._Gst.State.PAUSED)
        else:
            self._pipeline.set_state(self._Gst.State.PLAYING)

    def _on_scrub_change(self, scale, scroll, value):
        """Called when user drags the scrubber (change-value is more responsive)."""
        if not self._pipeline or self._duration_ns == 0:
            return
        target_ns = int(self._duration_ns * (value / 1000.0))
        self._pipeline.seek_simple(
            self._Gst.Format.TIME,
            self._Gst.SeekFlags.FLUSH | self._Gst.SeekFlags.KEY_UNIT, target_ns,
        )

    # -- cleanup --

    def _stop_pipeline(self):
        self._stop_position_updates()
        if self._pipeline:
            self._pipeline.set_state(self._Gst.State.NULL)
            self._pipeline = None

    def _stop_and_destroy(self):
        self._stop_pipeline()
        self.destroy()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(Gtk.ApplicationWindow):
    """Top-level afl-cam window."""

    def __init__(self, app):
        super().__init__(application=app, title="afl-cam")
        self.set_default_size(1280, 800)
        self.connect("destroy", self._on_destroy)

        self._db = RecordingDB(DB_PATH)
        self._previews: dict[str, CameraPreview] = {}
        self._recorders: list[CameraRecorder] = []
        self._db_ids: list[int] = []
        self._durations: dict[CameraRecorder, float] = {}
        self._recording = False
        self._stitching = False
        self._session_id: int | None = None
        self._swapped = False
        self._rec_start: float = 0.0
        self._timer_id = None

        # State: assigned camera per panel position (left=0, right=1)
        # [0] = cam label for left panel, [1] = cam label for right panel
        self._panel_cams = ["cam0", "cam1"]

        self._build_ui()
        self._start_previews()

    # -- UI construction --

    def _build_ui(self):
        # Header bar
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(False)
        hb.set_title("afl-cam")

        quit_btn = Gtk.Button(label="Quit")
        quit_btn.connect("clicked", lambda b: self.close())
        hb.pack_end(quit_btn)

        self.set_titlebar(hb)

        # Main vertical box
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.add(main)

        # --- Preview panels (side by side) ---
        preview_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        preview_box.set_margin_start(8)
        preview_box.set_margin_end(8)
        preview_box.set_margin_top(8)

        self._left_panel = PreviewPanel("Cam 0")
        self._right_panel = PreviewPanel("Cam 1")
        preview_box.pack_start(self._left_panel, True, True, 0)
        preview_box.pack_start(self._right_panel, True, True, 0)
        main.pack_start(preview_box, True, True, 0)

        # --- Controls ---
        self._controls = ControlBar()
        self._controls.swap_btn.connect("clicked", self._on_swap)
        self._controls.record_btn.connect("clicked", self._on_record_toggle)
        main.pack_start(self._controls, False, False, 0)

        # --- Recording list ---
        self._recording_list = RecordingList()
        main.pack_start(self._recording_list, True, True, 0)

        # Play / Delete / Refresh buttons under the list
        list_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        list_btns.set_margin_start(8)
        list_btns.set_margin_end(8)
        list_btns.set_margin_bottom(8)

        play_btn = Gtk.Button(label="Play Selected")
        play_btn.connect("clicked", self._on_play)
        list_btns.pack_start(play_btn, False, False, 0)

        self._stitch_btn = Gtk.Button(label="Stitch Selected")
        self._stitch_btn.connect("clicked", self._on_stitch)
        list_btns.pack_start(self._stitch_btn, False, False, 0)

        delete_btn = Gtk.Button(label="Delete Selected")
        delete_btn.connect("clicked", self._on_delete)
        list_btns.pack_start(delete_btn, False, False, 0)

        refresh_btn = Gtk.Button(label="Refresh List")
        refresh_btn.connect("clicked", lambda b: self._recording_list.refresh())
        list_btns.pack_end(refresh_btn, False, False, 0)

        main.pack_start(list_btns, False, False, 0)

        self.show_all()

        # Initial list load
        GLib.idle_add(self._recording_list.refresh)

    # -- Preview management --

    def _start_previews(self):
        """Start live previews on both cameras."""
        left_cam = self._panel_cams[0]
        right_cam = self._panel_cams[1]

        # Stop existing
        self._stop_previews()

        # Left panel
        if camera_present(left_cam):
            p = make_preview(camera_device(left_cam), label=left_cam)
            p.start(on_frame=lambda d, w, h, panel=self._left_panel: panel.update_frame(d, w, h))
            self._previews["left"] = p
            self._left_panel.set_label(f"Cam: {left_cam} ({camera_device(left_cam)})")
            self._left_panel.set_status("live")
        else:
            self._left_panel.set_status(f"offline ({left_cam})")

        # Right panel
        if camera_present(right_cam):
            p = make_preview(camera_device(right_cam), label=right_cam)
            p.start(on_frame=lambda d, w, h, panel=self._right_panel: panel.update_frame(d, w, h))
            self._previews["right"] = p
            self._right_panel.set_label(f"Cam: {right_cam} ({camera_device(right_cam)})")
            self._right_panel.set_status("live")
        else:
            self._right_panel.set_status(f"offline ({right_cam})")

        # Auto exposure AFTER the streams open: mode 3 = Aperture Priority,
        # mode 1 = Manual (almost-black frames), and stream open can reset it.
        for cam_key in (left_cam, right_cam):
            set_auto_exposure(cam_key)

    def _stop_previews(self):
        for p in self._previews.values():
            p.stop()
        self._previews.clear()

    def _on_swap(self, _btn):
        """Swap which camera is shown on left vs right."""
        self._swapped = not self._swapped
        self._panel_cams[0], self._panel_cams[1] = self._panel_cams[1], self._panel_cams[0]
        self._stop_previews()
        self._start_previews()
        log.info("Cameras swapped: left=%s right=%s", self._panel_cams[0], self._panel_cams[1])

    # -- Recording --

    def _on_record_toggle(self, _btn):
        if self._recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self):
        if self._recording:
            return

        res_key = self._controls.get_resolution()
        duration = self._controls.get_duration()

        # Stop previews (cameras can only have one stream open at a time)
        self._stop_previews()
        for panel in (self._left_panel, self._right_panel):
            panel.set_status("recording...")

        # Create the session (folder + DB row) and record into it
        session_dir = new_session_dir()
        session_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._session_id = self._db.add_session(
            created_at=session_time,
            folder=str(session_dir.relative_to(ROOT_DIR)),
        )

        self._recorders.clear()
        self._db_ids.clear()
        self._durations.clear()

        for cam_key in ["cam0", "cam1"]:
            dev = camera_device(cam_key)
            if not camera_present(cam_key):
                continue

            recorder = make_recorder(dev, label=cam_key, resolution_key=res_key)
            output = recorder.start(
                duration_sec=duration,
                on_done=self._on_recorder_done,
                output_dir=session_dir,
            )
            row_id = self._db.add(
                camera=cam_key, device_path=dev,
                resolution=RESOLUTIONS[res_key]["label"],
                file_path=str(output.relative_to(ROOT_DIR)),
                status="recording",
                session_id=self._session_id,
                created_at=session_time,
            )
            self._db_ids.append(row_id)
            self._recorders.append(recorder)

        if not self._recorders:
            log.error("No cameras available to record")
            self._resume_previews()
            return

        # Record the camera microphones for clap-based timecode sync
        self._audio_procs = []
        for cam_key in ("cam0", "cam1"):
            proc = start_audio_capture(cam_key, session_dir / f"{cam_key}.wav")
            if proc:
                self._audio_procs.append(proc)

        # Set auto exposure AFTER the streams open — opening a 4K stream can
        # reset controls, and mode 1 (Manual) records almost-black frames.
        for cam_key in ("cam0", "cam1"):
            set_auto_exposure(cam_key)

        self._recording = True
        self._controls.set_recording(True)
        self._controls.swap_btn.set_sensitive(False)
        self._stitch_btn.set_sensitive(False)
        self._controls.status_label.set_text("Recording... 0:00")
        self._rec_start = time.time()
        self._timer_id = GLib.timeout_add(1000, self._update_recording_timer)

    def _update_recording_timer(self) -> bool:
        if not self._recording:
            return False  # stop the timer
        elapsed = int(time.time() - self._rec_start)
        minutes = elapsed // 60
        seconds = elapsed % 60
        self._controls.status_label.set_text(f"Recording... {minutes}:{seconds:02d}")
        return True  # keep calling

    def _stop_recording(self):
        if not self._recording:
            return
        for r in self._recorders:
            if r.running:
                r.stop()

    def _on_recorder_done(self, ok: bool, path: Path, duration: float):
        """Called when a single recorder finishes (from engine thread)."""
        if ok:
            for recorder in self._recorders:
                if recorder.output_path == path:
                    self._durations[recorder] = duration
                    break
        GLib.idle_add(self._on_recorder_done_gtk)

    def _on_recorder_done_gtk(self) -> bool:
        # Check if all recorders finished
        if any(r.running for r in self._recorders):
            return False  # still waiting for others

        # All done — finalize (per-recorder durations, not the last callback's)
        for i, recorder in enumerate(self._recorders):
            output = recorder.output_path
            if output and output.exists():
                file_size = output.stat().st_size
                duration = self._durations.get(recorder, 0.0)
                if i < len(self._db_ids):
                    self._db.update_complete(self._db_ids[i], duration, file_size)
                    log.info("Saved: %s (%s)", output.name, self._db.file_size_fmt(file_size))

        self._recording = False
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = None
        self._stop_audio()
        self._controls.set_recording(False)
        self._controls.swap_btn.set_sensitive(True)
        self._stitch_btn.set_sensitive(not self._stitching)
        self._controls.status_label.set_text("Ready")
        self._recording_list.refresh()
        self._resume_previews()
        return False

    def _stop_audio(self):
        """Terminate the microphone recording processes."""
        stop_audio_procs(getattr(self, "_audio_procs", []))
        self._audio_procs = []

    def _resume_previews(self):
        """Restart previews after recording finishes."""
        GLib.timeout_add(500, self._start_previews)

    # -- Playback --

    def _on_play(self, _btn):
        rec_id = self._recording_list.get_selected_recording_id()
        if rec_id is None:
            # Session row selected — play the first complete recording (cam0 preferred)
            session_id = self._recording_list.get_selected_session_id()
            if session_id is None:
                return
            for rec in self._db.session_recordings(session_id):
                if rec["status"] == "complete" and rec["file_path"]:
                    rec_id = rec["id"]
                    break
            if rec_id is None:
                return
        rec = self._db.get(rec_id)
        if not rec:
            return
        filepath = Path(rec["file_path"])
        if not filepath.is_absolute():
            filepath = ROOT_DIR / filepath
        if filepath.exists():
            if PLATFORM == "darwin":
                # No gtksink on macOS — hand off to QuickTime Player
                subprocess.Popen(["open", str(filepath)])
            else:
                PlaybackDialog(self, str(filepath))

    def _on_delete(self, _btn):
        session_id = self._recording_list.get_selected_session_id()
        if session_id is None:
            return

        session = self._db.get_session(session_id)
        if not session:
            return

        # Collect what will be removed
        paths: list[Path] = []
        if session.get("folder"):
            folder = Path(session["folder"])
            if not folder.is_absolute():
                folder = ROOT_DIR / folder
            if folder.exists():
                paths = sorted(p for p in folder.iterdir() if p.is_file())
        else:
            for rec in self._db.session_recordings(session_id):
                if rec["file_path"]:
                    p = Path(rec["file_path"])
                    if not p.is_absolute():
                        p = ROOT_DIR / p
                    if p.exists():
                        paths.append(p)

        listing = "\n".join(f"  {p}" for p in paths) if paths else "  (no files on disk)"
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=f"Delete session #{session_id}?\nWill remove:\n{listing}",
        )
        if dialog.run() != Gtk.ResponseType.OK:
            dialog.destroy()
            return
        dialog.destroy()

        # Remove files, then DB rows (one transaction)
        if session.get("folder"):
            folder = Path(session["folder"])
            if not folder.is_absolute():
                folder = ROOT_DIR / folder
            if folder.exists() and session_dir_is_safe(folder):
                remove_session_dir(folder)
        else:
            for p in paths:
                p.unlink(missing_ok=True)
        self._db.delete_session(session_id)
        self._recording_list.refresh()

    # -- Stitching --

    def _on_stitch(self, _btn):
        if self._recording or self._stitching:
            return
        session_id = self._recording_list.get_selected_session_id()
        if session_id is None:
            return

        # Verify the session has both recordings before starting
        recs = {r["camera"]: r for r in self._db.session_recordings(session_id)}
        if "cam0" not in recs or "cam1" not in recs:
            dialog = Gtk.MessageDialog(
                transient_for=self, modal=True,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text=f"Session #{session_id} is missing a camera recording.\n"
                     "Record with both cameras before stitching.",
            )
            dialog.run()
            dialog.destroy()
            return

        self._db.update_session_stitch_status(session_id, "stitching")
        self._recording_list.refresh()
        self._stitching = True
        self._set_buttons_busy(True)
        self._controls.status_label.set_text("Stitching: starting…")
        Thread(target=self._stitch_worker, args=(session_id,), daemon=True).start()

    def _stitch_worker(self, session_id):
        """Runs off the GTK main thread. Owns its own DB connection."""
        from .stitcher import stitch_video_files

        db = RecordingDB(DB_PATH)
        try:
            session = db.get_session(session_id)
            cam0_path = cam1_path = None
            for rec in db.session_recordings(session_id):
                p = Path(rec["file_path"])
                if not p.is_absolute():
                    p = ROOT_DIR / p
                if rec["camera"] == "cam0":
                    cam0_path = p
                elif rec["camera"] == "cam1":
                    cam1_path = p

            folder = Path(session["folder"]) if session.get("folder") else None
            if folder and not folder.is_absolute():
                folder = ROOT_DIR / folder
            if folder is None or not folder.exists():
                # Legacy session without a folder — create one from its timestamp
                from .sessions import session_dir_from_created_at
                folder = session_dir_from_created_at(session["created_at"])
                folder.mkdir(parents=True, exist_ok=True)
                db.set_session_folder(session_id, str(folder.relative_to(ROOT_DIR)))

            def progress(msg):
                GLib.idle_add(
                    self._controls.status_label.set_text, f"Stitching: {msg}")

            result = stitch_video_files(
                cam0_path, cam1_path, folder,
                progress=progress,
            )
            db.update_session_stitch_status(session_id, "stitched")
            message = (f"Stitched {result['frame_count']} frames "
                       f"({result['elapsed']:.0f}s) → {result['video_path']}")
            if result.get("frame_lag"):
                message += f"  sync {result['frame_lag']:+d} frames"
            ok = True
        except Exception as e:
            db.update_session_stitch_status(session_id, "failed", str(e))
            message = str(e)
            ok = False
        finally:
            db.close()
        GLib.idle_add(self._stitch_finished_gtk, session_id, ok, message)

    def _stitch_finished_gtk(self, session_id, ok, message) -> bool:
        self._stitching = False
        self._set_buttons_busy(False)
        self._recording_list.refresh()
        if ok:
            self._controls.status_label.set_text(f"✓ {message}")
            self._recording_list.select_session(session_id)
            log.info("Stitch complete: session %d - %s", session_id, message)
        else:
            self._controls.status_label.set_text("Stitch failed")
            log.error("Stitch failed: session %d - %s", session_id, message)
            dialog = Gtk.MessageDialog(
                transient_for=self, modal=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=f"Stitch failed:\n{message}",
            )
            dialog.run()
            dialog.destroy()
        return False

    def _set_buttons_busy(self, busy: bool):
        """Disable interactive buttons while a stitch is running."""
        self._stitch_btn.set_sensitive(not busy)
        self._controls.record_btn.set_sensitive(not busy)
        self._controls.swap_btn.set_sensitive(not busy)

    # -- Shutdown --

    def _on_destroy(self, _widget):
        self._stop_previews()
        if self._recording:
            for r in self._recorders:
                if r.running:
                    r.stop()
            time.sleep(0.5)
        self._stop_audio()
        self._db.close()


# ---------------------------------------------------------------------------
# Application entry point
# ---------------------------------------------------------------------------

class AflCamApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.aflcam.recorder")
        self.connect("activate", self._on_activate)

    def _on_activate(self, _app):
        MainWindow(self)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    app = AflCamApp()
    app.run(None)


if __name__ == "__main__":
    main()
