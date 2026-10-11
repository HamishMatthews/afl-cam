"""GTK3 GUI for afl-cam — live preview, recording controls, playback browser."""

import logging
import os
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from queue import Queue
from threading import Lock, Thread

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib

from .config import (
    CAMERAS, RESOLUTIONS, DEFAULT_RESOLUTION, LIVE_STITCH,
    PREVIEW_WIDTH, PREVIEW_HEIGHT, CAPTURES_DIR, DB_PATH, ROOT_DIR, PLATFORM,
)
from .capture import (camera_device, camera_present, ensure_cameras,
                      set_auto_exposure, start_audio_capture,
                      stop_audio_procs)
from .db import RecordingDB
from .engine import can_live_stitch, make_preview, make_recorder
from .remote import RemoteServer
from .sessions import new_session_dir, session_dir_is_safe, remove_session_dir

log = logging.getLogger("afl_cam.ui")




# Preview panel — one per camera

class PreviewPanel(Gtk.Frame):
    """Displays live preview for one camera."""

    def __init__(self, label: str):
        super().__init__(label=label)
        self._label = label
        self._pixbuf: GdkPixbuf.Pixbuf | None = None
        self._lock = Lock()
        self._got_frame = False

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_start(4)
        box.set_margin_end(4)
        box.set_margin_top(4)
        box.set_margin_bottom(4)

        self._drawing = Gtk.DrawingArea()
        # small minimum: the draw handler letterboxes to any allocation, so
        # the window can shrink for small screens (5" panels)
        self._drawing.set_size_request(120, 68)
        self._drawing.connect("draw", self._on_draw)
        # Keep the drawing 16:9 with its WIDTH: the letterboxing draw
        # handler means a taller window otherwise just adds empty bands
        # inside the frames. Height follows the allocated width only, so
        # vertical window growth never stretches the previews.
        self._drawing.connect("size-allocate", self._on_alloc)
        box.pack_start(self._drawing, True, True, 0)

        # Prominent camera identity header (e.g. "cam0 — /dev/video0")
        self._title_label = Gtk.Label()
        self._title_label.set_markup(f"<b>{GLib.markup_escape_text(label)}</b>")
        self._title_label.set_halign(Gtk.Align.CENTER)
        box.pack_start(self._title_label, False, False, 0)

        self._status_label = Gtk.Label(label="offline")
        self._status_label.set_halign(Gtk.Align.CENTER)
        box.pack_start(self._status_label, False, False, 0)

        self.add(box)

    def _on_alloc(self, drawing, alloc):
        h = int(alloc.width * 9 / 16 + 0.5)
        if h != alloc.height:
            # -1 keeps the width minimum at 120 so the window can always
            # shrink back after being widened
            drawing.set_size_request(-1, h)

    def set_label(self, label: str):
        self._label = label
        self.set_label_widget(Gtk.Label(label=label))
        self._title_label.set_markup(f"<b>{GLib.markup_escape_text(label)}</b>")

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
                self._got_frame = True
            except Exception as e:
                log.warning("Failed to create pixbuf: %s", e)
                return False
        self._drawing.queue_draw()
        return False  # don't re-call

    def set_jpeg_file(self, path: str):
        """Load a JPEG snapshot from disk (must run on the GTK thread).

        Recording-time live preview: while the recorders own the camera
        streams they write raw-JPEG snapshots to <session>/camN_preview.jpg;
        the UI polls those files and shows them here.
        """
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
        except Exception as e:
            log.warning("Failed to load preview snapshot %s: %s", path, e)
            return
        with self._lock:
            self._pixbuf = pixbuf
            self._got_frame = True
        self._drawing.queue_draw()

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

    def reset_frame_flag(self):
        with self._lock:
            self._got_frame = False

    @property
    def got_frame(self) -> bool:
        with self._lock:
            return self._got_frame


# Control bar — resolution, duration, record/stop

