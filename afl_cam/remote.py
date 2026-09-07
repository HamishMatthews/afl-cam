"""afl-cam field remote — stdlib-only iPhone/Safari control + preview server.

A tiny ``ThreadingHTTPServer`` on 0.0.0.0 that gives a phone on the board's
AP (or LAN) the same record/stop, realign and swap controls as the GTK UI,
plus a live snapshot preview while recording.

Design
------
The server is deliberately dumb: it never touches cameras, GTK, OpenCV or
any other afl-cam module (stdlib only). Every call crosses into the GTK app,
so the controller (``ui.py``) must supply three thread-safe methods — all
called from HTTP worker threads and expected to marshal onto the GTK thread
on the controller's side::

    controller.remote_status() -> dict          JSON-able status blob
    controller.remote_preview_dir() -> Path|None
        session dir holding ``cam0_preview.jpg`` / ``cam1_preview.jpg``
        (the native recorders write these), or None when not recording
    controller.remote_action(action, params) -> bool
        enqueue/validate on the controller side; False for unknown actions

``action_queue`` is accepted purely as pass-through (the controller may
route remote actions through it); the server never uses it.

Status schema — the page renders these keys (all optional; it degrades
gracefully, but the controller should return them)::

    {
      "recording": bool,          # a session is live
      "elapsed": float,           # seconds since recording start (0 idle)
      "realigning": bool,         # alignment recapture in progress
      "mode": "live"|"separate",  # live GPU stitch vs per-camera + post
      "resolution": "4K"|"1440p"|"1080p"|"720p",
      "duration": int,            # planned seconds; 0 = manual stop
      "session": str|None,        # outputs/<session> folder name
      "disk_free_gb": float,      # free space on the recordings disk
    }

Action vocabulary (POST /api/control JSON body)::

    {"action": "toggle"}                          record <-> stop
    {"action": "set_mode",      "value": "live"|"separate"}
    {"action": "set_resolution","value": "4K"|"1440p"|"1080p"|"720p"}
    {"action": "set_duration",  "value": <int seconds, 0 = manual>}
    {"action": "realign"}                         re-freeze alignment
    {"action": "swap"}                            swap left/right cameras

Unknown and refused actions both come back ``ok: false`` with an error
string — the controller decides what is legal at that moment and the server
relays the verdict verbatim.

Endpoints
---------
    GET  /                          index.html (the field page)
    GET  /status.json               {"ok": true, **controller.remote_status()}
    GET  /preview/{cam0,cam1}.jpg   raw recorder snapshot, image/jpeg,
                                    Cache-Control: no-store; 404 JSON when
                                    not recording / file missing
    POST /api/control               {"ok": bool, "error": str} — HTTP 200
                                    either way; the page shows the error
    anything else                   404 {"ok": false, "error": "not found"}

Logging is deliberately quiet: requests log at DEBUG only, never bodies.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import socket
import struct
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Optional
from urllib.parse import urlsplit

log = logging.getLogger("afl_cam.remote")

DEFAULT_PORT = 8080
PORT_ENV = "AFL_CAM_REMOTE_PORT"
MAX_BODY_BYTES = 64 * 1024  # control POSTs are a few hundred bytes
IDLE_CONN_TIMEOUT = 30.0  # seconds — free threads parked on keep-alive
PREVIEW_CAMERAS = ("cam0", "cam1")  # whitelist: no other names are served
INDEX_HTML = Path(__file__).resolve().parent / "remote" / "index.html"

_SIOCGIFADDR = 0x8915
_OK_JSON = {"ok": True, "error": ""}


def _fmt_urls(ips: list[str], port: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for ip in ips:
        if ip in seen:
            continue
        seen.add(ip)
        if ip.startswith(("127.", "169.254.", "0.0.0.0")):
            continue
        out.append(f"http://{ip}:{port}/")
    return out


def _urls_via_hostname(port: int) -> list[str]:
    """Fallback for non-Linux: whatever gethostname() resolves to."""
    try:
        addrs = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    return _fmt_urls([a[4][0] for a in addrs], port)


def _urls_via_ioctl(port: int) -> list[str]:
    """IPv4 http URLs for every non-loopback interface, Linux ioctl style.

    Walks /sys/class/net and asks each interface for its address
    (SIOCGIFADDR, unprivileged). Skips interfaces with no IPv4 lease —
    e.g. wlan0 before the AP comes up.
    """
    try:
        import fcntl  # Linux only — kept out of the module imports
    except ImportError:
        return _urls_via_hostname(port)
    try:
        ifaces = sorted(os.listdir("/sys/class/net"))
    except OSError:
        return _urls_via_hostname(port)
    out: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            for name in ifaces:
                if name == "lo":
                    continue
                try:
                    res = fcntl.ioctl(
                        s.fileno(), _SIOCGIFADDR,
                        struct.pack("256s", name.encode()[:15]),
                    )
                except OSError:
                    continue  # no IPv4 lease on this interface
                # struct ifreq: 16-byte name + sockaddr_in (family at 16,
                # address at 20)
                family = struct.unpack("@H", res[16:18])[0]
                if family != socket.AF_INET:
                    continue
                out.append(socket.inet_ntoa(res[20:24]))
    except OSError:
        return _urls_via_hostname(port)
    return _fmt_urls(out, port)


class _Handler(BaseHTTPRequestHandler):
    """One instance per request (ThreadingHTTPServer). Quiet, no-store."""

    protocol_version = "HTTP/1.1"
    server_version = "AflCamRemote/0.1"
    sys_version = ""  # don't advertise the Python version
    timeout = IDLE_CONN_TIMEOUT  # closes idle keep-alive conns -> free threads

    # -- helpers ----------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        """Requests log at DEBUG only — and never include request bodies."""
        log.debug("http %s: %s", self.client_address[0], fmt % args)

    def _send_bytes(self, data: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass  # client went away; keep quiet

    def _json(self, obj: dict[str, Any], status: int = 200) -> None:
        self._send_bytes(
            json.dumps(obj).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def _not_found(self) -> None:
        self._json({"ok": False, "error": "not found"}, status=404)

    def _controller(self) -> Any:
        return self.server.remote.controller  # type: ignore[attr-defined]

    # -- routes -----------------------------------------------------------

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            return self._index()
        if path == "/status.json":
            return self._status()
        if path.startswith("/preview/"):
            return self._preview(path[len("/preview/"):])
        self._not_found()

    def _index(self) -> None:
        try:
            data = INDEX_HTML.read_bytes()
        except OSError:
            log.error("field page missing: %s", INDEX_HTML)
            return self._json(
                {"ok": False, "error": "index.html not found"}, status=500)
        self._send_bytes(data, "text/html; charset=utf-8")

    def _status(self) -> None:
        """{"ok": true, **controller.remote_status()} — controller calls
        come from HTTP threads and may raise or return junk; tolerate both."""
        try:
            st = self._controller().remote_status()
        except Exception as e:  # controller thread-marshalling hiccup
            log.debug("remote_status raised: %r", e)
            st = None
        if not isinstance(st, dict):
            payload = {"ok": False, "error": "status unavailable"}
        else:
            payload = {"ok": True}
            payload.update(st)
        self._json(payload)

    def _preview(self, suffix: str) -> None:
        """Serve <preview_dir>/<cam>_preview.jpg for cam0/cam1 only.

        The whitelist already makes traversal impossible; the resolve()
        check is defence in depth in case a preview dir is ever a symlink
        farm. 404 with JSON when not recording or the file is missing."""
        if suffix not in ("cam0.jpg", "cam1.jpg"):
            return self._not_found()
        cam = suffix[:-len(".jpg")]
        try:
            d = self._controller().remote_preview_dir()
        except Exception as e:
            log.debug("remote_preview_dir raised: %r", e)
            d = None
        fp: Optional[Path] = None
        if d is not None:
            try:
                base = Path(d).resolve()
                cand = (base / f"{cam}_preview.jpg").resolve()
                if cand.is_file() and cand.is_relative_to(base):
                    fp = cand
            except (OSError, ValueError, RuntimeError):
                fp = None
        if fp is None:
            return self._json({"ok": False, "error": "no preview"}, status=404)
        try:
            data = fp.read_bytes()
        except OSError:
            return self._json({"ok": False, "error": "no preview"}, status=404)
        self._send_bytes(data, "image/jpeg")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path != "/api/control":
            return self._not_found()
        # body["action"] is forwarded to the controller; the remaining
        # fields become params. HTTP 200 either way — the page renders
        # {"error"} text, so even malformed bodies are a 200 with ok:false.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return self._json({"ok": False, "error": "empty request body"})
        if length > MAX_BODY_BYTES:
            return self._json({"ok": False, "error": "request body too large"})
        try:
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return self._json({"ok": False, "error": "invalid JSON body"})
        action = body.get("action")
        if not isinstance(action, str) or not action.strip():
            return self._json({"ok": False, "error": "missing action"})
        params = {k: v for k, v in body.items() if k != "action"}
        try:
            ok = self._controller().remote_action(action, params)
        except Exception as e:  # controller may refuse by raising
            log.debug("remote_action(%s) raised: %r", action, e)
            ok = False
        if ok is True:
            self._json(_OK_JSON)
        else:
            # The controller returns False for unknown actions and for legal
            # actions it refuses right now — the page just shows the text.
            self._json(
                {"ok": False, "error": "action rejected or unknown"})


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer with a back-reference and quiet error handling."""

    daemon_threads = True

    def __init__(self, addr: tuple, handler: type, remote: "RemoteServer"):
        super().__init__(addr, handler)
        self.remote = remote

    def handle_error(self, request: Any, client_address: Any) -> None:
        # client disconnect / EOF mid-request — normal for phone polling
        log.debug("request error from %s", client_address[0])


