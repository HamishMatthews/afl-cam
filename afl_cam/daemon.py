"""afl-camd — the field recording daemon (docs/archive/iphone-control-plan.md).

One supervised pipeline + one API + one web page, GoPro-style. Stdlib
only (no cv2/gi) so it runs headless under systemd.

Architecture
------------
Three processes, one job each::

    cam2stitch  (C)       cameras + GPU stitch + master/proxy encode.
                          Long-running: standby until REC_START on its
                          unix control socket. Supervised here.
    afl-camd    (this)    state machine, REST + WebSocket API, web UI,
                          session finalize (mux + thumbnails), clock sync,
                          auto-stop, schedule, GPIO button/LED.
    MediaMTX    (binary)  preview video to the phone (WHEP + HLS), fed by
                          a supervised ffmpeg that picks cam2stitch's
                          Annex-B UDP preview stream up and publishes RTSP.

Golden rule: nothing the phone does can stop a recording. The daemon (and
the whole HTTP layer) can die and cam2stitch keeps recording; on restart
the daemon re-attaches to the control socket and reads the current state.

Endpoints (default port 80, AFL_CAM_PORT overrides)::

    GET  /                          web UI (afl_cam/web/index.html)
    GET  /api/status                full state snapshot
    GET  /api/events                WebSocket: status push ~1/s + changes
    POST /api/record/start          {"request_id"} — idempotent
    POST /api/record/stop           {"request_id"}
    POST /api/time                  {"epoch_ms"} — sets clock if unsynced
    GET  /api/recordings            list (id, time, duration, size, proxy…)
    GET  /api/recordings/<id>/proxy.mp4   HTTP Range (Safari requires 206)
    GET  /api/recordings/<id>/thumb.jpg
    DELETE /api/recordings/<id>
    POST /api/schedule              {"start_at": epoch_ms, "stop_after": s}
    POST /api/realign               re-align the cameras (closes the pipeline; refused while locked)
    POST /api/align-lock            {"locked": bool} — the field alignment is locked by default
    GET  /api/realign               re-align progress + full log + scores
    GET  /api/realign/seam.jpg      seam crop from the last re-align
    GET  /api/selftest              pre-game checks (cameras, alignment, …)
    GET  /api/framing               seam + horizon positions for the preview
    POST /api/mode                  {"mode": "live"|"separate"} (not while recording)
    POST /api/fps-lock              {"locked": bool} — hold 30 fps (exposure capped at 30 ms) or let it fall in dim light
    POST /api/quality               {"qp": 16..24 | 0 = CBR} live-stitch quality (not while recording)
    POST /api/recordings/<id>/stitch   re-stitch a separate-mode session
    GET  /api/live/cam0.jpg|cam1.jpg   separate-mode recording snapshots
    GET  /manifest.webmanifest, /icon-*.png   PWA (Add to Home Screen)

Recording modes: "live" (default) — cam2stitch records the stitched
panorama; "separate" — the pipeline closes and one cam2h264 per camera
records the raw views (the offline stitch test bed), re-stitched later on
request (afl_cam/restitch.py, a subprocess: it needs OpenCV).

State machine: idle → starting → standby → recording → finalizing →
standby, plus error, and realigning (pipeline closed while the re-align
chain owns the cameras; back to idle when it finishes). Threading: one tick loop owns ALL state transitions;
HTTP threads only validate + queue intents (start/stop/…), which the tick
loop executes. The ctl socket reader runs in its own thread and publishes
cam2stitch status into a locked slot.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import queue
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from . import cropsig, framing, selftest, sysmon
from .config import (
    ALIGN_PYTHON, ALIGN_SCRIPT, AUDIO_DEVICES, CAMERAS, DB_PATH,
    ENCODE_CODEC, EXPOSURE_TIME, NATIVE_BITRATE_KBPS, OUTPUTS_DIR,
    PREVIEW_INTERVAL_SECS, ROOT_DIR, STITCH_BITRATE_KBPS, STITCH_QP,
    FPS_LOCK_EXPOSURE_MS, UNLOCKED_EXPOSURE_MS,
    STITCH_QUALITY_PRESETS,
    CROP_PARAMS_DIR, STITCH_CANVAS_WIDTH, STITCH_PARAMS_DIR, STITCH_RECORDER_BIN,
    STITCH_SHADER, _discover_cameras, build_cam2h264, build_cam2stitch,
    rediscover_cameras, vk_env,
)
from . import resume
from .db import RecordingDB
from .sessions import new_session_dir, remove_session_dir, session_dir_is_safe

log = logging.getLogger("afl_cam.daemon")

# ---------------------------------------------------------------- config --

CTL_SOCK = os.environ.get("AFL_CAM_CTL_SOCK", "/tmp/afl-cam-stitch.sock")
PREVIEW_UDP = os.environ.get("AFL_CAM_PREVIEW_UDP", "127.0.0.1:8890")
MEDIAMTX_RTSP_URL = os.environ.get(
    "AFL_CAM_MEDIAMTX_RTSP", "rtsp://127.0.0.1:8554/preview")
HTTP_PORT = int(os.environ.get("AFL_CAM_PORT", "80"))
HTTP_FALLBACK_PORT = 8080
IDLE_STOP_SECS = float(os.environ.get("AFL_CAM_IDLE_SECS", "600"))
TEMP_STOP_C = float(os.environ.get("AFL_CAM_TEMP_STOP_C", "80"))
STORAGE_STOP_GB = float(os.environ.get("AFL_CAM_STORAGE_STOP_GB", "10"))
# camera audio muxed into the master + proxy (issue #10). The mic wavs are
# deleted once the mux that used them succeeded; AFL_CAM_KEEP_WAV=1 keeps
# them (a mux can fail, and the wav is the only copy of the sound).
AUDIO_BITRATE_KBPS = int(os.environ.get("AFL_CAM_AUDIO_BITRATE_KBPS", "128"))
KEEP_WAV = os.environ.get("AFL_CAM_KEEP_WAV") == "1"
AUTH_TOKEN = os.environ.get("AFL_CAM_TOKEN", "")
GPIO_BTN = os.environ.get("AFL_CAM_GPIO_BTN", "")
GPIO_LED = os.environ.get("AFL_CAM_GPIO_LED", "")
WEB_UI = Path(__file__).resolve().parent / "web" / "index.html"
WEB_UI_DIR = WEB_UI.parent
_web_ui_cache = None      # last good copy of the page (see _Handler._index)
LIVE_SNAP_DIR = OUTPUTS_DIR / "_live"    # camN_preview.jpg snapshots land here
WHEP_PORT = 8889
HLS_PORT = 8888
RESOLUTION = {"width": 3840, "height": 2160, "fps": 30}   # stitch pipeline
HEVC = ENCODE_CODEC != "h264"
INTENT_PATH = LIVE_SNAP_DIR / "intent.json"   # afl_cam/resume.py
SETTINGS_PATH = LIVE_SNAP_DIR / "settings.json"   # persisted phone settings (crop, quality)
CROP_TIMEOUT_S = 120
# a reboot later than this after the last heartbeat finalizes the
# recording instead of resuming it (the game is over; don't record the shed)
RESUME_MAX_GAP_S = float(os.environ.get("AFL_CAM_RESUME_MAX_GAP_S", "900"))
# Re-align target: the live map set. The rig test points it at a scratch
# dir (calibration/ is shared by every test run and must not be written).
ALIGN_OUT = Path(os.environ.get("AFL_CAM_ALIGN_OUT") or STITCH_PARAMS_DIR)
SCORE_SCRIPT = ROOT_DIR / "scripts" / "stitch_score.py"
REALIGN_TIMEOUT_S = 900
MAP_FILES = ("params.bin", "map0.bin", "map1.bin", "w0.bin", "w1.bin")
MODES = ("live", "separate")
MASTER_NAMES = ("stitch.mp4", "stitch_video.mp4")   # live, re-stitched
PWA_FILES = {
    "/manifest.webmanifest": ("manifest.webmanifest",
                              "application/manifest+json"),
    "/icon-180.png": ("icon-180.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
}


def _fmt_hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


# ------------------------------------------------------------- supervisors --

def _rediscover_with_kick() -> dict:
    """Re-run camera discovery, kicking wedged devices if a camera is
    missing (stdlib stand-in for capture.resolve_camera_sides)."""
    resolved = rediscover_cameras()
    if resolved and len(resolved) == 2:
        return resolved
    kick = ROOT_DIR / "scripts" / "usb_kick_cameras.sh"
    try:
        subprocess.run(["sudo", "-n", str(kick)],
                       capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass
    return rediscover_cameras() or {}


def _load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_settings(settings: dict) -> None:
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".tmp")
        tmp.write_text(json.dumps(settings))
        tmp.replace(SETTINGS_PATH)
    except OSError as e:
        log.warning("settings: could not save (%s)", e)


class StitchSupervisor(threading.Thread):
    """Owns the cam2stitch process and its control-socket connection.

    Spawns the pipeline (daemon mode: standby until REC_START), keeps one
    client on the ctl socket, parses the ~1/s status pushes into
    ``self.status`` (JSON dict or None), and answers command() calls.
    When the process dies the loop restarts it after ``restart_delay`` —
    the daemon can keep running and the recording state on the socket is
    re-read after re-attach (a died-and-restarted cam2stitch is standby,
    and any unfinished recording files are finalized by the daemon).
    """

    def __init__(self, daemon: "CamDaemon"):
        super().__init__(name="stitch-supervisor", daemon=True)
        self.daemon = daemon
        self.proc: Optional[subprocess.Popen] = None
        self._adopted = False        # socket alive but process is foreign
        self._logf = None
        self._cmd_q: queue.Queue = queue.Queue()   # (line, reply_event)
        self.status: Optional[dict] = None
        self.status_ts = 0.0
        self._status_lock = threading.Lock()
        self._stop = threading.Event()

    # -- process --------------------------------------------------------

    def spawn(self) -> bool:
        """(Re)discover cameras, build if stale, start cam2stitch. The
        process is expected to stay up; returns True once the ctl socket
        accepts connections. If a cam2stitch from a previous daemon run is
        still alive (its socket answers), ADOPT it instead of spawning —
        never unlink a live socket."""
        log.info("stitch: bringing the pipeline up…")
        if not self.build_ok():
            return False
        if self._probe_socket():
            log.warning("stitch: adopting an already-running pipeline "
                        "(previous daemon's process)")
            self.proc = None
            self._adopted = True
            return True
        cams = _rediscover_with_kick()
        if len(cams) < 2:
            log.error("stitch: %d/2 cameras found — %s",
                      len(cams), cams or "none")
            return False
        params_dir = self.daemon.choose_params_dir()
        if not params_dir.exists():
            log.error("stitch: params dir missing: %s", params_dir)
            return False

        env = vk_env()
        env.update({
            "AFL_CAM_CTL_SOCK": CTL_SOCK,
            "AFL_CAM_PREVIEW_UDP": PREVIEW_UDP,
            "AFL_CAM_PREVIEW_DIR": str(LIVE_SNAP_DIR),
            "AFL_CAM_PREVIEW_SECS": "1.0",
            "AFL_CAM_PREVIEW_VIDEO_W": "1600",
            "AFL_CAM_PREVIEW_BITRATE_KBPS": "3500",
            "AFL_CAM_CODEC": ENCODE_CODEC,
            "AFL_CAM_STITCH_BITRATE_KBPS": str(STITCH_BITRATE_KBPS),
            "AFL_CAM_STITCH_QP": str(self.daemon.stitch_qp),
            "AFL_CAM_AE_CAP_MS": str(FPS_LOCK_EXPOSURE_MS if self.daemon.fps_lock
                                     else UNLOCKED_EXPOSURE_MS),
        })
        # the exposure meters the field: canvas rows below the horizon guide
        guides = framing.framing(params_dir)
        if guides and guides.get("horizon") is not None:
            env["AFL_CAM_HORIZON"] = f"{guides['horizon']:.4f}"
        cmd = [
            str(STITCH_RECORDER_BIN),
            cams["cam0"], cams["cam1"],
            str(params_dir),
            str(LIVE_SNAP_DIR / "placeholder.raw.h264"),
            "0",                          # frames: run until QUIT/SIGTERM
            str(RESOLUTION["width"]),
            str(RESOLUTION["height"]),
            str(RESOLUTION["fps"]),
            "0",                          # exposure: the shared field meter
            "0",                          # wall_sec: no deadline
            str(STITCH_SHADER),
        ]
        LIVE_SNAP_DIR.mkdir(parents=True, exist_ok=True)
        if not self.daemon.pipeline_wanted():
            return False            # closed while discovering (re-align)
        try:
            os.unlink(CTL_SOCK)
        except OSError:
            pass
        log.info("stitch: spawn %s", " ".join(cmd))
        logf = open(LIVE_SNAP_DIR / "stitch.log", "w")
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, cwd=str(ROOT_DIR))
        except OSError as e:
            log.error("stitch: spawn failed: %s", e)
            logf.close()
            self.proc = None
            return False
        self._logf = logf
        # filter thread: drop the PRE-EXISTING MPP refcount noise (present
        # since before the daemon era — verified in commit 9ba8dc3 — and
        # benign for the stream, but 2 lines/frame would flood the log:
        # a wedged weekend wrote 400MB before logrotate was added)
        threading.Thread(
            target=self._pump_log, args=(self.proc, logf),
            name="stitch-log", daemon=True).start()
        self._adopted = False
        # wait for the ctl socket (pipeline init takes ~1-3 s)
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.proc.poll() is not None:
                log.error("stitch: exited immediately (rc=%s) — "
                          "cameras busy or init failed",
                          self.proc.returncode)
                return False
            if os.path.exists(CTL_SOCK):
                return True
            time.sleep(0.2)
        log.error("stitch: ctl socket never appeared")
        return False

    def _pump_log(self, proc, logf):
        _BENIGN = (b"ref_count", b"invalid mem pool ptr",
                   b"put_meta invalid negative", b"MppPacket 0x")
        try:
            for line in proc.stdout:
                if any(pat in line for pat in _BENIGN):
                    continue
                try:
                    logf.write(line.decode(errors="replace"))
                    logf.flush()
                except OSError:
                    break
        except (OSError, ValueError):
            pass

    def stop(self):
        self._stop.set()
        try:
            self.command("QUIT", timeout=2)
        except Exception:
            pass
        proc = self.proc
        if proc and proc.poll() is None:
            try:
                proc.send_signal(15)
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()

    def kill_pipeline(self):
        """Hard-reset a wedged pipeline so the supervisor respawns it
        (watchdog path: stuck starting, or status stalled while running)."""
        log.error("stitch: killing the pipeline (watchdog)")
        self.set_status(None)
        proc = self.proc
        self.proc = None
        self._adopted = False
        if proc is None:
            # adopted (foreign) process: ask it nicely over the socket
            try:
                self.command("QUIT", timeout=3)
            except Exception:
                pass
        elif proc.poll() is None:
            try:
                proc.send_signal(15)
                proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except OSError:
                    pass
        if self._logf is not None:
            try:
                self._logf.close()
            except OSError:
                pass
            self._logf = None

    def build_ok(self) -> bool:
        return build_cam2stitch() is not None

    def _probe_socket(self) -> bool:
        """True when the ctl socket accepts a connection (a cam2stitch is
        alive — possibly left over from a previous daemon instance)."""
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(0.5)
            s.connect(CTL_SOCK)
            s.close()
            return True
        except OSError:
            return False

    # -- ctl client ------------------------------------------------------

    def command(self, line: str, timeout: float = 5.0) -> str:
        """Send one ctl command; wait for its reply line. Raises
        RuntimeError on timeout (socket down or cam2stitch stuck)."""
        ev = threading.Event()
        slot: list = [None]
        self._cmd_q.put((line, ev, slot))
        if not ev.wait(timeout):
            raise RuntimeError(f"ctl command timed out: {line}")
        return slot[0] or "ERR no reply"

    def set_status(self, st: Optional[dict]):
        with self._status_lock:
            self.status = st
            self.status_ts = time.monotonic()

    def get_status(self) -> Optional[dict]:
        with self._status_lock:
            return self.status

    # -- thread ----------------------------------------------------------

    def run(self):
        restart_delay = 1.0
        while not self._stop.is_set():
            if not self.daemon.pipeline_wanted():
                if self._stop.wait(1.0):
                    return
                continue
            if not self.spawn():
                self.set_status(None)
                if self._stop.wait(min(restart_delay, 30)):
                    return
                restart_delay = min(restart_delay * 2, 30)
                continue
            restart_delay = 1.0
            self._serve_socket()
            # socket EOF => process gone or socket torn down
            self.set_status(None)
            self.daemon.on_stitch_exit()
            proc = self.proc
            if proc is not None and proc.poll() is None:
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if self._stop.wait(1.0):
                return

    def _serve_socket(self):
        """One client connection to the ctl socket: forwards commands,
        parses pushed status lines, watches for EOF."""
        sock = None
        deadline = time.time() + 10
        while time.time() < deadline and not self._stop.is_set():
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(0.5)
                sock.connect(CTL_SOCK)
                break
            except OSError:
                sock and sock.close()
                sock = None
                time.sleep(0.25)
        if sock is None:
            return
        log.info("stitch: control socket connected")
        buf = b""
        try:
            while not self._stop.is_set():
                # send pending commands
                try:
                    line, ev, slot = self._cmd_q.get_nowait()
                except queue.Empty:
                    pass
                else:
                    try:
                        sock.sendall((line + "\n").encode())
                        # wait for the reply: it arrives as a non-JSON line
                        deadline = time.time() + 5
                        while time.time() < deadline:
                            reply, buf = self._recv_line(sock, buf, 0.5)
                            if reply is None:
                                continue
                            if reply.startswith("{") and reply.endswith("}"):
                                self._maybe_status(reply)
                                continue
                            slot[0] = reply
                            ev.set()
                            break
                        else:
                            slot[0] = "ERR timeout"
                            ev.set()
                    except OSError as e:
                        slot[0] = f"ERR socket: {e}"
                        ev.set()
                        raise
                # read pushed status lines
                try:
                    line, buf = self._recv_line(sock, buf, 0.5)
                except OSError:
                    break
                if line is None:
                    continue
                self._maybe_status(line)
        except OSError:
            pass
        finally:
            sock.close()
            self._adopted = False
        log.info("stitch: control socket closed")

    @staticmethod
    def _recv_line(sock, buf, timeout):
        sock.settimeout(timeout)
        while b"\n" not in buf:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                return None, buf
            if not data:
                raise OSError("EOF")
            buf += data
        line, _, rest = buf.partition(b"\n")
        return line.decode(errors="replace"), rest

    def _maybe_status(self, line: str):
        try:
            st = json.loads(line)
        except ValueError:
            return
        if isinstance(st, dict) and "state" in st and "cam0" in st:
            self.set_status(st)


# ---------------------------------------------------------------- preview --

class PreviewChain(threading.Thread):
    """Supervised ffmpeg: cam2stitch's Annex-B UDP → RTSP publish to
    MediaMTX (stream copy — no re-encode). Restarts while the pipeline
    lives; dies silently when cam2stitch is down (UDP goes quiet)."""

    def __init__(self, daemon: "CamDaemon"):
        super().__init__(name="preview-chain", daemon=True)
        self.daemon = daemon
        self.proc: Optional[subprocess.Popen] = None
        self.up = False
        self._lock = threading.Lock()

    def run(self):
        while not self.daemon.stopping:
            if not self.daemon.stitch_running():
                time.sleep(2)
                continue
            self._spawn()
            self.proc.wait()
            with self._lock:
                self.up = False
            log.warning("preview: ffmpeg pusher exited (rc=%s) — restarting",
                        self.proc.returncode)
            time.sleep(3)

    def _spawn(self):
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-f", "h264", "-i", f"udp://{PREVIEW_UDP}",
            "-c", "copy", "-f", "rtsp", MEDIAMTX_RTSP_URL,
        ]
        chain_log = open(LIVE_SNAP_DIR / "preview-chain.log", "w")
        self.proc = subprocess.Popen(
            cmd, stdout=chain_log, stderr=subprocess.STDOUT)
        with self._lock:
            self.up = True
        log.info("preview: pushing to %s", MEDIAMTX_RTSP_URL)

    def stop(self):
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()


# --------------------------------------------------------------- finalize --

def _read_pts(pts_path: Path) -> list[int]:
    """Kernel timestamps from a .pts sidecar (stdlib retime.load_pts)."""
    try:
        return [int(x) for x in pts_path.read_text().split()]
    except (OSError, ValueError):
        return []


def _pts_median_fps(pts_path: Path, fallback: float) -> float:
    """Median frame rate from a .pts sidecar (the stitcher's CFR raws)."""
    pts = _read_pts(pts_path)
    if len(pts) < 3:
        return fallback
    dts = sorted(
        b - a for a, b in zip(pts, pts[1:]) if b - a > 1000)
    if not dts:
        return fallback
    return 1e6 / dts[len(dts) // 2]


def _pts_bounds(pts_path: Path) -> Optional[tuple[int, float]]:
    """(first timestamp us, video duration s) from a .pts sidecar.

    The duration is the container's: the last frame's stamp plus its own
    interval, because playback ends when the last frame's time is up.
    """
    pts = _read_pts(pts_path)
    if len(pts) < 2:
        return None
    return pts[0], (pts[-1] - pts[0] + pts[-1] - pts[-2]) / 1e6


def _wav_seconds(wav: Path) -> float:
    """How much audio a wav holds (0.0 when arecord wrote none)."""
    try:
        with wave.open(str(wav)) as w:
            rate = w.getframerate()
            return w.getnframes() / rate if rate else 0.0
    except (OSError, EOFError, wave.Error):
        return 0.0


def _audio_offset_s(wav: Path, first_pts_us: int) -> float:
    """Seconds to DELAY the mic so it lines up with the video's first frame
    (negative = the mic started late, so its head gets dropped).

    arecord's spawn time rides next to the wav as a `.start` marker, in the
    same kernel monotonic clock as the `.pts` sidecar. 0.0 when there is no
    usable marker, or when the offset is longer than the audio itself —
    that only happens with a marker left over from another run, and padding
    the whole track with silence would be worse than the unaligned file.
    """
    try:
        start_us = int(resume.audio_marker(wav).read_text().strip())
    except (OSError, ValueError):
        return 0.0
    offset = (first_pts_us - start_us) / 1e6
    length = _wav_seconds(wav)
    if length <= 0 or abs(offset) >= length:
        log.warning("finalize: ignoring %+.3f s mic offset for %s "
                    "(%.1f s of audio)", offset, wav.name, length)
        return 0.0
    return offset


def _make_audio_track(wav: Path, first_pts_us: int, duration_s: float,
                      out: Path) -> bool:
    """Encode the mic wav to AAC lined up with the video's first frame and
    trimmed to the video's length, ready for the muxer to copy in."""
    offset = _audio_offset_s(wav, first_pts_us)
    args = []
    if offset < 0:                      # the mic started late: drop its head
        args += ["-ss", f"{-offset:.3f}"]
    args += ["-i", str(wav)]
    if offset > 0:                      # the mic started early: pad the head
        # one delay per channel (arecord runs -c 2): `all=1` says the same
        # but only exists from ffmpeg 4.3
        ms = int(round(offset * 1000))
        args += ["-af", f"adelay={ms}|{ms}"]
    args += ["-t", f"{duration_s:.3f}", "-c:a", "aac",
             "-b:a", f"{AUDIO_BITRATE_KBPS}k", str(out)]
    ok, err = _run_ffmpeg(args, timeout=600)
    if ok and out.exists() and out.stat().st_size > 0:
        log.info("finalize: %s -> AAC, mic offset %+.3f s", wav.name, offset)
        return True
    log.error("finalize: audio encode failed for %s: %s", wav.name, err)
    return False


def _mux(raw: Path, pts: Path, out: Path, codec: str,
         tag: Optional[str], audio: Optional[Path], faststart: bool) -> bool:
    """Retime a raw elementary stream into `out` from its .pts sidecar,
    with the camera's mic muxed in when there is one.

    The container carries the sidecar's real per-frame timing, so duration,
    playback speed and the audio all match the game even when auto-exposure
    changed the sensor rate mid-game. There is deliberately NO fallback to
    a fixed frame rate: a mux that cannot use the sidecar fails, and the
    raw files stay on disk (issue #9).
    """
    # local import: retime pulls in numpy, which the daemon itself avoids
    from .retime import retime_file

    track = None
    if audio is not None:
        bounds = _pts_bounds(pts)
        length = _wav_seconds(audio)
        if bounds is None or length <= 0.1:
            # say why, or the field report is "the recording has no sound"
            # with nothing in the log to explain it
            log.warning("finalize: no mic for %s (%s holds %.1f s) — muxing "
                        "video only", raw.name, audio.name, length)
        else:
            track = out.with_suffix(".audio.m4a")
            if not _make_audio_track(audio, bounds[0], bounds[1], track):
                track.unlink(missing_ok=True)
                track = None
    try:
        ok = retime_file(raw, pts, out, codec=codec, tag=tag,
                         faststart=faststart, audio=track)
    finally:
        if track is not None:
            track.unlink(missing_ok=True)
    if not ok:
        log.error("finalize: mux failed for %s", raw.name)
    return ok


def mux_master(raw: Path, pts: Path, out: Path,
               audio: Optional[Path] = None,
               faststart: bool = False) -> bool:
    """stitch.raw.h264 (or a separate-mode camN.raw.h264) → .mp4 carrying
    the real frame timing from the .pts sidecar, plus the camera's audio.

    HEVC masters carry the hvc1 tag — without it iPhone/QuickTime refuse
    to play the file (docs/archive/iphone-control-plan.md §4)."""
    return _mux(raw, pts, out, "hevc" if HEVC else "h264",
                "hvc1" if HEVC else None, audio, faststart)


def _segments_report(folder: Optional[Path]) -> Optional[dict]:
    """{restarts, lost_s} for a recording that resumed after a crash."""
    try:
        rep = json.loads((folder / "segments.json").read_text())
        return {"restarts": rep["restarts"], "lost_s": rep["lost_s"]}
    except (TypeError, OSError, ValueError, KeyError):
        return None


def _run_ffmpeg(args: list[str], timeout: int = 300) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return result.returncode == 0, result.stderr.strip()


def mux_proxy(raw: Path, pts: Path, out: Path,
              audio: Optional[Path] = None) -> bool:
    """preview.raw.h264 → proxy.mp4 (H.264 avc1, faststart — Safari's
    seek requirement). The preview encoder is always H.264, whatever the
    master's codec. Carries the camera's audio too."""
    return _mux(raw, pts, out, "h264", None, audio, faststart=True)


def make_thumb(video: Path, out: Path) -> bool:
    ok, err = _run_ffmpeg(
        ["-ss", "1", "-i", str(video),
         "-frames:v", "1", "-vf", "scale=480:-2", str(out)], timeout=60)
    return ok and out.exists() and out.stat().st_size > 0


class Finalizer(threading.Thread):
    """Mux + thumbnail a finished session (runs off the tick loop)."""

    def __init__(self, daemon: "CamDaemon", session: "Session"):
        super().__init__(name="finalize", daemon=True)
        self.daemon = daemon
        self.session = session

    def run(self):
        if self.session.kind == "separate":
            return self._run_separate()
        s = self.session
        dir_ = s.dir
        # every run of cam2stitch in this session is a segment (one unless
        # the recording resumed after a crash — afl_cam/resume.py)
        master = resume.segments(dir_, "stitch.raw.h264")
        proxy = resume.segments(dir_, "preview.raw.h264")

        # each segment carries the mic take arecord wrote beside it
        def master_mux(raw, pts, out):
            return mux_master(raw, pts, out, audio=resume.audio_for(raw))

        def proxy_mux(raw, pts, out):
            return mux_proxy(raw, pts, out, audio=resume.audio_for(raw))

        master_ok, master_all = resume.mux_segments(
            master, dir_ / "stitch.mp4", master_mux,
            ("-tag:v", "hvc1") if HEVC else ()) if master else (False, False)
        proxy_ok, proxy_all = resume.mux_segments(
            proxy, dir_ / "proxy.mp4", proxy_mux,
            ("-movflags", "+faststart")) if proxy else (False, False)
        if proxy_ok:
            make_thumb(dir_ / "proxy.mp4", dir_ / "thumb.jpg")
        if s.restarts:
            resume.write_report(dir_, s.restarts, s.gaps)
            log.warning("finalize: session %d resumed %d time(s), gaps %s s",
                        s.session_id, s.restarts,
                        [g["gap_s"] for g in s.gaps])
        # raw elementary streams are gone once muxed (the UI's remux
        # deletes them too); keep the small .pts sidecars for analysis
        if master_all:
            for raw, _ in master:
                raw.unlink(missing_ok=True)
                if not KEEP_WAV:
                    # the master carries the sound now (a proxy that failed
                    # is redone from the master, not from the wav)
                    resume.drop_audio(raw)
        if proxy_all:
            for raw, _ in proxy:
                raw.unlink(missing_ok=True)
        size = (dir_ / "stitch.mp4").stat().st_size if master_ok else 0
        db = RecordingDB(DB_PATH)
        try:
            db.update_complete(s.recording_id, s.duration, size)
            db.update_session_stitch_status(
                s.session_id, "stitched" if master_ok else "failed")
        finally:
            db.close()
        self.daemon.on_finalized(s, master_ok, proxy_ok)
        log.info("finalize: session %d %s (master=%s proxy=%s %.0fs)",
                 s.session_id, dir_.name, master_ok, proxy_ok, s.duration)

    def _run_separate(self):
        """Per-camera files: mux each (cam0 faststart), proxy.mp4 is a hard
        link to cam0.mp4 (no second copy of a 4K file), thumbnail."""
        s = self.session
        dir_ = s.dir
        ok = {}
        db = RecordingDB(DB_PATH)
        try:
            for cam, row_id in s.recording_ids.items():
                raw = dir_ / f"{cam}.raw.h264"
                out = dir_ / f"{cam}.mp4"
                ok[cam] = raw.exists() and mux_master(
                    raw, dir_ / f"{cam}.pts", out,
                    audio=resume.audio_for(raw), faststart=cam == "cam0")
                if ok[cam]:
                    raw.unlink(missing_ok=True)
                    if not KEEP_WAV:      # this camera's own mic
                        resume.drop_audio(raw, (cam,))
                    db.update_complete(row_id, s.duration, out.stat().st_size)
                else:
                    db.update_error(row_id, "mux failed or no frames")
            proxy_ok = False
            if ok.get("cam0"):
                try:
                    os.link(dir_ / "cam0.mp4", dir_ / "proxy.mp4")
                    proxy_ok = True
                except OSError as e:
                    log.error("finalize: proxy link failed: %s", e)
            if proxy_ok:
                make_thumb(dir_ / "proxy.mp4", dir_ / "thumb.jpg")
            db.update_session_stitch_status(s.session_id, "none")
        finally:
            db.close()
        both = all(ok.get(c) for c in ("cam0", "cam1"))
        self.daemon.on_finalized(s, both, proxy_ok)
        log.info("finalize: separate session %d %s (%s, %.0fs)",
                 s.session_id, dir_.name, ok, s.duration)


class SeparateRecorder:
    """Separate mode: one cam2h264 per camera, raw views on disk (no stitch
    at record time). SIGINT stops them gracefully (flush + close)."""

    def __init__(self, dir_: Path, devices: dict):
        self.dir = dir_
        self.devices = devices
        self.procs: dict = {}

    def start(self) -> Optional[str]:
        """Start both recorders; an error message if either fails."""
        binary = build_cam2h264()
        if binary is None:
            return "per-camera recorder failed to build — see board log"
        env = dict(os.environ, AFL_CAM_CODEC=ENCODE_CODEC,
                   AFL_CAM_BITRATE_KBPS=str(NATIVE_BITRATE_KBPS),
                   AFL_CAM_PREVIEW_SECS=str(PREVIEW_INTERVAL_SECS))
        for cam in ("cam0", "cam1"):
            cmd = [str(binary), self.devices[cam],
                   str(self.dir / f"{cam}.raw.h264"), "0",
                   str(RESOLUTION["width"]), str(RESOLUTION["height"]),
                   str(RESOLUTION["fps"]), str(EXPOSURE_TIME), "0"]
            try:
                with open(self.dir / f"{cam}.rec.log", "w") as logf:
                    self.procs[cam] = subprocess.Popen(
                        cmd, stdout=logf, stderr=subprocess.STDOUT, env=env)
            except OSError as e:
                self.stop()
                return f"{cam} recorder failed to start: {e}"
        # a recorder that can't open its camera exits within a second
        deadline = time.time() + 3
        while time.time() < deadline:
            dead = self.dead()
            if dead:
                self.stop()
                return f"{dead} recorder exited at start — camera busy?"
            time.sleep(0.1)
        return None

    def dead(self) -> Optional[str]:
        for cam, proc in self.procs.items():
            if proc.poll() is not None:
                return cam
        return None

    def stop(self, timeout: float = 15):
        for proc in self.procs.values():
            if proc.poll() is None:
                try:
                    proc.send_signal(2)          # SIGINT: flush + close
                except OSError:
                    pass
        deadline = time.time() + timeout
        for proc in self.procs.values():
            try:
                proc.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                log.warning("separate: recorder ignored SIGINT — killing")
                proc.kill()


class RestitchJob(threading.Thread):
    """Re-stitch one separate-mode session (afl_cam/restitch.py in a
    niced subprocess), keeping its latest progress line for the phone."""

    def __init__(self, session_id: int):
        super().__init__(name="restitch", daemon=True)
        self.session_id = session_id
        self.progress = "starting…"
        self.ok: Optional[bool] = None
        self.finished_at: Optional[float] = None

    def summary(self) -> dict:
        return {"session_id": self.session_id,
                "running": self.finished_at is None,
                "progress": self.progress, "ok": self.ok}

    def run(self):
        cmd = ["nice", "-n", "10", sys.executable, "-m", "afl_cam.restitch",
               str(self.session_id)]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", bufsize=1, cwd=str(ROOT_DIR),
                env=dict(os.environ, PYTHONUNBUFFERED="1"))
            for line in proc.stdout:
                if line.strip():
                    self.progress = line.strip()[-160:]
                    log.info("restitch %d: %s", self.session_id, self.progress)
            self.ok = proc.wait() == 0
        except OSError as e:
            self.progress, self.ok = f"failed to run: {e}", False
        self.finished_at = time.time()


class Session:
    """Bookkeeping for one recording (folder, DB rows, audio procs)."""

    def __init__(self, dir_: Path, session_id: int, recording_id: int,
                 audio: list, kind: str = "live",
                 recorder: Optional[SeparateRecorder] = None,
                 recording_ids: Optional[dict] = None):
        self.dir = dir_
        self.session_id = session_id
        self.recording_id = recording_id
        self.audio = audio
        self.kind = kind                      # live | separate
        self.recorder = recorder              # separate mode only
        self.recording_ids = recording_ids or {}
        self.start = time.time()       # this segment's start
        self.duration = 0.0            # final, set at stop
        self.elapsed_before = 0.0      # earlier segments (crash resume)
        self.restarts = 0
        self.gaps: list = []           # [{"at_s", "gap_s"}] per resume
        self.lost_at = 0.0             # when the live segment died

    def elapsed(self, seg_elapsed: Optional[float] = None) -> float:
        """Recorded time over all segments (seg_elapsed: cam2stitch's own
        count for the live one, else wall clock)."""
        if seg_elapsed is None:
            seg_elapsed = time.time() - self.start
        return self.elapsed_before + seg_elapsed

    def intent(self) -> dict:
        return {"dir": str(self.dir), "session_id": self.session_id,
                "recording_id": self.recording_id,
                "elapsed_before": self.elapsed_before,
                "seg_start": self.start, "restarts": self.restarts,
                "gaps": self.gaps, "alive_at": time.time()}

    @classmethod
    def from_intent(cls, intent: dict) -> "Session":
        s = cls(Path(intent["dir"]), int(intent["session_id"]),
                int(intent["recording_id"]), [])
        s.elapsed_before = float(intent.get("elapsed_before") or 0.0)
        s.restarts = int(intent.get("restarts") or 0)
        s.gaps = list(intent.get("gaps") or [])
        return s

    def stop_audio(self):
        for proc in self.audio:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


# -------------------------------------------------------------- websocket --

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WSClient:
    """Server side of one WebSocket connection (RFC 6455, text only)."""

    def __init__(self, sock: socket.socket, on_close):
        self.sock = sock
        self.on_close = on_close
        self.closed = False

    def send_text(self, text: str) -> bool:
        payload = text.encode("utf-8")
        n = len(payload)
        if n < 126:
            header = bytes([0x81, n])
        elif n < 65536:
            header = struct.pack("!BBH", 0x81, 126, n)
        else:
            header = struct.pack("!BBQ", 0x81, 127, n)
        try:
            self.sock.sendall(header + payload)
            return True
        except OSError:
            self.close()
            return False

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.sendall(b"\x88\x00")  # close, no payload
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.on_close(self)

    def reader(self):
        """Blocks: answer pings, honour closes, ignore other frames."""
        try:
            while not self.closed:
                hdr = self._read_exact(2)
                if hdr is None:
                    break
                b0, b1 = hdr[0], hdr[1]
                opcode = b0 & 0x0F
                ln = b1 & 0x7F
                if ln == 126:
                    ext = self._read_exact(2)
                    ln = struct.unpack("!H", ext)[0] if ext else 0
                elif ln == 127:
                    ext = self._read_exact(8)
                    ln = struct.unpack("!Q", ext)[0] if ext else 0
                if ln > 1 << 20:
                    break
                rest = self._read_exact(ln) if ln else b""
                if rest is None and ln:
                    break
                if opcode == 8:      # close
                    self.close()
                    break
                elif opcode == 9:    # ping -> pong (masked ping: copy data)
                    try:
                        self.sock.sendall(b"\x8a" + bytes([ln]) + rest)
                    except OSError:
                        break
                # text/binary frames from the phone are ignored — control
                # goes over REST; the WS is a push channel
        except OSError:
            pass
        finally:
            self.close()

    def _read_exact(self, n: int) -> Optional[bytes]:
        data = b""
        while len(data) < n:
            try:
                chunk = self.sock.recv(n - len(data))
            except OSError:
                return None
            if not chunk:
                return None
            data += chunk
        return data


# --------------------------------------------------------------- realign --

class RealignJob(threading.Thread):
    """Runs the re-align chain (scripts/recapture_alignment.py) while the
    daemon holds the pipeline closed, keeping its output for the phone,
    then scores the new stills against the old and the new map set (the
    before/after the Setup view shows). The daemon owns the state; this
    thread only runs processes and records what they said."""

    LOG_LINES = 500
    SCORE_KEYS = ("misalign_median", "misalign_p95", "misalign_tiles",
                  "luma_ratio", "luma_step")

    def __init__(self, daemon: "CamDaemon", out_dir: Path = None):
        super().__init__(name="realign", daemon=True)
        self.daemon = daemon
        self.out_dir = Path(out_dir or ALIGN_OUT)
        self.crop = LIVE_SNAP_DIR / "realign_seam.jpg"
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.ok: Optional[bool] = None
        self.error: Optional[str] = None
        self.before: Optional[dict] = None
        self.after: Optional[dict] = None
        self._lines: list = []
        self._lock = threading.Lock()

    def say(self, line: str):
        line = line.rstrip()
        log.info("realign: %s", line)
        with self._lock:
            self._lines.append(line)
            del self._lines[:-self.LOG_LINES]

    def lines(self) -> list:
        with self._lock:
            return list(self._lines)

    def summary(self, tail: int = 12) -> dict:
        return {
            "running": self.is_alive() and self.finished_at is None,
            "ok": self.ok,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "before": self.before,
            "after": self.after,
            "has_crop": bool(self.ok and self.crop.is_file()),
            "log_tail": self.lines()[-tail:],
        }

    def run(self):
        try:
            self._run()
        except Exception as e:          # never leave the daemon parked
            log.exception("realign crashed")
            self.ok, self.error = False, f"re-align crashed: {e}"
        if self.error:
            self.say("✗ " + self.error)
        self.finished_at = time.time()
        self.daemon.on_realign_done(self)

    def _run(self):
        LIVE_SNAP_DIR.mkdir(parents=True, exist_ok=True)
        try:
            self.crop.unlink()
        except OSError:
            pass
        # keep the current map set: "before" scores the NEW stills on it
        with tempfile.TemporaryDirectory(prefix="realign-before-",
                                         dir=LIVE_SNAP_DIR) as before:
            before = Path(before)
            have_before = self._snapshot_maps(STITCH_PARAMS_DIR, before)
            self.say("capturing stills and fitting the alignment "
                     "(about 2 minutes)…")
            rc = self._stream([ALIGN_PYTHON, "-u", str(ALIGN_SCRIPT),
                               str(self.out_dir), "--canvas-width",
                               str(STITCH_CANVAS_WIDTH)])
            if rc != 0:
                self.ok = False
                self.error = (f"re-align failed (exit {rc}) — the previous "
                              "alignment is still in use")
                return
            self.say("scoring the new alignment…")
            self.after = self._score(self.out_dir, self.crop)
            if have_before:
                self.before = self._score(before, None)
        self.ok = True
        self.say("✓ alignment updated — the next recording uses it")

    @staticmethod
    def _snapshot_maps(src: Path, dst: Path) -> bool:
        try:
            for name in MAP_FILES:
                shutil.copy2(src / name, dst / name)
        except OSError:
            return False
        return True

    def _stream(self, cmd: list) -> int:
        """Run cmd, forwarding each output line to the log as it comes."""
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", bufsize=1, env=env,
                cwd=str(ROOT_DIR))
        except OSError as e:
            self.say(f"cannot run {Path(cmd[0]).name}: {e}")
            return -1
        killer = threading.Timer(REALIGN_TIMEOUT_S, proc.kill)
        killer.start()
        try:
            for line in proc.stdout:
                if line.strip():
                    self.say(line)
            return proc.wait()
        finally:
            killer.cancel()

    def _score(self, maps: Path, crop: Optional[Path]) -> Optional[dict]:
        """scripts/stitch_score.py on the stills the re-align left in
        out_dir, against the map set in `maps`."""
        cmd = [ALIGN_PYTHON, str(SCORE_SCRIPT),
               str(self.out_dir / "cam0.rgb"), str(self.out_dir / "cam1.rgb"),
               "--maps", str(maps)]
        if crop is not None:
            cmd += ["--crop", str(crop)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=300, cwd=str(ROOT_DIR))
            score = json.loads(proc.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            self.say(f"scoring failed: {e}")
            return None
        if proc.returncode != 0 or not isinstance(score, dict):
            self.say(f"scoring failed: {proc.stderr.strip()[-200:]}")
            return None
        return {k: score.get(k) for k in self.SCORE_KEYS}


# ------------------------------------------------------------------ http --

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AflCamd/1.0"
    sys_version = ""
    timeout = 30.0

    def log_message(self, fmt, *args):
        log.debug("http %s: %s", self.client_address[0], fmt % args)

    @property
    def daemon(self) -> "CamDaemon":
        return self.server.daemon  # type: ignore[attr-defined]

    # -- plumbing --------------------------------------------------------

    def _json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    def _ok(self, **extra):
        self._json({"ok": True, **extra})

    def _err(self, msg, status=200):
        self._json({"ok": False, "error": msg}, status=status)

    def _authed(self) -> bool:
        """Optional per-device token (docs/archive/iphone-control-plan.md §5): when
        AFL_CAM_TOKEN is set, mutating endpoints require it."""
        if not AUTH_TOKEN:
            return True
        got = self.headers.get("X-AFL-Token", "")
        return got == AUTH_TOKEN

    def _body_json(self) -> Optional[dict]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > 64 * 1024:
            return None
        try:
            body = json.loads(self.rfile.read(length).decode())
        except (ValueError, UnicodeDecodeError):
            return None
        return body if isinstance(body, dict) else None

    def _serve_file(self, path: Path, content_type: str,
                    range_ok: bool = False):
        """Serve a static file, with HTTP Range support for proxy.mp4
        (Safari refuses to play or seek without 206 responses)."""
        if not path.is_file():
            return self._err("not found", status=404)
        size = path.stat().st_size
        start, end = 0, size - 1
        status = 200
        if range_ok:
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                try:
                    spec = rng[6:].split("-", 1)
                    start = int(spec[0]) if spec[0] else 0
                    end = int(spec[1]) if len(spec) > 1 and spec[1] else size - 1
                    end = min(end, size - 1)
                    if start > end or start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                    status = 206
                except ValueError:
                    pass
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range",
                             f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(1 << 16, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except OSError:
            pass

    # -- routes ----------------------------------------------------------

    def do_GET(self):
        self.daemon.touch()
        path = urlsplit(self.path).path
        if path == "/":
            return self._index()
        if path == "/hls.min.js":
            # bundled hls.js — Chrome/Firefox need MSE to play HLS; a CDN
            # would fail at the ground (the AP has no internet)
            return self._serve_file(WEB_UI_DIR / "hls.min.js",
                                    "text/javascript")
        if path in PWA_FILES:
            name, ctype = PWA_FILES[path]
            return self._serve_file(WEB_UI_DIR / name, ctype)
        if path == "/api/status":
            return self._json(self.daemon.snapshot())
        if path == "/api/realign":
            return self._json({"ok": True, **self.daemon.realign_status(full=True)})
        if path == "/api/realign/seam.jpg":
            job = self.daemon.realign
            if job is None or not job.ok:
                return self._err("not found", status=404)
            return self._serve_file(job.crop, "image/jpeg")
        if path == "/api/selftest":
            return self._json({"ok": True, **self.daemon.selftest()})
        if path in ("/api/live/cam0.jpg", "/api/live/cam1.jpg"):
            snap = self.daemon.live_snapshot(path[-8:-4])
            if snap is None:
                return self._err("not found", status=404)
            return self._serve_file(snap, "image/jpeg")
        if path == "/api/framing":
            return self._json({"ok": True,
                               "framing": framing.framing(self.daemon.params_dir)})
        if path == "/api/events":
            return self._upgrade_ws()
        if path == "/api/recordings":
            return self._json({"ok": True,
                               "recordings": self.daemon.recordings()})
        if path.startswith("/api/recordings/"):
            return self._recording_file(path)
        self._err("not found", status=404)

    def _index(self):
        global _web_ui_cache
        try:
            data = _web_ui_cache = WEB_UI.read_bytes()
        except OSError as e:
            # a deploy rewriting the file or a disk hiccup: keep serving
            # the last good copy, and say why when there is none
            log.error("web UI unreadable (%s): %s", WEB_UI, e)
            if _web_ui_cache is None:
                return self._err(f"web UI unreadable: {e}", status=500)
            data = _web_ui_cache
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    def _recording_file(self, path: str):
        parts = path.split("/")
        # /api/recordings/<id>/proxy.mp4 | thumb.jpg
        if len(parts) != 5 or not parts[3].isdigit():
            return self._err("not found", status=404)
        sid = int(parts[3])
        folder = self.daemon.session_folder(sid)
        if folder is None:
            return self._err("not found", status=404)
        name = parts[4]
        if name == "proxy.mp4":
            return self._serve_file(folder / "proxy.mp4", "video/mp4",
                                    range_ok=True)
        if name == "thumb.jpg":
            return self._serve_file(folder / "thumb.jpg", "image/jpeg")
        if name == "master.mp4":
            master = session_master(folder)
            if master is None:
                return self._err("not found", status=404)
            return self._serve_file(master, "video/mp4", range_ok=True)
        return self._err("not found", status=404)

    def _upgrade_ws(self):
        if self.headers.get("Upgrade", "").lower() != "websocket":
            return self._err("expected websocket upgrade", status=426)
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        try:
            self.wfile.flush()
            # dup() the raw socket: it survives the handler teardown (the
            # server closes self.connection when this handler returns)
            ws = WSClient(self.connection.dup(),
                          self._ws_closed)
        except OSError:
            return
        self.server._ws_conns.add(self.connection)
        self.close_connection = True
        self.daemon.add_ws_client(ws)
        ws.send_text(json.dumps(self.daemon.snapshot()))
        threading.Thread(target=ws.reader, daemon=True).start()

    def _ws_closed(self, ws):
        self.server._ws_conns.discard(self.connection)
        self.daemon.remove_ws_client(ws)

    def do_POST(self):
        self.daemon.touch()
        path = urlsplit(self.path).path
        body = self._body_json()
        if path == "/api/record/start":
            return self._record_start(body)
        if path == "/api/record/stop":
            return self._record_stop(body)
        if path == "/api/time":
            return self._set_time(body)
        if path == "/api/schedule":
            return self._schedule(body)
        if path == "/api/mode":
            if not self._authed():
                return self._err("forbidden", status=403)
            ok, msg = self.daemon.set_mode((body or {}).get("mode"))
            return self._json({"ok": ok, "error": msg})
        if path == "/api/crop":
            if not self._authed():
                return self._err("forbidden", status=403)
            ok, msg = self.daemon.set_crop((body or {}).get("enabled"))
            return self._json({"ok": ok, "error": msg})
        if path == "/api/quality":
            if not self._authed():
                return self._err("forbidden", status=403)
            ok, msg = self.daemon.set_quality((body or {}).get("qp"))
            return self._json({"ok": ok, "error": msg})
        if path == "/api/fps-lock":
            if not self._authed():
                return self._err("forbidden", status=403)
            ok, msg = self.daemon.set_fps_lock((body or {}).get("locked"))
            return self._json({"ok": ok, "error": msg})
        if path == "/api/align-lock":
            if not self._authed():
                return self._err("forbidden", status=403)
            ok, msg = self.daemon.set_align_locked((body or {}).get("locked"))
            return self._json({"ok": ok, "error": msg})
        if path == "/api/realign":
            if not self._authed():
                return self._err("forbidden", status=403)
            ok, msg = self.daemon.intent_realign()
            return self._json({"ok": ok, "error": msg})
        parts = path.split("/")
        if len(parts) == 5 and parts[1:3] == ["api", "recordings"] \
                and parts[3].isdigit() and parts[4] == "proxy":
            return self._make_proxy(int(parts[3]))
        if len(parts) == 5 and parts[1:3] == ["api", "recordings"] \
                and parts[3].isdigit() and parts[4] == "stitch":
            if not self._authed():
                return self._err("forbidden", status=403)
            ok, msg = self.daemon.start_restitch(int(parts[3]))
            return self._json({"ok": ok, "error": msg})
        self._err("not found", status=404)

    def _make_proxy(self, sid: int):
        """Generate an iPhone-playable proxy for a session that lacks one
        (older desktop-app and CLI recordings are in the same DB but have no
        proxy).
        Runs in the background; the phone polls the recordings list."""
        folder = self.daemon.session_folder(sid)
        if folder is None:
            return self._err("not found", status=404)

        def work():
            ok = self.daemon.make_proxy_for_session(sid)
            log.info("proxy for session %d: %s", sid,
                     "ready" if ok else "failed")

        threading.Thread(target=work, daemon=True).start()
        return self._json({"ok": True, "started": True})

    def do_DELETE(self):
        self.daemon.touch()
        path = urlsplit(self.path).path
        parts = path.split("/")
        if len(parts) == 4 and parts[1:3] == ["api", "recordings"] \
                and parts[3].isdigit():
            return self._delete_recording(int(parts[3]))
        self._err("not found", status=404)

    # -- actions ---------------------------------------------------------

    def _record_start(self, body):
        if not self._authed():
            return self._err("forbidden", status=403)
        request_id = (body or {}).get("request_id")
        if not isinstance(request_id, str) or not request_id:
            request_id = None
        if request_id and not self.daemon.request_new(request_id):
            return self._json(self.daemon.request_outcome(request_id))
        ok, msg = self.daemon.intent_start()
        self.daemon.request_done(request_id, ok, msg)
        return self._json({"ok": ok, "error": msg})

    def _record_stop(self, body):
        if not self._authed():
            return self._err("forbidden", status=403)
        request_id = (body or {}).get("request_id")
        if isinstance(request_id, str) and request_id \
                and not self.daemon.request_new(request_id):
            return self._json(self.daemon.request_outcome(request_id))
        ok, msg = self.daemon.intent_stop()
        self.daemon.request_done(request_id, ok, msg)
        return self._json({"ok": ok, "error": msg})

    def _set_time(self, body):
        if not body or not isinstance(body.get("epoch_ms"), (int, float)):
            return self._err("epoch_ms required")
        ok, msg = self.daemon.set_clock(int(body["epoch_ms"]))
        return self._json({"ok": ok, "error": msg})

    def _schedule(self, body):
        if not self._authed():
            return self._err("forbidden", status=403)
        if not body or body.get("start_at") is None:
            self.daemon.set_schedule(None)
            return self._ok()
        try:
            start_at = int(body["start_at"])
            stop_after = float(body.get("stop_after") or 0)
        except (TypeError, ValueError):
            return self._err("bad schedule fields")
        if stop_after <= 0 or stop_after > 6 * 3600:
            return self._err("stop_after must be 0<s<=6h")
        self.daemon.set_schedule({"start_at": start_at, "stop_after": stop_after})
        return self._ok()

    def _delete_recording(self, sid: int):
        if not self._authed():
            return self._err("forbidden", status=403)
        ok, msg = self.daemon.delete_session(sid)
        return self._json({"ok": ok, "error": msg})


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, daemon: "CamDaemon"):
        super().__init__(addr, _Handler)
        self.daemon = daemon
        # sockets whose handler upgraded to WebSocket: shutdown_request
        # must NOT send SHUT_WR (a FIN would kill the dup'd WS socket too)
        self._ws_conns: set = set()

    def shutdown_request(self, request):
        if request in self._ws_conns:
            return
        super().shutdown_request(request)

    def handle_error(self, request, client_address):
        log.debug("http request error from %s", client_address[0])


# ---------------------------------------------------------------- daemon --

def session_master(folder: Path) -> Optional[Path]:
    """The session's stitched video: the live master, else a re-stitch."""
    for name in MASTER_NAMES:
        path = folder / name
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def _file_size(path: Optional[Path]) -> int:
    try:
        return path.stat().st_size if path else 0
    except OSError:
        return 0


class CamDaemon:
    """State machine + API. The tick loop is the single state owner."""

    def __init__(self):
        self.state = "idle"          # idle|starting|standby|recording|finalizing|error
        self.error: Optional[str] = None
        self.session: Optional[Session] = None
        self.schedule: Optional[dict] = None
        self.last_recording: Optional[dict] = None
        self.stopping = False
        self._shutdown_done = False
        self._clock_synced = False
        self._pending_start = False
        self._pending_stop = False
        self._resume = False          # a crashed recording waits to resume
        self._intent_saved_at = 0.0
        self._pending_realign = False
        self.realign: Optional[RealignJob] = None
        self.mode = "live"            # live | separate (resets on restart)
        settings = _load_settings()
        self.crop_enabled = bool(settings.get("crop"))
        qp = settings.get("stitch_qp")
        self.stitch_qp = qp if self._quality_ok(qp) else STITCH_QP
        self.fps_lock = settings.get("fps_lock", True) is not False
        # the rig is static: once a field alignment is good, a stray tap on
        # Re-align must not replace it, so re-align is locked until unlocked
        self.align_locked = settings.get("align_locked", True) is not False
        # the map set the running pipeline was started with (framing guides
        # and the status canvas must match what the preview shows)
        self.params_dir = self._settled_params_dir()
        self._cameras_busy: Optional[str] = None   # separate recorders own them
        self.restitch: Optional[RestitchJob] = None
        self._idle_sigterm = False    # we asked the pipeline down (not a crash)
        self._sigterm_sent_at = 0.0   # SIGTERM'd; SIGKILL if it doesn't exit
        self._sigterm_target = None   # WHICH process we SIGTERM'd — the
                                      # enforcement must never SIGKILL a
                                      # fresh spawn (2026-10-12: it did)
        self._starting_since = 0.0
        self._intents_lock = threading.Lock()
        self._requests: dict = {}    # request_id -> outcome (idempotency)
        self._request_order: list = []
        # intervals run on the monotonic clock: the wall clock jumps when
        # the phone or NTP sets it (hours, at the ground), and a jump must
        # never read as a stalled pipeline or an idle phone
        self._last_http = time.monotonic()
        self._touch_lock = threading.Lock()
        self._ws: set = set()
        self._ws_lock = threading.Lock()
        self._led_blink = 0
        self._btn_last = False
        self._btn_last_edge = 0.0
        self._last_soc_c = self._last_nvme_c = None
        self.started_at = time.time()
        self.sysmon = sysmon.SysMon()
        self.supervisor = StitchSupervisor(self)
        self.preview_chain = PreviewChain(self)
        self.db = RecordingDB(DB_PATH)
        self._check_ntp()

    # -- lifecycle -------------------------------------------------------

    def run(self):
        self._recover_intent()
        self.supervisor.start()
        self.preview_chain.start()
        tick = threading.Thread(target=self._tick_loop, daemon=True,
                                name="daemon-tick")
        tick.start()
        port = HTTP_PORT
        server = None
        for candidate in (port, HTTP_FALLBACK_PORT):
            try:
                server = _Server(("0.0.0.0", candidate), self)
            except OSError as e:
                log.warning("port %d unavailable: %s", candidate, e)
                continue
            port = candidate
            break
        if server is None:
            log.error("no HTTP port available — exiting")
            return
        log.info("afl-camd listening on 0.0.0.0:%d", port)
        self.port = port
        server.timeout = 0.5
        try:
            while not self.stopping:
                server.handle_request()
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self):
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self.stopping = True
        self.preview_chain.stop()
        self.supervisor.stop()
        if self.session:
            self.session.stop_audio()
        self.db.close()

    # -- http-visible helpers ---------------------------------------------

    def touch(self):
        with self._touch_lock:
            self._last_http = time.monotonic()

    def add_ws_client(self, ws: WSClient):
        with self._ws_lock:
            self._ws.add(ws)

    def remove_ws_client(self, ws: WSClient):
        with self._ws_lock:
            self._ws.discard(ws)

    def broadcast(self, payload: str):
        with self._ws_lock:
            clients = list(self._ws)
        dead = []
        for ws in clients:
            if not ws.send_text(payload):
                dead.append(ws)
        if dead:
            with self._ws_lock:
                for ws in dead:
                    self._ws.discard(ws)

    def stitch_running(self) -> bool:
        if self.supervisor._adopted:
            return True        # the foreign process owns the pipeline
        return self.supervisor.proc is not None \
            and self.supervisor.proc.poll() is None

    def pipeline_wanted(self) -> bool:
        """Should cam2stitch be running right now? False while idle with
        no clients (the phone wakes it), True otherwise — including error
        (crash retries) and any recent HTTP/WS activity."""
        if self.stopping:
            return False
        if self.state == "realigning":
            return False           # the re-align chain owns the cameras
        if self._cameras_busy:
            return False           # separate-mode recorders own them
        if self.state != "idle":
            return True
        if self._pending_start or self._resume or self._ws:
            return True
        return time.monotonic() - self._last_http < 2

    def _close_pipeline(self):
        """Close cam2stitch and wait until the cameras are free. The
        caller has already moved the state somewhere pipeline_wanted()
        says no to, so the supervisor does not respawn it."""
        proc = self.supervisor.proc
        if proc is None and not self.supervisor._adopted:
            return                  # nothing running
        self._idle_sigterm = True
        self._sigterm_sent_at = time.monotonic()
        self._sigterm_target = proc
        if proc is None:
            # adopted (foreign) pipeline: ask it over the ctl socket, then
            # wait for the socket to go away (it closes with the cameras)
            try:
                self.supervisor.command("QUIT", timeout=3)
            except Exception:
                pass
            deadline = time.time() + 10
            while time.time() < deadline and self.supervisor._probe_socket():
                time.sleep(0.2)
        else:
            # Wait here so the caller knows the cameras are free: cam2stitch
            # can deadlock in its exit path and must be SIGKILLed — the
            # tick has the same enforcement as a safety net (2026-10-11).
            proc.send_signal(15)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning("pipeline didn't exit in 5s — SIGKILL")
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        self._sigterm_sent_at = 0.0
        self._sigterm_target = None

    def request_new(self, request_id: str) -> bool:
        with self._intents_lock:
            if request_id in self._requests:
                return False
            self._requests[request_id] = None
            self._request_order.append(request_id)
            while len(self._request_order) > 64:
                self._requests.pop(self._request_order.pop(0), None)
            return True

    def request_done(self, request_id, ok, msg):
        if not request_id:
            return
        with self._intents_lock:
            self._requests[request_id] = {"ok": ok, "error": msg}

    def request_outcome(self, request_id: str) -> dict:
        with self._intents_lock:
            outcome = self._requests.get(request_id)
        if outcome is None:
            return {"ok": False, "error": "request unknown"}
        return outcome

    def intent_start(self) -> tuple[bool, str]:
        with self._intents_lock:
            if self.state == "recording":
                return False, "already recording"
            if self.state == "finalizing":
                return False, "finalizing the previous recording — retry in a moment"
            if self.state == "realigning" or self._pending_realign:
                return False, "re-aligning the cameras — wait for it to finish"
            self._pending_start = True
        # the tick loop executes; a cold pipeline takes a few seconds to
        # spawn, so wait for the state to settle
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.state == "recording":
                return True, ""
            time.sleep(0.1)
        if self.state in ("starting", "standby") and self._pending_start:
            return False, "pipeline still coming up — retry in a moment"
        return False, self.error or "start failed — see board log"

    def set_mode(self, mode) -> tuple[bool, str]:
        if mode not in MODES:
            return False, f"mode must be one of {', '.join(MODES)}"
        with self._intents_lock:
            if self.session is not None or self.state in (
                    "recording", "finalizing") or self._pending_start:
                return False, "can't change mode while recording"
            self.mode = mode
        log.info("mode: %s", mode)
        return True, ""

    # -- crop setting ------------------------------------------------------

    def _settled_params_dir(self) -> Path:
        """Map set for the current setting without building anything: the
        crop set only when it exists and matches the live maps."""
        if self.crop_enabled:
            if cropsig.current(STITCH_PARAMS_DIR, CROP_PARAMS_DIR) is not None:
                return CROP_PARAMS_DIR
        return STITCH_PARAMS_DIR

    def choose_params_dir(self) -> Path:
        """Map set cam2stitch starts with. Builds (or reuses) the crop set
        when the setting is on, and falls back to the full canvas when that
        fails so a bad crop never costs a recording."""
        if not self.crop_enabled:
            self.params_dir = STITCH_PARAMS_DIR
            return self.params_dir
        try:
            run = subprocess.run(
                [sys.executable, "-m", "afl_cam.crop_maps",
                 str(STITCH_PARAMS_DIR), str(CROP_PARAMS_DIR)],
                capture_output=True, text=True, timeout=CROP_TIMEOUT_S, cwd=str(ROOT_DIR))
            if run.returncode == 0:
                self.params_dir = CROP_PARAMS_DIR
                log.info("crop: %s", run.stdout.strip())
                return self.params_dir
            log.warning("crop: failed (%s) — using the full canvas",
                        (run.stderr or run.stdout).strip()[-200:])
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("crop: failed (%s) — using the full canvas", e)
        self.params_dir = STITCH_PARAMS_DIR
        return self.params_dir

    def crop_status(self) -> dict:
        guides = framing.framing(self.params_dir)
        return {"enabled": self.crop_enabled,
                "active": self.params_dir == CROP_PARAMS_DIR,
                "canvas": guides["canvas"] if guides else None}

    def set_crop(self, enabled) -> tuple[bool, str]:
        if not isinstance(enabled, bool):
            return False, "enabled must be true or false"
        with self._intents_lock:
            if self.session is not None or self.state in (
                    "recording", "finalizing") or self._pending_start:
                return False, "can't change crop while recording"
            if enabled == self.crop_enabled:
                return True, ""
            self.crop_enabled = enabled
            settings = _load_settings()
            settings["crop"] = enabled
            _save_settings(settings)
            # a running pipeline restarts on the other map set (the phone's
            # WebSocket brings it straight back up); the encoder and the
            # preview take their size from the maps at start
            self._restart_idle_pipeline("crop %s" % ("on" if enabled else "off"),
                                        new_size=True)
        log.info("crop: %s", "on" if enabled else "off")
        return True, ""

    def _restart_idle_pipeline(self, why: str, new_size: bool = False) -> None:
        """Restart a standby pipeline so it picks up a changed setting (the
        phone's WebSocket brings it straight back up). Call with
        _intents_lock held; a stopped pipeline just starts with it later."""
        if self.state != "standby" or (
                self.supervisor.proc is None and not self.supervisor._adopted):
            return
        log.info("%s — restarting the pipeline", why)
        self._idle_sigterm = True
        self._set_state("idle")
        self._sigterm_sent_at = time.monotonic()
        self._sigterm_target = self.supervisor.proc
        if self.supervisor.proc is not None:
            self.supervisor.proc.send_signal(15)
        else:
            try:
                self.supervisor.command("QUIT", timeout=3)
            except Exception:
                pass
        if new_size:
            # the preview changes size: a running ffmpeg pusher cannot
            # follow a new resolution mid-stream (it would drop out later,
            # taking the HLS/WebRTC stream with it), so start it afresh
            self.preview_chain.stop()

    # -- quality setting ---------------------------------------------------

    @staticmethod
    def _quality_ok(qp) -> bool:
        return isinstance(qp, int) and not isinstance(qp, bool) \
            and any(qp == p for p, _ in STITCH_QUALITY_PRESETS)

    def quality_status(self) -> dict:
        # GB/hour = Mbps * 3600 / 8 / 1000
        opts = [{"qp": qp, "mbps": mbps, "gb_per_hour": round(mbps * 0.45)}
                for qp, mbps in STITCH_QUALITY_PRESETS]
        if not any(o["qp"] == self.stitch_qp for o in opts):   # env override
            opts.append({"qp": self.stitch_qp, "mbps": None, "gb_per_hour": None})
        return {"qp": self.stitch_qp, "options": opts}

    def set_quality(self, qp) -> tuple[bool, str]:
        if not self._quality_ok(qp):
            return False, "unknown quality"
        with self._intents_lock:
            if self.session is not None or self.state in (
                    "recording", "finalizing") or self._pending_start:
                return False, "can't change quality while recording"
            if qp == self.stitch_qp:
                return True, ""
            self.stitch_qp = qp
            settings = _load_settings()
            settings["stitch_qp"] = qp
            _save_settings(settings)
            # the encoder takes its rate control at start
            self._restart_idle_pipeline("quality QP %d" % qp)
        log.info("quality: QP %d", qp)
        return True, ""

    def set_fps_lock(self, locked) -> tuple[bool, str]:
        if not isinstance(locked, bool):
            return False, "locked must be true or false"
        with self._intents_lock:
            if self.session is not None or self.state in (
                    "recording", "finalizing") or self._pending_start:
                return False, "can't change this while recording"
            if locked == self.fps_lock:
                return True, ""
            self.fps_lock = locked
            settings = _load_settings()
            settings["fps_lock"] = locked
            _save_settings(settings)
            # exposure limits are read when the pipeline starts
            self._restart_idle_pipeline("30 fps lock %s" % ("on" if locked else "off"))
        log.info("30 fps lock: %s", "on" if locked else "off")
        return True, ""

    def start_restitch(self, session_id: int) -> tuple[bool, str]:
        if self.restitch is not None and self.restitch.is_alive():
            return False, "a re-stitch is already running"
        if self.session is not None:
            return False, "stop the recording before re-stitching"
        folder = self.session_folder(session_id)
        if folder is None:
            return False, "no such recording"
        if not ((folder / "cam0.mp4").is_file() and (folder / "cam1.mp4").is_file()):
            return False, "not a separate-mode recording (needs cam0 + cam1)"
        self.restitch = RestitchJob(session_id)
        self.restitch.start()
        return True, ""

    def live_snapshot(self, cam: str) -> Optional[Path]:
        """Latest 4K snapshot a separate-mode recorder wrote."""
        s = self.session
        if s is None or s.kind != "separate":
            return None
        path = s.dir / f"{cam}_preview.jpg"
        return path if path.is_file() else None

    def intent_realign(self) -> tuple[bool, str]:
        """Queue a re-align; the tick loop closes the pipeline and starts
        it. Progress is in the snapshot's "realign" block."""
        with self._intents_lock:
            if self.align_locked:
                return False, "alignment is locked: unlock it in Setup to re-align"
            if self.state == "realigning" or self._pending_realign:
                return False, "already re-aligning"
            if self.state in ("recording", "finalizing") or self.session:
                return False, "stop the recording before re-aligning"
            self._pending_realign = True
        return True, ""

    def set_align_locked(self, locked) -> tuple[bool, str]:
        if not isinstance(locked, bool):
            return False, "locked must be true or false"
        with self._intents_lock:
            self.align_locked = locked
            settings = _load_settings()
            settings["align_locked"] = locked
            _save_settings(settings)
        log.info("alignment: %s", "locked" if locked else "unlocked")
        return True, ""

    def realign_status(self, full: bool = False) -> dict:
        job = self.realign
        if job is None:
            return {"running": self._pending_realign, "ok": None,
                    "locked": self.align_locked}
        out = job.summary()
        out["locked"] = self.align_locked
        out["running"] = out["running"] or self._pending_realign
        if full:
            out["log"] = job.lines()
        return out

    def on_realign_done(self, job: "RealignJob"):
        # the tick loop moves realigning -> idle once the thread is gone
        log.info("realign finished: %s", "ok" if job.ok else job.error)

    def selftest(self) -> dict:
        soc, nvme = self._temperatures()
        guides = framing.framing(self.params_dir)
        checks = [
            selftest.cameras(_discover_cameras() or {},
                             self.supervisor.get_status()),
            selftest.alignment(guides and guides.get("aligned_at")),
            selftest.storage(self._storage_minutes()),
            selftest.temperature(soc, nvme, TEMP_STOP_C),
            selftest.clock(self._clock_synced),
        ]
        return {"level": selftest.overall(checks), "checks": checks}

    def intent_stop(self) -> tuple[bool, str]:
        with self._intents_lock:
            if self.state != "recording" and not self._resume:
                return False, "not recording"
            self._pending_stop = True
        deadline = time.time() + 8
        while time.time() < deadline:
            with self._intents_lock:
                if not self._pending_stop:
                    return True, ""
            time.sleep(0.05)
        with self._intents_lock:
            self._pending_stop = False
        return False, "stop timed out"

    # -- tick loop (the only place state changes) -------------------------

    def _tick_loop(self):
        last_ws_push = 0.0
        while not self.stopping:
            st = self.supervisor.get_status()
            now = time.time()          # wall clock: schedule, intent stamps
            mono = time.monotonic()    # intervals (see _last_http)

            # idle → starting (phone arrived or button press)
            if self.state in ("idle", "error") and \
                    (self._pending_start or self._resume or
                     mono - self._last_http < 2 or self._ws):
                self._set_state("starting", error=(
                    "recording interrupted — resuming" if self._resume else None))

            if self.state == "starting":
                # adopted pipelines have proc=None — the socket connection
                # and the pushed status are the liveness proof, not proc
                if st is not None and (
                        self.supervisor.proc is not None
                        or self.supervisor._adopted):
                    if st.get("state") == "recording":
                        # pipeline was already recording (daemon restarted
                        # while cam2stitch kept running)
                        self._resume = False
                        self._set_state("recording")
                        self._attach_recording(st)
                    else:
                        self._set_state("standby")
                elif self.supervisor.proc is not None \
                        and self.supervisor.proc.poll() is not None:
                    self._set_state("error", error=(
                        "pipeline failed to start — cameras busy or "
                        "init failed; check the board"))

            # watchdogs: a stuck pipeline must never hold the cameras
            # indefinitely (2026-10-11: a wedged encoder stalled the main
            # loop, status stopped, and the daemon sat in "starting" for
            # 36 hours with the cameras held)
            if self.state == "starting" \
                    and mono - self._starting_since > 30:
                log.error("watchdog: stuck in 'starting' — restarting "
                          "the pipeline")
                self.supervisor.kill_pipeline()
                self._starting_since = mono
            elif self.state in ("standby", "recording", "finalizing") \
                    and not self._cameras_busy \
                    and not (self.session and self.session.kind == "separate") \
                    and self.supervisor.status_ts \
                    and mono - self.supervisor.status_ts > 10:
                log.error("watchdog: stitch status stalled %.0fs — "
                          "restarting the pipeline",
                          mono - self.supervisor.status_ts)
                self.supervisor.kill_pipeline()
                if self.session is not None:
                    # resume into the next segment (per-packet flushes keep
                    # what was written)
                    self.on_stitch_exit()

            # separate mode: the recorders take the cameras from the
            # pipeline (no need to wait for standby)
            if self._pending_start and self.mode == "separate" \
                    and self.session is None and not self._pending_realign \
                    and self.state in ("idle", "starting", "standby", "error"):
                self._execute_start_separate()
            if self.state == "recording" and self.session is not None \
                    and self.session.kind == "separate":
                dead = self.session.recorder.dead()
                if dead:
                    log.error("separate: %s recorder died — finalizing", dead)
                    self.error = f"{dead} recorder stopped unexpectedly"
                    self._execute_stop()

            # a stop while a crashed recording waits to resume: finalize
            # the segments already on disk
            if self._pending_stop and self._resume:
                self._abandon_resume()

            # crashed recording: back into the same session once the
            # pipeline is up again
            if self._resume and self.state == "standby":
                self._execute_resume()
            # re-align: close the pipeline, hand the cameras to the chain
            if self._pending_realign and self.session is None \
                    and self.state in ("idle", "starting", "standby", "error"):
                self._begin_realign()
            elif self.state == "realigning" and self.realign is not None \
                    and not self.realign.is_alive():
                self._set_state("idle")    # next wake reopens the pipeline

            # pending start
            if self._pending_start and self.state == "standby":
                self._execute_start()

            # pending stop
            if self._pending_stop and self.state == "recording":
                self._execute_stop()

            # heartbeat the record intent (a reboot long after the last
            # one finalizes instead of resuming)
            if self.state == "recording" and self.session is not None \
                    and mono - self._intent_saved_at > 5:
                self._save_intent()

            # schedule
            if self.schedule and self.state == "standby":
                sched = self.schedule
                if now * 1000 >= sched["start_at"] - 1000:
                    if now * 1000 - sched["start_at"] < 120000:
                        log.info("schedule: starting (%s)",
                                 datetime.now().strftime("%H:%M:%S"))
                        self._pending_start = True
                        self._execute_start()
                    else:
                        log.warning("schedule: start time missed — cleared")
                        self.schedule = None
            elif self.schedule and self.state == "recording" \
                    and self.session is not None:
                if self.session.elapsed() >= self.schedule["stop_after"]:
                    log.info("schedule: stopping after %.0fs",
                             self.schedule["stop_after"])
                    self.schedule = None
                    self._execute_stop()

            # auto-stop: temperature / storage (finalize cleanly rather
            # than leave a broken file)
            if self.state == "recording":
                reason = self._autostop_reason()
                if reason:
                    self.error = reason
                    log.error("auto-stop: %s", reason)
                    self._execute_stop()

            # idle timeout: close the cameras when nobody is around
            if self.state in ("standby",) and not self.session and \
                    mono - self._last_http > IDLE_STOP_SECS and not self._ws:
                log.info("idle %.0fs — closing the pipeline",
                         IDLE_STOP_SECS)
                self._idle_sigterm = True
                self._set_state("idle")
                self._sigterm_sent_at = mono
                self._sigterm_target = self.supervisor.proc
                if self.supervisor.proc is not None:
                    self.supervisor.proc.send_signal(15)

            # enforce pipeline death: cam2stitch can deadlock in its exit
            # path (all threads in futex waits after closing the cameras —
            # seen 2026-10-11). Cameras are already free at that point, so
            # a SIGKILL is clean. The target check is critical: without it
            # the enforcement kills a FRESH pipeline spawned after the hold
            # (2026-10-12: every handover was followed by rc=-9 churn).
            if self._sigterm_target is not None \
                    and self._sigterm_target is self.supervisor.proc \
                    and self.supervisor.proc.poll() is None \
                    and mono - self._sigterm_sent_at > 5:
                log.warning("pipeline didn't exit %.0fs after SIGTERM — "
                            "SIGKILL", mono - self._sigterm_sent_at)
                self.supervisor.proc.kill()
                self._sigterm_sent_at = 0.0
                self._sigterm_target = None

            # hardware button (GPIO, optional): toggle record
            self._poll_button()

            # ws pushes ~1/s + on state change
            if now - last_ws_push >= 1.0:
                last_ws_push = now
                if self._ws:
                    self.broadcast(json.dumps(self.snapshot()))

            # LED
            self._update_led()

            time.sleep(0.1)
        log.info("tick loop stopped")

    def _begin_realign(self):
        self._pending_realign = False
        self._set_state("realigning")
        self._close_pipeline()
        self.realign = RealignJob(self)
        self.realign.start()

    def _set_state(self, state: str, error: Optional[str] = None):
        if self.state != state:
            log.info("state: %s -> %s%s", self.state, state,
                     f" ({error})" if error else "")
            if state == "starting":
                self._starting_since = time.monotonic()
        if error is not None:
            self.error = error
        elif self.state == "error" and state not in ("error", "finalizing"):
            self.error = None
        self.state = state
        if self._ws:
            self.broadcast(json.dumps(self.snapshot()))

    def _execute_start(self):
        if self.mode == "separate":
            return self._execute_start_separate()
        self._pending_start = False
        self._idle_sigterm = False
        self.error = None
        st = self.supervisor.get_status()
        if st is not None and st.get("state") == "recording":
            # re-attach: cam2stitch was already recording (daemon restarted)
            self.state = "recording"
            self._attach_recording(st)
            return
        devs = _rediscover_with_kick()
        db = self.db
        dir_ = new_session_dir()
        try:
            session_id = db.add_session(
                created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                folder=str(dir_.relative_to(ROOT_DIR)))
            row_id = db.add(
                camera="stitch",
                device_path=f"{devs.get('cam0', '?')}+{devs.get('cam1', '?')}",
                resolution="live stitch",
                file_path=str((dir_ / "stitch.mp4").relative_to(ROOT_DIR)),
                status="recording", session_id=session_id,
                created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                codec="HEVC" if HEVC else "H264", container="mp4")
        except Exception as e:
            log.error("session setup failed: %s", e)
            self._set_state("error", error=f"session setup failed: {e}")
            return
        audio = self._start_audio(dir_)
        try:
            reply = self.supervisor.command(f"REC_START {dir_}")
        except RuntimeError as e:
            reply = f"ERR {e}"
        if not reply.startswith("OK"):
            log.error("REC_START refused: %s", reply)
            for proc in audio:
                proc.terminate()
            db.update_error(row_id, f"REC_START refused: {reply}")
            self._set_state("error", error=f"record start refused: {reply}")
            return
        self.session = Session(dir_, session_id, row_id, audio)
        self._save_intent()
        self._set_state("recording")
        log.info("recording: session %d -> %s", session_id, dir_.name)

    # -- crash resume (afl_cam/resume.py) ---------------------------------

    def _save_intent(self):
        # only the live stitch can resume; a separate-mode session that
        # dies is finalized from what its recorders wrote
        if self.session is None or self.session.kind != "live":
            return
        try:
            resume.save_intent(INTENT_PATH, self.session.intent())
            self._intent_saved_at = time.monotonic()
        except OSError as e:
            log.error("record intent not saved: %s", e)

    def _recover_intent(self):
        """Boot / daemon start: a record intent left behind means the last
        recording never stopped. Resume it (fresh enough) or finalize it.
        No intent but a live recording the DB still calls 'recording': the
        recording was stopped and its finalize never finished (the daemon
        was restarted mid-mux, 2026-10-14) — finalize it again."""
        intent = resume.load_intent(INTENT_PATH)
        unfinished = False
        if intent is None:
            if resume.intent_unreadable(INTENT_PATH):
                intent = self._intent_from_db()
            else:
                intent = self._intent_from_db(unreadable=False)
                unfinished = intent is not None
        if intent is None:
            return
        try:
            session = Session.from_intent(intent)
        except (KeyError, TypeError, ValueError) as e:
            log.error("record intent unreadable (%s) — cleared", e)
            resume.clear_intent(INTENT_PATH)
            return
        if not session_dir_is_safe(session.dir) or not session.dir.is_dir():
            log.error("record intent points at %s — cleared", session.dir)
            resume.clear_intent(INTENT_PATH)
            if unfinished:
                self.db.update_error(session.recording_id,
                                     "session folder missing")
            return
        alive_at = float(intent.get("alive_at") or 0.0)
        seg_start = float(intent.get("seg_start") or alive_at)
        session.elapsed_before += max(0.0, alive_at - seg_start)
        session.lost_at = alive_at
        if unfinished or time.time() - alive_at > RESUME_MAX_GAP_S:
            if unfinished:
                log.warning("recording %s was stopped but never finalized "
                            "— finalizing it", session.dir.name)
            else:
                log.warning("record intent for %s is %.0f s old — "
                            "finalizing instead of resuming",
                            session.dir.name, time.time() - alive_at)
            resume.clear_intent(INTENT_PATH)
            session.duration = session.elapsed_before
            # visible as finalizing, so a deploy or rig test waits for it
            self._set_state("finalizing")
            Finalizer(self, session).start()
            return
        log.warning("recording %s never stopped — resuming it",
                    session.dir.name)
        self.session = session
        self._resume = True
        self.error = "recording interrupted — resuming"

    def _intent_from_db(self, unreadable: bool = True) -> Optional[dict]:
        """The recordings DB's live recording still marked 'recording', as
        an intent: for an intent file a power cut left unreadable, or (no
        intent file) a recording whose finalize never finished."""
        try:
            row = self.db.live_recording_in_progress()
        except Exception as e:
            log.error("recording lookup in the DB failed: %s", e)
            return None
        if not row or not row.get("folder"):
            if unreadable:
                log.error("record intent unreadable and no recording in "
                          "progress in the DB — cleared")
                resume.clear_intent(INTENT_PATH)
            return None
        dir_ = Path(row["folder"])
        if not dir_.is_absolute():
            dir_ = ROOT_DIR / dir_
        if unreadable:
            log.warning("record intent unreadable (power cut mid-write?) — "
                        "recovering recording %d from the DB",
                        row["recording_id"])
        return resume.intent_from_files(dir_, row["session_id"],
                                        row["recording_id"])

    def _segment_lost(self):
        """The live segment died (pipeline crash / watchdog kill): keep the
        session and resume into the next segment once the pipeline is back."""
        s = self.session
        s.elapsed_before = s.elapsed()
        s.lost_at = s.start = time.time()   # elapsed_before holds it all now
        s.stop_audio()
        s.audio = []
        self._resume = True
        self._save_intent()
        log.error("stitch pipeline died mid-recording — resuming into the "
                  "next segment of session %d", s.session_id)
        self._set_state("starting",
                        error="pipeline restarted — resuming the recording")

    def _execute_resume(self):
        s = self.session
        if s is None:
            self._resume = False
            return
        seg = resume.seal_segment(s.dir)
        audio = self._start_audio(s.dir)
        try:
            reply = self.supervisor.command(f"REC_START {s.dir}")
        except RuntimeError as e:
            reply = f"ERR {e}"
        if not reply.startswith("OK"):
            for proc in audio:
                proc.terminate()
            log.error("resume REC_START refused: %s", reply)
            self._set_state("error", error=f"resume refused: {reply} — retrying")
            return
        now = time.time()
        s.restarts += 1
        gap = round(now - s.lost_at, 1) if s.lost_at else None
        s.gaps.append({"at_s": round(s.elapsed_before, 1), "gap_s": gap})
        s.start = now
        s.audio = audio
        self._resume = False
        self._pending_start = False     # a RECORD press while resuming
        self._save_intent()
        self.error = None
        self._set_state("recording")
        log.warning("recording resumed: session %d, restart %d, gap %s s "
                    "(sealed segment %s)", s.session_id, s.restarts, gap, seg)

    def _abandon_resume(self):
        """Stop pressed while waiting to resume: finalize what's on disk."""
        self._pending_stop = False
        self._resume = False
        resume.clear_intent(INTENT_PATH)
        session = self.session
        self.session = None
        if session is None:
            return
        session.duration = session.elapsed_before
        self._set_state("finalizing", error=None)
        Finalizer(self, session).start()

    def _execute_start_separate(self):
        """Separate mode: close the pipeline, then one cam2h264 per camera
        into a new session folder (cam0/cam1.raw.h264 + .pts)."""
        self._pending_start = False
        self.error = None
        self._cameras_busy = "separate"
        self._close_pipeline()
        devs = _rediscover_with_kick()
        if not devs.get("cam0") or not devs.get("cam1"):
            self._cameras_busy = None
            self._set_state("error", error="separate mode needs both cameras")
            return
        dir_ = new_session_dir()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            session_id = self.db.add_session(
                created_at=now, folder=str(dir_.relative_to(ROOT_DIR)))
            row_ids = {cam: self.db.add(
                camera=cam, device_path=devs[cam], resolution="4K separate",
                file_path=str((dir_ / f"{cam}.mp4").relative_to(ROOT_DIR)),
                status="recording", session_id=session_id, created_at=now,
                codec="HEVC" if HEVC else "H264", container="mp4")
                for cam in ("cam0", "cam1")}
        except Exception as e:
            log.error("session setup failed: %s", e)
            self._cameras_busy = None
            self._set_state("error", error=f"session setup failed: {e}")
            return
        recorder = SeparateRecorder(dir_, devs)
        err = recorder.start()
        if err:
            log.error("separate: %s", err)
            for row_id in row_ids.values():
                self.db.update_error(row_id, err)
            self._cameras_busy = None
            self._set_state("error", error=err)
            return
        audio = self._start_audio(dir_)
        self.session = Session(dir_, session_id, row_ids["cam0"], audio,
                               kind="separate", recorder=recorder,
                               recording_ids=row_ids)
        self._set_state("recording")
        log.info("recording (separate): session %d -> %s",
                 session_id, dir_.name)

    def _start_audio(self, dir_: Path) -> list:
        """Camera mics (clap fallback / audio track). Each wav is stamped
        with arecord's start (kernel monotonic us — the clock the `.pts`
        sidecars use) so finalize can line the mic up with the video."""
        audio = []
        for cam in ("cam0", "cam1"):
            dev = AUDIO_DEVICES.get(cam)
            if not dev:
                continue
            wav = dir_ / f"{cam}.wav"
            try:
                proc = subprocess.Popen(
                    ["arecord", "-q", "-D", dev, "-f", "S16_LE",
                     "-c", "2", "-r", "44100", str(wav)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                continue
            audio.append(proc)
            # stamped after the fork (arecord opens the device after it
            # returns; that latency is the offset's residual error). Losing
            # the stamp costs alignment, never the recording, so it must
            # not raise into the caller.
            try:
                resume.audio_marker(wav).write_text(
                    f"{time.monotonic_ns() // 1000}\n")
            except OSError as e:
                log.warning("audio: cannot stamp %s start: %s", wav.name, e)
        return audio

    def _attach_recording(self, st: dict):
        """Daemon restarted while cam2stitch was recording: rebuild the
        session bookkeeping from the status (folder = rec_dir)."""
        rec_dir = st.get("rec_dir") or ""
        dir_ = Path(rec_dir) if rec_dir else None
        if dir_ is None or not session_dir_is_safe(dir_):
            log.error("cannot re-attach to rec_dir %r", rec_dir)
            self._set_state("error", error="re-attach failed (bad rec_dir)")
            return
        try:
            db = self.db
            sessions = [s for s in db.list_sessions(limit=10)
                        if s.get("folder") == str(dir_.relative_to(ROOT_DIR))]
            session_id = sessions[0]["id"] if sessions else \
                db.add_session(
                    created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    folder=str(dir_.relative_to(ROOT_DIR)))
            recs = db.session_recordings(session_id)
            row_id = recs[0]["id"] if recs else db.add(
                camera="stitch", device_path="re-attached",
                resolution="live stitch",
                file_path=str((dir_ / "stitch.mp4").relative_to(ROOT_DIR)),
                status="recording", session_id=session_id,
                codec="HEVC" if HEVC else "H264", container="mp4")
        except Exception as e:
            log.error("re-attach bookkeeping failed: %s", e)
            self._set_state("error", error=str(e))
            return
        prev = self.session
        self.session = Session(dir_, session_id, row_id, [])
        if prev is not None and prev.dir == dir_:
            # the record intent's counters (resumed before this attach)
            self.session.elapsed_before = prev.elapsed_before
            self.session.restarts = prev.restarts
            self.session.gaps = prev.gaps
        self._save_intent()
        log.info("re-attached to session %d (%s)", session_id, dir_.name)

    def _execute_stop(self):
        self._pending_stop = False
        if self.state != "recording" or self.session is None:
            return
        s = self.session
        if s.kind == "separate":
            s.duration = time.time() - s.start
            s.recorder.stop()
            s.stop_audio()
            self.session = None
            self._set_state("finalizing")
            Finalizer(self, s).start()
            return
        resume.clear_intent(INTENT_PATH)   # an explicit stop ends it
        st = self.supervisor.get_status()
        s.duration = s.elapsed(float(st["rec_elapsed_s"])
                               if st and st.get("rec_elapsed_s") else None)
        try:
            self.supervisor.command("REC_STOP")
        except RuntimeError as e:
            log.error("REC_STOP failed: %s", e)
        s.stop_audio()
        # wait for cam2stitch to confirm the files are closed (the stop
        # drains the encoder tail — a few frame intervals at low light)
        deadline = time.time() + 15
        while time.time() < deadline:
            st = self.supervisor.get_status()
            if st is None or st.get("state") != "recording":
                break
            time.sleep(0.1)
        session = self.session
        self.session = None
        self._set_state("finalizing")
        Finalizer(self, session).start()

    def on_finalized(self, session: Session, master_ok: bool, proxy_ok: bool):
        self.last_recording = {
            "session_id": session.session_id,
            "ok": master_ok,
            "has_proxy": proxy_ok,
            "duration_s": session.duration,
            "thumb": f"/api/recordings/{session.session_id}/thumb.jpg"
            if proxy_ok else None,
        }
        if session.kind == "separate":
            self._cameras_busy = None
            if self.state == "finalizing":
                self._set_state("idle")    # the next wake reopens the pipeline
        elif self.state == "finalizing":
            # a resume abandoned before the pipeline came back: bring it up
            self._set_state("standby" if self.stitch_running() else "starting")

    def on_stitch_exit(self):
        """cam2stitch died — either our idle-stop (expected) or a crash.
        On a crash mid-recording the record intent survives: the session
        resumes into its next segment once the pipeline is back (the files
        are flushed per packet, so what's on disk is a valid segment)."""
        if self._idle_sigterm:
            self._idle_sigterm = False
            return                      # deliberate shutdown — stay idle
        if self.state == "recording" and self.session is not None \
                and self.session.kind == "live":
            self._segment_lost()
        elif self._resume:
            return                      # already waiting to resume
        elif self.state != "idle":
            self._set_state("error",
                            error="stitch pipeline crashed — retrying")

    # -- sensors & limits --------------------------------------------------

    def _autostop_reason(self) -> Optional[str]:
        soc, nvme = self._temperatures()
        if soc is not None and soc >= TEMP_STOP_C:
            return f"temperature {soc:.0f}°C — stopped to protect the board"
        if nvme is not None and nvme >= TEMP_STOP_C:
            return f"NVMe {nvme:.0f}°C — stopped to protect the disk"
        du = shutil.disk_usage(OUTPUTS_DIR)
        if du.free < STORAGE_STOP_GB * 1e9:
            return (f"only {du.free / 1e9:.1f} GB free — "
                    "stopped to keep the disk alive")
        return None

    @staticmethod
    def _temperatures() -> tuple[Optional[float], Optional[float]]:
        soc = nvme = None
        try:
            for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
                if (zone / "type").read_text().strip() == "soc-thermal":
                    soc = int((zone / "temp").read_text()) / 1000.0
        except OSError:
            pass
        try:
            for hw in Path("/sys/class/hwmon").glob("hwmon*"):
                if (hw / "name").read_text().strip() == "nvme" \
                        and (hw / "temp1_input").exists():
                    nvme = int((hw / "temp1_input").read_text()) / 1000.0
        except OSError:
            pass
        return soc, nvme

    def _storage_minutes(self) -> Optional[float]:
        du = shutil.disk_usage(OUTPUTS_DIR)
        # measured rate while recording (CBR scales with the real fps),
        # else the nominal sum: master + two-camera budget + proxy
        st = self.supervisor.get_status()
        if st and st.get("rec_elapsed_s") and st.get("rec_bytes"):
            rate_bps = st["rec_bytes"] * 8 / max(1.0, st["rec_elapsed_s"])
        else:
            # fixed QP has no nominal rate: budget its busy-scene rate
            nominal_kbps = next((mbps * 1000 for qp, mbps in STITCH_QUALITY_PRESETS
                                 if qp == self.stitch_qp), 220000)
            rate_bps = nominal_kbps * 1000 * 2.5  # + proxy slack
        return du.free * 8 / max(1.0, rate_bps) / 60

    # -- clock ------------------------------------------------------------

    def _check_ntp(self):
        try:
            result = subprocess.run(
                ["timedatectl", "show", "-p", "NTPSynchronized",
                 "--value"], capture_output=True, text=True, timeout=5)
            self._clock_synced = result.stdout.strip() == "yes"
        except (OSError, subprocess.SubprocessError):
            self._clock_synced = False
        log.info("clock: %s",
                 "NTP-synced" if self._clock_synced else "unsynced")

    def set_clock(self, epoch_ms: int) -> tuple[bool, str]:
        """Set the board clock from the phone (offline AP = no NTP), but
        only when it has not synced since boot. Needs CAP_SYS_TIME, granted
        by the afl-camd.service AmbientCapabilities."""
        if self._clock_synced:
            return True, "already synced"
        stamp = datetime.fromtimestamp(epoch_ms / 1000)
        log.info("clock: setting from phone: %s", stamp)
        try:
            time.clock_settime(time.CLOCK_REALTIME, epoch_ms / 1000)
        except OSError as e:
            return False, f"clock_settime failed: {e}"
        self._clock_synced = True
        return True, ""

    # -- schedule ----------------------------------------------------------

    def set_schedule(self, sched: Optional[dict]):
        self.schedule = sched
        log.info("schedule: %s", sched or "cleared")

    # -- recordings --------------------------------------------------------

    def session_folder(self, session_id: int) -> Optional[Path]:
        try:
            row = self.db.get_session(session_id)
        except Exception:
            return None
        if not row or not row.get("folder"):
            return None
        folder = Path(row["folder"])
        if not folder.is_absolute():
            folder = ROOT_DIR / folder
        return folder if session_dir_is_safe(folder) else None

    def make_proxy_for_session(self, session_id: int) -> bool:
        """Ensure <folder>/proxy.mp4 exists (iPhone-playable): remux the
        stitched master, or fall back to the first camera raw + pts.
        Older desktop-app and CLI recordings never got a proxy."""
        folder = self.session_folder(session_id)
        if folder is None:
            return False
        proxy = folder / "proxy.mp4"
        if proxy.is_file() and proxy.stat().st_size > 0:
            return True
        stitch = folder / "stitch.mp4"
        if stitch.is_file():
            ok, err = _run_ffmpeg(["-i", str(stitch), "-c", "copy",
                                   "-movflags", "+faststart", str(proxy)])
            if ok:
                return True
            log.error("proxy: stitch remux failed: %s", err)
        for raw in sorted(folder.glob("*.raw.h264")) \
                + sorted(folder.glob("*.raw.hevc")):
            pts = raw.with_suffix("").with_suffix(".pts")
            if not pts.is_file():
                continue
            fps = _pts_median_fps(pts, 15.0)
            args = ["-f", "hevc" if raw.suffix == ".hevc" else "h264",
                    "-r", f"{fps:.4f}", "-i", str(raw), "-c", "copy"]
            if raw.suffix == ".hevc":
                args += ["-tag:v", "hvc1"]
            ok, err = _run_ffmpeg(args + ["-movflags", "+faststart",
                                          str(proxy)])
            if ok:
                return True
            log.error("proxy: %s mux failed: %s", raw.name, err)
        return False

    def recordings(self) -> list[dict]:
        out = []
        try:
            for s in self.db.list_sessions(limit=200):
                folder = self.session_folder(s["id"])
                proxy = folder / "proxy.mp4" if folder else None
                master = session_master(folder) if folder else None
                separate = bool(folder and (folder / "cam0.mp4").is_file()
                                and (folder / "cam1.mp4").is_file())
                out.append({
                    "id": s["id"],
                    "created_at": s.get("created_at"),
                    "duration_s": s.get("total_duration"),
                    "size_bytes": s.get("total_bytes") or
                    (master.stat().st_size if master and master.exists()
                     else 0),
                    "has_proxy": bool(proxy and proxy.is_file()
                                      and proxy.stat().st_size > 0),
                    "has_master": master is not None,
                    "proxy_bytes": _file_size(proxy),
                    "master_bytes": _file_size(master),
                    "kind": "separate" if separate else "live",
                    "thumb": f"/api/recordings/{s['id']}/thumb.jpg"
                    if proxy and (folder / "thumb.jpg").is_file() else None,
                    "stitch_status": s.get("stitch_status"),
                    "resumed": _segments_report(folder),
                    "error": bool(s.get("has_error")),
                    "recording": self.session is not None
                    and self.session.session_id == s["id"],
                })
        except Exception as e:
            log.error("recordings list failed: %s", e)
        return out

    def delete_session(self, session_id: int) -> tuple[bool, str]:
        if self.session is not None and self.session.session_id == session_id:
            return False, "that recording is still running"
        try:
            row = self.db.get_session(session_id)
        except Exception as e:
            return False, str(e)
        if not row:
            return False, "no such session"
        folder = self.session_folder(session_id)
        if folder is not None and folder.exists():
            remove_session_dir(folder)
        try:
            self.db.delete_session(session_id)
        except Exception as e:
            return False, f"db delete failed: {e}"
        if self.last_recording and self.last_recording.get("session_id") == session_id:
            self.last_recording = None
        log.info("deleted session %d", session_id)
        return True, ""

    # -- gpio --------------------------------------------------------------

    def _gpio(self, num: str, direction: str, value: Optional[str] = None) -> Optional[str]:
        base = Path("/sys/class/gpio") / f"gpio{num}"
        try:
            if not base.exists():
                (Path("/sys/class/gpio") / "export").write_text(num)
                for _ in range(20):
                    if base.exists():
                        break
                    time.sleep(0.05)
            if not base.exists():
                return None
            (base / "direction").write_text(direction)
            if value is not None:
                (base / "value").write_text(value)
            return (base / "value").read_text().strip()
        except OSError:
            return None

    def _update_led(self):
        if not GPIO_LED:
            return
        if self.state == "recording":
            self._gpio(GPIO_LED, "out", "1")
        elif self.state == "error":
            self._led_blink = 1 - self._led_blink
            self._gpio(GPIO_LED, "out", str(self._led_blink))
        else:
            self._gpio(GPIO_LED, "out", "0")

    def _poll_button(self):
        """GPIO record button (optional, AFL_CAM_GPIO_BTN): toggle record
        on a press edge, 300 ms debounce. Pressed = line low (button to
        GND; AFL_CAM_GPIO_ACTIVE_HIGH=1 inverts)."""
        if not GPIO_BTN:
            return
        value = self._gpio(GPIO_BTN, "in")
        if value is None:
            return
        active_high = os.environ.get("AFL_CAM_GPIO_ACTIVE_HIGH") == "1"
        pressed = (value == "1") if active_high else (value == "0")
        if pressed == self._btn_last:
            return
        now = time.time()
        self._btn_last = pressed
        if not pressed or now - self._btn_last_edge < 0.3:
            return
        self._btn_last_edge = now
        log.info("gpio button pressed — toggle")
        if self.state == "realigning":
            return
        if self.state == "recording":
            self._pending_stop = True
        else:
            self._pending_start = True

    # -- snapshot ----------------------------------------------------------

    def snapshot(self) -> dict:
        st = self.supervisor.get_status()
        soc, nvme = self._temperatures()
        recording = self.state == "recording"
        elapsed = 0.0
        if recording and self.session:
            elapsed = self.session.elapsed(
                float(st["rec_elapsed_s"])
                if st and st.get("rec_elapsed_s") else None)
        elif self._resume and self.session:
            elapsed = self.session.elapsed_before
        return {
            "ok": True,
            "state": self.state,
            "error": self.error,
            "recording": recording,
            "rec_dir": st.get("rec_dir") or "" if st else "",
            "elapsed": elapsed,
            "elapsed_hms": _fmt_hms(elapsed),
            "stitch": st,
            "storage": {
                "free_gb": round(shutil.disk_usage(OUTPUTS_DIR).free / 1e9, 1),
                "minutes_left": round(self._storage_minutes() or 0),
            },
            "temp": {
                "soc_c": round(soc, 1) if soc is not None else None,
                "nvme_c": round(nvme, 1) if nvme is not None else None,
                "critical": (soc is not None and soc >= TEMP_STOP_C)
                or (nvme is not None and nvme >= TEMP_STOP_C),
            },
            "sys": self.sysmon.snapshot(),
            "audio": {
                "cam0": bool(self.session and len(self.session.audio) > 0),
                "cam1": bool(self.session and len(self.session.audio) > 1),
            } if recording else {"cam0": False, "cam1": False},
            # crash resume: a live recording comes back on its own after
            # a pipeline/daemon restart or a reboot (afl_cam/resume.py)
            "resume": {
                "armed": bool(self.session) and (recording or self._resume),
                "pending": self._resume,
                "restarts": self.session.restarts if self.session else 0,
            },
            "schedule": self.schedule,
            "realign": self.realign_status(),
            "mode": self.mode,
            "crop": self.crop_status(),
            "quality": self.quality_status(),
            "fps_lock": self.fps_lock,
            "session_kind": self.session.kind if self.session else None,
            "restitch": self.restitch.summary() if self.restitch else None,
            "last_recording": self.last_recording,
            "clock": {"synced": self._clock_synced},
            "preview": {
                "whep": f"http://{{host}}:{WHEP_PORT}/preview/whep",
                "hls": f"http://{{host}}:{HLS_PORT}/preview/index.m3u8",
                "chain": self.preview_chain.up,
            },
            "time_ms": int(time.time() * 1000),
            "uptime_s": int(time.time() - self.started_at),
        }


def main():
    import signal

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S")
    daemon = CamDaemon()
    # clean shutdown on SIGTERM/SIGINT: stop the pipeline (QUIT via the
    # ctl socket so recordings finalize in cam2stitch), close the DB
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: daemon.shutdown())
    daemon.run()


if __name__ == "__main__":
    main()
