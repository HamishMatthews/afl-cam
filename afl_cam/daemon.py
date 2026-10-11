"""afl-camd — the field recording daemon (iphone-control-plan.md).

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

State machine: idle → starting → standby → recording → finalizing →
standby, plus error. Threading: one tick loop owns ALL state transitions;
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
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from .config import (
    AUDIO_DEVICES, CAMERAS, DB_PATH, ENCODE_CODEC, OUTPUTS_DIR, ROOT_DIR,
    STITCH_BITRATE_KBPS, STITCH_PARAMS_DIR, STITCH_RECORDER_BIN, STITCH_SHADER,
    build_cam2stitch, rediscover_cameras, vk_env,
)
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
SUDO_PASS = os.environ.get("AFL_CAM_SUDO_PASS", "123")
AUTH_TOKEN = os.environ.get("AFL_CAM_TOKEN", "")
GPIO_BTN = os.environ.get("AFL_CAM_GPIO_BTN", "")
GPIO_LED = os.environ.get("AFL_CAM_GPIO_LED", "")
WEB_UI = Path(__file__).resolve().parent / "web" / "index.html"
LIVE_SNAP_DIR = OUTPUTS_DIR / "_live"    # camN_preview.jpg snapshots land here
WHEP_PORT = 8889
HLS_PORT = 8888
RESOLUTION = {"width": 3840, "height": 2160, "fps": 30}   # stitch pipeline
HEVC = ENCODE_CODEC != "h264"


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
        if not STITCH_PARAMS_DIR.exists():
            log.error("stitch: params dir missing: %s", STITCH_PARAMS_DIR)
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
        })
        cmd = [
            str(STITCH_RECORDER_BIN),
            cams["cam0"], cams["cam1"],
            str(STITCH_PARAMS_DIR),
            str(LIVE_SNAP_DIR / "placeholder.raw.h264"),
            "0",                          # frames: run until QUIT/SIGTERM
            str(RESOLUTION["width"]),
            str(RESOLUTION["height"]),
            str(RESOLUTION["fps"]),
            "0",                          # exposure: auto-settle-then-lock
            "0",                          # wall_sec: no deadline
            str(STITCH_SHADER),
        ]
        LIVE_SNAP_DIR.mkdir(parents=True, exist_ok=True)
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
            self.status_ts = time.time()

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

def _pts_median_fps(pts_path: Path, fallback: float) -> float:
    """Median frame rate from a .pts sidecar (stdlib retime.load_pts)."""
    try:
        pts = [int(x) for x in pts_path.read_text().split()]
    except (OSError, ValueError):
        return fallback
    if len(pts) < 3:
        return fallback
    dts = sorted(
        b - a for a, b in zip(pts, pts[1:]) if b - a > 1000)
    if not dts:
        return fallback
    return 1e6 / dts[len(dts) // 2]


def _run_ffmpeg(args: list[str], timeout: int = 300) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return result.returncode == 0, result.stderr.strip()


def mux_master(raw: Path, pts: Path, out: Path) -> bool:
    """stitch.raw.h264 → stitch.mp4 as CFR at the measured rate.

    HEVC masters carry the hvc1 tag — without it iPhone/QuickTime refuse
    to play the file (iphone-control-plan.md §4)."""
    fps = _pts_median_fps(pts, 30.0)
    args = ["-f", "hevc" if HEVC else "h264",
            "-r", f"{fps:.4f}", "-i", str(raw),
            "-c", "copy"]
    if HEVC:
        args += ["-tag:v", "hvc1"]
    args.append(str(out))
    ok, err = _run_ffmpeg(args)
    if ok and out.exists() and out.stat().st_size > 0:
        return True
    log.error("finalize: master mux failed: %s", err)
    return False


def mux_proxy(raw: Path, pts: Path, out: Path) -> bool:
    """preview.raw.h264 → proxy.mp4 (H.264 avc1, faststart — Safari's
    seek requirement)."""
    fps = _pts_median_fps(pts, 15.0)
    ok, err = _run_ffmpeg([
        "-f", "h264", "-r", f"{fps:.4f}", "-i", str(raw),
        "-c", "copy", "-movflags", "+faststart", str(out)])
    if ok and out.exists() and out.stat().st_size > 0:
        return True
    log.error("finalize: proxy mux failed: %s", err)
    return False


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
        s = self.session
        dir_ = s.dir
        raw = dir_ / "stitch.raw.h264"
        pts = dir_ / "stitch.pts"
        proxy_raw = dir_ / "preview.raw.h264"
        proxy_pts = dir_ / "preview.pts"
        master_ok = raw.exists() and mux_master(raw, pts, dir_ / "stitch.mp4")
        proxy_ok = proxy_raw.exists() and mux_proxy(
            proxy_raw, proxy_pts, dir_ / "proxy.mp4")
        if proxy_ok:
            make_thumb(dir_ / "proxy.mp4", dir_ / "thumb.jpg")
        # raw elementary streams are gone once muxed (the UI's remux
        # deletes them too); keep the small .pts sidecars for analysis
        if master_ok:
            raw.unlink(missing_ok=True)
        if proxy_ok:
            proxy_raw.unlink(missing_ok=True)
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


class Session:
    """Bookkeeping for one recording (folder, DB rows, audio procs)."""

    def __init__(self, dir_: Path, session_id: int, recording_id: int,
                 audio: list):
        self.dir = dir_
        self.session_id = session_id
        self.recording_id = recording_id
        self.audio = audio
        self.start = time.time()
        self.duration = 0.0

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
        """Optional per-device token (iphone-control-plan.md §5): when
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
        if path == "/api/status":
            return self._json(self.daemon.snapshot())
        if path == "/api/events":
            return self._upgrade_ws()
        if path == "/api/recordings":
            return self._json({"ok": True,
                               "recordings": self.daemon.recordings()})
        if path.startswith("/api/recordings/"):
            return self._recording_file(path)
        self._err("not found", status=404)

    def _index(self):
        try:
            data = WEB_UI.read_bytes()
        except OSError:
            return self._err("web UI missing", status=500)
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
            return self._serve_file(folder / "stitch.mp4", "video/mp4",
                                    range_ok=True)
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
        self._err("not found", status=404)

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
        self._idle_sigterm = False    # we asked the pipeline down (not a crash)
        self._starting_since = 0.0
        self._intents_lock = threading.Lock()
        self._requests: dict = {}    # request_id -> outcome (idempotency)
        self._request_order: list = []
        self._last_http = time.time()
        self._touch_lock = threading.Lock()
        self._ws: set = set()
        self._ws_lock = threading.Lock()
        self._led_blink = 0
        self._btn_last = False
        self._btn_last_edge = 0.0
        self._last_soc_c = self._last_nvme_c = None
        self.started_at = time.time()
        self.supervisor = StitchSupervisor(self)
        self.preview_chain = PreviewChain(self)
        self.db = RecordingDB(DB_PATH)
        self._check_ntp()

    # -- lifecycle -------------------------------------------------------

    def run(self):
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
            self._last_http = time.time()

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
        if self.state != "idle":
            return True
        if self._pending_start or self._ws:
            return True
        return time.time() - self._last_http < 2

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

    def intent_stop(self) -> tuple[bool, str]:
        with self._intents_lock:
            if self.state != "recording":
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
            now = time.time()

            # idle → starting (phone arrived or button press)
            if self.state in ("idle", "error") and \
                    (self._pending_start or
                     now - self._last_http < 2 or self._ws):
                self._set_state("starting", error=None)

            if self.state == "starting":
                # adopted pipelines have proc=None — the socket connection
                # and the pushed status are the liveness proof, not proc
                if st is not None and (
                        self.supervisor.proc is not None
                        or self.supervisor._adopted):
                    if st.get("state") == "recording":
                        # pipeline was already recording (daemon restarted
                        # while cam2stitch kept running)
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
                    and now - self._starting_since > 30:
                log.error("watchdog: stuck in 'starting' — restarting "
                          "the pipeline")
                self.supervisor.kill_pipeline()
                self._starting_since = now
            elif self.state in ("standby", "recording", "finalizing") \
                    and self.supervisor.status_ts \
                    and now - self.supervisor.status_ts > 10:
                log.error("watchdog: stitch status stalled %.0fs — "
                          "restarting the pipeline",
                          now - self.supervisor.status_ts)
                self.supervisor.kill_pipeline()
                if self.session is not None:
                    # finalize what was written (per-packet flushes)
                    self.on_stitch_exit()

            # pending start
            if self._pending_start and self.state == "standby":
                self._execute_start()

            # pending stop
            if self._pending_stop and self.state == "recording":
                self._execute_stop()

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
                if self.session.duration >= self.schedule["stop_after"]:
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
                    now - self._last_http > IDLE_STOP_SECS and not self._ws:
                log.info("idle %.0fs — closing the pipeline",
                         IDLE_STOP_SECS)
                self._idle_sigterm = True
                self._set_state("idle")
                if self.supervisor.proc is not None:
                    self.supervisor.proc.send_signal(15)

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

    def _set_state(self, state: str, error: Optional[str] = None):
        if self.state != state:
            log.info("state: %s -> %s%s", self.state, state,
                     f" ({error})" if error else "")
            if state == "starting":
                self._starting_since = time.time()
        if error is not None:
            self.error = error
        elif self.state == "error" and state not in ("error", "finalizing"):
            self.error = None
        self.state = state
        if self._ws:
            self.broadcast(json.dumps(self.snapshot()))

    def _execute_start(self):
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
        # audio: camera mics (clap fallback / audio track)
        audio = []
        for cam in ("cam0", "cam1"):
            dev = AUDIO_DEVICES.get(cam)
            if not dev:
                continue
            try:
                proc = subprocess.Popen(
                    ["arecord", "-q", "-D", dev, "-f", "S16_LE",
                     "-c", "2", "-r", "44100", str(dir_ / f"{cam}.wav")],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                audio.append(proc)
            except OSError:
                pass
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
        self._set_state("recording")
        log.info("recording: session %d -> %s", session_id, dir_.name)

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
        self.session = Session(dir_, session_id, row_id, [])
        log.info("re-attached to session %d (%s)", session_id, dir_.name)

    def _execute_stop(self):
        self._pending_stop = False
        if self.state != "recording" or self.session is None:
            return
        s = self.session
        st = self.supervisor.get_status()
        if st and st.get("rec_elapsed_s"):
            s.duration = float(st["rec_elapsed_s"])
        else:
            s.duration = time.time() - s.start
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
        if self.state == "finalizing":
            self._set_state("standby")

    def on_stitch_exit(self):
        """cam2stitch died — either our idle-stop (expected) or a crash.
        On a crash mid-recording, finalize what was written (the files are
        flushed per packet, so what's on disk muxes into a valid shorter
        recording)."""
        if self._idle_sigterm:
            self._idle_sigterm = False
            return                      # deliberate shutdown — stay idle
        if self.state in ("recording",) and self.session is not None:
            log.error("stitch pipeline died mid-recording — finalizing "
                      "what was written")
            session = self.session
            session.duration = time.time() - session.start
            session.stop_audio()
            self.session = None
            self._set_state("finalizing", error=None)
            Finalizer(self, session).start()
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
            rate_bps = STITCH_BITRATE_KBPS * 1000 * 2.5  # + proxy slack
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
        only when it has not synced since boot."""
        if self._clock_synced:
            return True, "already synced"
        stamp = datetime.fromtimestamp(epoch_ms / 1000)
        log.info("clock: setting from phone: %s", stamp)
        try:
            proc = subprocess.run(
                ["sudo", "-S", "-p", "", "date", "-s", f"@{epoch_ms // 1000}"],
                input=SUDO_PASS + "\n", capture_output=True, text=True,
                timeout=10)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"date failed: {e}"
        if proc.returncode != 0:
            return False, f"date refused: {proc.stderr.strip()[:80]}"
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

    def recordings(self) -> list[dict]:
        out = []
        try:
            for s in self.db.list_sessions(limit=200):
                folder = self.session_folder(s["id"])
                proxy = folder / "proxy.mp4" if folder else None
                master = folder / "stitch.mp4" if folder else None
                out.append({
                    "id": s["id"],
                    "created_at": s.get("created_at"),
                    "duration_s": s.get("total_duration"),
                    "size_bytes": s.get("total_bytes") or
                    (master.stat().st_size if master and master.exists()
                     else 0),
                    "has_proxy": bool(proxy and proxy.is_file()
                                      and proxy.stat().st_size > 0),
                    "has_master": bool(master and master.is_file()
                                       and master.stat().st_size > 0),
                    "thumb": f"/api/recordings/{s['id']}/thumb.jpg"
                    if proxy and (folder / "thumb.jpg").is_file() else None,
                    "stitch_status": s.get("stitch_status"),
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
            if st and st.get("rec_elapsed_s"):
                elapsed = float(st["rec_elapsed_s"])
            else:
                elapsed = time.time() - self.session.start
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
            "audio": {
                "cam0": bool(self.session and len(self.session.audio) > 0),
                "cam1": bool(self.session and len(self.session.audio) > 1),
            } if recording else {"cam0": False, "cam1": False},
            "schedule": self.schedule,
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