class ControlBar(Gtk.Box):
    """Recording controls — two rows so the bar fits narrow screens."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.set_margin_start(8)
        self.set_margin_end(8)
        self.set_margin_top(4)
        self.set_margin_bottom(4)

        row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.pack_start(row1, False, False, 0)
        self.pack_start(row2, False, False, 0)

        # Row 1: Resolution | Mode | Duration +/-
        row1.pack_start(Gtk.Label(label="Res:"), False, False, 0)
        self.res_combo = Gtk.ComboBoxText()
        for key in RESOLUTIONS:
            self.res_combo.append(key, key)
        self.res_combo.set_active_id(DEFAULT_RESOLUTION)
        self.res_combo.set_tooltip_text(
            "\n".join(f"{k} = {RESOLUTIONS[k]['label']}" for k in RESOLUTIONS))
        row1.pack_start(self.res_combo, False, False, 0)

        # Record mode: live GPU stitch, or separate cameras + stitch later
        row1.pack_start(Gtk.Label(label="Mode:"), False, False, 0)
        self.mode_combo = Gtk.ComboBoxText()
        self.mode_combo.append("live", "Live")
        self.mode_combo.append("separate", "Separate")
        self.mode_combo.set_active_id("live")
        self.mode_combo.set_tooltip_text(
            "Live stitch: one panorama recorded directly on the GPU.\n"
            "Separate + stitch: record both cameras, stitch afterwards.")
        row1.pack_start(self.mode_combo, False, False, 0)

        row1.pack_start(Gtk.Label(label="Dur:"), False, False, 0)
        self.dur_entry = Gtk.Entry()
        self.dur_entry.set_width_chars(4)
        self.dur_entry.set_text("0")
        self.dur_entry.set_tooltip_text("Seconds; 0 = record until stopped manually")
        row1.pack_start(self.dur_entry, False, False, 0)

        minus_btn = Gtk.Button(label="-")
        minus_btn.connect("clicked", self._on_dur_adjust, -1)
        row1.pack_start(minus_btn, False, False, 0)

        plus_btn = Gtk.Button(label="+")
        plus_btn.connect("clicked", self._on_dur_adjust, 1)
        row1.pack_start(plus_btn, False, False, 0)

        # Row 2: Record | status
        self.record_btn = Gtk.Button(label="Record")
        self.record_btn.get_style_context().add_class("suggested-action")
        row2.pack_start(self.record_btn, False, False, 0)

        self.status_label = Gtk.Label(label="Ready")
        self.status_label.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        row2.pack_start(self.status_label, False, False, 0)

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

    def get_record_mode(self) -> str:
        """'live' (GPU stitch during recording) or 'separate' (record both
        cameras, stitch afterwards with the Stitch button)."""
        return self.mode_combo.get_active_id() or "live"

    def set_resolution(self, key: str):
        if key in RESOLUTIONS:
            self.res_combo.set_active_id(key)

    def set_record_mode(self, key: str):
        if key in ("live", "separate"):
            self.mode_combo.set_active_id(key)

    def set_duration(self, secs: float):
        try:
            val = int(secs)
        except (TypeError, ValueError):
            val = 0
        self.dur_entry.set_text(str(max(0, min(7200, val))))

    def set_recording(self, active: bool):
        if active:
            self.record_btn.set_label("Stop Recording")
            self.record_btn.get_style_context().remove_class("suggested-action")
            self.record_btn.get_style_context().add_class("destructive-action")
            self.res_combo.set_sensitive(False)
            self.mode_combo.set_sensitive(False)
            self.dur_entry.set_sensitive(False)
        else:
            self.record_btn.set_label("Record")
            self.record_btn.get_style_context().remove_class("destructive-action")
            self.record_btn.get_style_context().add_class("suggested-action")
            self.res_combo.set_sensitive(True)
            self.mode_combo.set_sensitive(True)
            self.dur_entry.set_sensitive(True)


# Recording list — tree view of past recordings

class RecordingList(Gtk.ScrolledWindow):
    """Browseable list of recording sessions (grouped cam0/cam1 + stitch status)."""

    def __init__(self):
        super().__init__()
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.set_min_content_height(70)

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

        # Fill the whole scrolled area: without vexpand the tree keeps its
        # natural row height and the extra window space shows as dead
        # padding between the rows and the button row below.
        self._tree.set_vexpand(True)
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



# Main window

class MainWindow(Gtk.ApplicationWindow):
    """Top-level afl-cam window."""

    def __init__(self, app):
        super().__init__(application=app, title="afl-cam")
        self.set_default_size(960, 600)
        # allow shrinking onto small screens (5" LCD at display scaling)
        self.set_size_request(420, 240)
        self.connect("destroy", self._on_destroy)

        self._db = RecordingDB(DB_PATH)
        self._previews: dict[str, CameraPreview] = {}
        self._recorders: list[CameraRecorder] = []
        self._db_ids: list[int] = []
        self._durations: dict[CameraRecorder, float] = {}
        self._recording = False
        self._stitching = False
        self._realigning = False
        self._session_id: int | None = None
        self._rec_start: float = 0.0
        self._timer_id = None
        self._preview_timer = None
        self._session_dir: Path | None = None

        # Camera bring-up: kick wedged devices and re-discover if any
        # camera is missing (see capture.py). Left/right is hardcoded to
        # the physically labeled sockets — no swap feature.
        log.info("cameras: %s", ensure_cameras())

        # State: assigned camera per panel position (left=0, right=1)
        # [0] = cam label for left panel, [1] = cam label for right panel
        self._panel_cams = ["cam0", "cam1"]
        self._health_last_kick = 0.0

        self._build_ui()
        self._start_previews()
        GLib.timeout_add(5000, self._health_check)

        # Field remote (iPhone over the board AP): HTTP control + preview
        # snapshots. The server calls the remote_* methods below from its
        # worker threads; actions cross to the GTK thread via a queue.
        self._remote_actions: Queue = Queue()
        self._remote = RemoteServer(self, self._remote_actions)
        try:
            self._remote.start()
        except OSError as e:
            log.warning("field remote failed to start: %s", e)
        GLib.timeout_add(100, self._drain_remote_actions)

    # -- field-remote controller interface (called from server threads) -----

    def remote_status(self) -> dict:
        """Status blob for the field-remote page (JSON-able, thread-safe)."""
        import shutil
        du = shutil.disk_usage(ROOT_DIR)
        session = self._session_dir.name if self._session_dir is not None else None
        return {
            "recording": self._recording,
            "elapsed": time.time() - self._rec_start if self._recording else 0.0,
            "realigning": self._realigning,
            "mode": self._controls.get_record_mode(),
            "resolution": self._controls.get_resolution(),
            "duration": self._controls.get_duration(),
            "session": session,
            "disk_free_gb": round(du.free / 1e9, 1),
        }

    def remote_preview_dir(self):
        """Session dir holding the recorder snapshots, or None when idle."""
        return self._session_dir if self._recording else None

    def remote_action(self, action: str, params: dict) -> bool:
        """Validate + enqueue a field-remote action for the GTK thread."""
        if action not in ("toggle", "set_mode", "set_resolution",
                          "set_duration", "realign"):
            return False
        self._remote_actions.put((action, params))
        return True

    def _drain_remote_actions(self) -> bool:
        while not self._remote_actions.empty():
            action, params = self._remote_actions.get_nowait()
            try:
                self._apply_remote_action(action, params)
            except Exception as e:
                log.warning("remote action %s failed: %s", action, e)
        return True

    def _apply_remote_action(self, action: str, params: dict) -> None:
        """Run a field-remote action on the GTK thread (same paths as the
        buttons, so recorder/DB behavior is identical)."""
        if action == "toggle":
            self._on_record_toggle(None)
        elif action == "realign":
            if not self._recording and not self._realigning:
                self._on_realign(None)
        elif action == "set_mode":
            if not self._recording:
                self._controls.set_record_mode(str(params.get("value", "")))
        elif action == "set_resolution":
            if not self._recording:
                self._controls.set_resolution(str(params.get("value", "")))
        elif action == "set_duration":
            if not self._recording:
                self._controls.set_duration(float(params.get("value", 0) or 0))



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

# Preview panels (side by side) 
        preview_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        preview_box.set_margin_start(8)
        preview_box.set_margin_end(8)
        preview_box.set_margin_top(8)

        self._left_panel = PreviewPanel("Cam 0")
        self._right_panel = PreviewPanel("Cam 1")
        preview_box.pack_start(self._left_panel, True, True, 0)
        preview_box.pack_start(self._right_panel, True, True, 0)
        # NOT vertically expanding: the previews keep their 16:9 size and
        # extra window height goes to the recordings list instead
        main.pack_start(preview_box, False, False, 0)

# Controls 
        self._controls = ControlBar()
        self._controls.record_btn.connect("clicked", self._on_record_toggle)
        main.pack_start(self._controls, False, False, 0)

# Recording list (collapsible — small screens can fold it away). This is
        # the vertically expanding element: extra window height shows more
        # list rows, never taller previews.
        self._recording_list = RecordingList()
        rec_expander = Gtk.Expander(label="Recordings")
        rec_expander.set_expanded(True)
        rec_v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        rec_v.pack_start(self._recording_list, True, True, 0)
        rec_expander.add(rec_v)
        main.pack_start(rec_expander, True, True, 0)

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

        self._realign_btn = Gtk.Button(label="Re-align Cameras")
        self._realign_btn.set_tooltip_text(
            "Re-freeze the live-stitch alignment: captures one still per "
            "camera and regenerates the stitch params (run after the rig "
            "has been moved or re-aimed; takes ~90s)")
        self._realign_btn.connect("clicked", self._on_realign)
        list_btns.pack_start(self._realign_btn, False, False, 0)

        open_folder_btn = Gtk.Button(label="Open Folder")
        open_folder_btn.set_tooltip_text(
            "Open the selected session's folder in the file manager")
        open_folder_btn.connect("clicked", self._on_open_folder)
        list_btns.pack_start(open_folder_btn, False, False, 0)

        delete_btn = Gtk.Button(label="Delete Selected")
        delete_btn.connect("clicked", self._on_delete)
        list_btns.pack_start(delete_btn, False, False, 0)

        refresh_btn = Gtk.Button(label="Refresh List")
        refresh_btn.connect("clicked", lambda b: self._recording_list.refresh())
        list_btns.pack_end(refresh_btn, False, False, 0)

        rec_v.pack_start(list_btns, False, False, 0)

        self.show_all()

        # Initial list load
        GLib.idle_add(self._recording_list.refresh)

        # afl-camd conflict warning (after the window is visible)
        GLib.idle_add(self._check_daemon_conflict)



    def _start_previews(self):
        """Start live previews on both cameras."""
        left_cam = self._panel_cams[0]
        right_cam = self._panel_cams[1]

        # Stop existing
        self._stop_previews()

        # Left panel
        self._left_panel.reset_frame_flag()
        if camera_present(left_cam):
            p = make_preview(camera_device(left_cam), label=left_cam)
            p.start(on_frame=lambda d, w, h, panel=self._left_panel: panel.update_frame(d, w, h))
            self._previews["left"] = p
            self._left_panel.set_label(f"Cam: {left_cam} ({camera_device(left_cam)})")
            self._left_panel.set_status("live")
        else:
            self._left_panel.set_status(f"offline ({left_cam})")

        # Right panel
        self._right_panel.reset_frame_flag()
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

        # Watchdog: a pipeline can reach PLAYING without ever delivering
        # frames (e.g. the node is the camera's metadata device) — flag it.
        GLib.timeout_add(3000, self._check_preview_frames)

    def _check_preview_frames(self) -> bool:
        for side, panel in (("left", self._left_panel), ("right", self._right_panel)):
            if self._previews.get(side) is not None and not panel.got_frame:
                panel.set_status("no frames — camera busy or wedged?")
        return False  # one-shot

    def _check_daemon_conflict(self) -> bool:
        """afl-camd (the phone service) may own the cameras. Offer a
        one-click handover: the daemon closes its pipeline on request
        (it keeps serving the phone page and resumes automatically once
        the UI closes) — no sudo, no terminal, no restart."""
        try:
            active = subprocess.run(
                ["systemctl", "is-active", "--quiet", "afl-camd"],
                timeout=5).returncode == 0
        except (OSError, subprocess.SubprocessError):
            active = False
        if not active:
            return False
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text="The phone service (afl-camd) is running and owns the cameras.",
        )
        dialog.format_secondary_text(
            "Take over the cameras for this UI session? The phone page stays "
            "available and the service resumes automatically when the UI "
            "closes.")
        dialog.add_button("Take over from phone service", Gtk.ResponseType.OK)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return False
        # the daemon closes the pipeline itself and holds off respawning
        # while the UI opens the cameras
        for port in (80, 8080):
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/release", method="POST",
                    data=b"{}", headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=4)
                break
            except OSError:
                continue
        time.sleep(2)   # let cam2stitch close the cameras
        return False

    def _stop_previews(self):
        for p in self._previews.values():
            p.stop()
        self._previews.clear()

    def _health_check(self) -> bool:
        """Idle watchdog: a camera can drop mid-session (USB glitch,
        wedge). If a device path vanishes, kick + rediscover + restart
        previews. Never runs while recording — cameras are busy then.
        Throttled to one recovery attempt per 30 s."""
        if self._recording or self._stitching or self._realigning:
            return True
        if all(camera_present(c) for c in ("cam0", "cam1")):
            return True
        now = time.time()
        if now - self._health_last_kick < 30:
            return True
        self._health_last_kick = now
        log.warning("camera drop detected — attempting recovery")
        log.info("cameras: %s", ensure_cameras())
        self._start_previews()
        return True

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
        self._session_dir = session_dir
        self._live_stitch = False
        session_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._session_id = self._db.add_session(
            created_at=session_time,
            folder=str(session_dir.relative_to(ROOT_DIR)),
        )

        self._recorders.clear()
        self._db_ids.clear()
        self._durations.clear()

        # Live GPU stitch (cam2stitch): one process records BOTH cameras and
        # writes the panorama directly — no post-stitch step. Falls back to
        # per-camera recording when the probe fails, a camera is missing, or
        # the user chose "Separate + stitch" mode.
        if (LIVE_STITCH and PLATFORM == "linux"
                and self._controls.get_record_mode() == "live"
                and camera_present("cam0") and camera_present("cam1")
                and can_live_stitch()):
            from .engine import StitchRecorderMPP

            dev0 = camera_device("cam0")
            dev1 = camera_device("cam1")
            recorder = StitchRecorderMPP(dev0, dev1, label="stitch",
                                         resolution_key=res_key)
            try:
                output = recorder.start(
                    duration_sec=duration,
                    on_done=self._on_recorder_done,
                    output_dir=session_dir,
                )
            except RuntimeError as e:
                log.error("Live stitch start failed (%s) — per-camera fallback", e)
            else:
                row_id = self._db.add(
                    camera="stitch", device_path=f"{dev0}+{dev1}",
                    resolution=RESOLUTIONS[res_key]["label"],
                    file_path=str(output.relative_to(ROOT_DIR)),
                    status="recording",
                    session_id=self._session_id,
                    created_at=session_time,
                )
                self._db_ids.append(row_id)
                self._recorders.append(recorder)
                log.info("Live stitch recording: %s", output.name)
                self._live_stitch = True
                return self._finish_recording_start()

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

        return self._finish_recording_start()

    def _finish_recording_start(self):
        session_dir = self._session_dir
        if not self._recorders:
            log.error("No cameras available to record")
            self._resume_previews()
            return

        # Record the camera microphones (clap fallback / audio track)
        self._audio_procs = []
        for cam_key in ("cam0", "cam1"):
            proc = start_audio_capture(cam_key, session_dir / f"{cam_key}.wav")
            if proc:
                self._audio_procs.append(proc)

        # Exposure is owned by the recorder: auto-settle-then-lock (or a
        # fixed manual value when EXPOSURE_TIME > 0). The UI must NOT touch
        # it here — a manual set would override the recorder's auto phase.

        self._recording = True
        self._controls.set_recording(True)
        self._stitch_btn.set_sensitive(False)
        self._controls.status_label.set_text("Recording... 0:00")
        self._rec_start = time.time()
        self._timer_id = GLib.timeout_add(1000, self._update_recording_timer)

        # Recording-time live preview: the recorders write raw-JPEG
        # snapshots (<session>/camN_preview.jpg) while they own the
        # cameras; poll for fresh frames so the panels aren't frozen.
        self._preview_mtimes = {"cam0": 0.0, "cam1": 0.0}
        self._preview_live = set()
        self._preview_timer = GLib.timeout_add(200, self._poll_recording_previews)

    def _poll_recording_previews(self) -> bool:
        """1 Hz check for fresh recorder snapshots (GTK thread)."""
        if not self._recording or self._session_dir is None:
            return False
        for cam_key, mtime in list(self._preview_mtimes.items()):
            path = self._session_dir / f"{cam_key}_preview.jpg"
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_mtime <= mtime:
                continue
            self._preview_mtimes[cam_key] = st.st_mtime
            panel = (self._left_panel if self._panel_cams[0] == cam_key
                     else self._right_panel)
            panel.set_jpeg_file(str(path))
            if cam_key not in self._preview_live:
                self._preview_live.add(cam_key)
                panel.set_status("recording — live preview")
        return True

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
        if self._preview_timer:
            GLib.source_remove(self._preview_timer)
            self._preview_timer = None
        self._stop_audio()
        self._controls.set_recording(False)
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



    def _on_open_folder(self, _btn):
        """Open the selected session's folder in the desktop file manager."""
        session_id = self._recording_list.get_selected_session_id()
        if session_id is None:
            return
        folder = self._db.session_folder(session_id)
        if not folder:
            return
        path = Path(folder)
        if not path.is_absolute():
            path = ROOT_DIR / path
        if path.is_dir():
            if PLATFORM == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _on_play(self, _btn):
        # Only individual recordings can be played; session rows do nothing.
        rec_id = self._recording_list.get_selected_recording_id()
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
                subprocess.Popen(["open", str(filepath)])
            else:
                subprocess.Popen(["mpv", str(filepath)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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



    def _on_stitch(self, _btn):
        if self._recording or self._stitching:
            return
        session_id = self._recording_list.get_selected_session_id()
        if session_id is None:
            return

        # Verify the session has both recordings before starting
        recs = {r["camera"]: r for r in self._db.session_recordings(session_id)}
        if "stitch" in recs and Path(str(ROOT_DIR / recs["stitch"]["file_path"])).exists():
            # recorded with live GPU stitch — nothing to do in post
            self._db.update_session_stitch_status(session_id, "stitched")
            self._recording_list.refresh()
            self._controls.status_label.set_text("✓ Stitched live during recording")
            return
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
        self._realign_btn.set_sensitive(not busy)

    def _on_realign(self, _btn):
        """Re-freeze the live-stitch alignment (stills + params regen)."""
        if self._recording or self._stitching or self._realigning:
            return
        self._realigning = True
        # the script opens both cameras — previews must yield them
        self._stop_previews()
        for panel in (self._left_panel, self._right_panel):
            panel.set_status("re-aligning…")
        self._set_buttons_busy(True)
        self._controls.status_label.set_text(
            "Re-aligning cameras (~90s) — capturing stills + stitch params…")
        Thread(target=self._realign_worker, daemon=True).start()

    def _realign_worker(self):
        """Runs off the GTK main thread: recapture_alignment.py."""
        import subprocess

        from .config import ALIGN_PYTHON, ALIGN_SCRIPT, STITCH_CANVAS_WIDTH

        # Output is NOT captured — the script streams its progress to the
        # terminal the UI was launched from (the user must see the bundle,
        # refinement and file writes; a swallowed failure is invisible).
        try:
            result = subprocess.run(
                [ALIGN_PYTHON, str(ALIGN_SCRIPT),
                 "--canvas-width", str(STITCH_CANVAS_WIDTH)],
                timeout=600,
                cwd=str(ROOT_DIR),
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            log.error("re-align failed to run: %s", e)
            ok = False
        else:
            ok = result.returncode == 0
            if not ok:
                log.error("re-align failed with exit code %s", result.returncode)
            else:
                log.info("live-stitch alignment re-frozen")

        def done():
            self._realigning = False
            self._set_buttons_busy(False)
            self._controls.status_label.set_text(
                "✓ Alignment updated — next recording uses it"
                if ok else "Re-align failed — see log")
            for panel in (self._left_panel, self._right_panel):
                panel.set_status("")
            self._start_previews()

        GLib.idle_add(done)



    def _on_destroy(self, _widget):
        self._stop_previews()
        if self._recording:
            for r in self._recorders:
                if r.running:
                    r.stop()
            time.sleep(0.5)
        self._stop_audio()
        self._db.close()


# Application entry point

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