class RemoteServer:
    """Field-remote HTTP control surface for the GTK app (stdlib only).

    Args:
        controller:   object exposing ``remote_status()``,
                      ``remote_preview_dir()`` and ``remote_action()``
                      (see the module docstring). Called from server threads.
        action_queue: pass-through ``queue.Queue`` the controller provides;
                      the server never touches it.
        port:         listen port; default 8080, overridden by the
                      ``AFL_CAM_REMOTE_PORT`` env var when no explicit
                      port is given.
    """

    def __init__(
        self,
        controller: Any,
        action_queue: Optional[queue.Queue] = None,
        port: Optional[int] = None,
    ):
        self.controller = controller
        self.action_queue = action_queue  # accepted for the controller's use
        if port is not None:
            self.port = int(port)
        else:
            env_port = os.environ.get(PORT_ENV)
            if env_port:
                try:
                    self.port = int(env_port)
                except ValueError:
                    log.warning(
                        "bad %s=%r — using %d", PORT_ENV, env_port, DEFAULT_PORT)
                    self.port = DEFAULT_PORT
            else:
                self.port = DEFAULT_PORT
        self._http: Optional[_Server] = None
        self._thread: Optional[Thread] = None

    def start(self) -> None:
        """Bind 0.0.0.0:<port> and serve in a daemon thread. Logs one
        http://<board-ip>:<port>/ URL per interface. Raises on bind
        failure (port in use etc.) so the caller can surface it."""
        if self._http is not None:
            return  # already running
        http = _Server(("0.0.0.0", self.port), _Handler, self)
        self._http = http
        self._thread = Thread(
            target=http.serve_forever,
            name="afl-cam-field-remote",
            daemon=True,
        )
        self._thread.start()
        log.info("field remote listening on 0.0.0.0:%d", self.port)
        urls = _urls_via_ioctl(self.port)
        if not urls:
            urls = [f"http://127.0.0.1:{self.port}/"]
        for u in urls:
            log.info("field remote: %s", u)

    def stop(self) -> None:
        """Shut the listener down and join the serve thread briefly."""
        http, self._http = self._http, None
        if http is None:
            return
        http.shutdown()
        http.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        log.info("field remote stopped")
