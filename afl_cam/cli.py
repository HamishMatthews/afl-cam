"""CLI interface for headless/SSH recording and management."""

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import CAMERAS, RESOLUTIONS, DEFAULT_RESOLUTION, CAPTURES_DIR, DB_PATH, ROOT_DIR, PLATFORM
from .capture import (camera_device, camera_present, capture_frame,
                      set_auto_exposure, set_manual_exposure,
                      start_audio_capture, stop_audio_procs)
from .db import RecordingDB
from .engine import make_recorder, is_rockchip

log = logging.getLogger("afl_cam.cli")


def _setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_devices(_args):
    """List connected cameras."""
    print("Cameras:")
    any_present = False
    for key in ("cam0", "cam1"):
        present = camera_present(key)
        any_present = any_present or present
        print(f"  {'✓' if present else '✗'} {key}: {camera_device(key)}")
    if not any_present:
        print("\nNo cameras found. Run 'list-devices' for details.")
        sys.exit(1)


def cmd_check_aim(_args):
    """Capture one frame per camera and report the current overlap."""
    import cv2
    from .stitcher import Stitcher, _find_translation

    frames = {}
    for cam_key in ("cam0", "cam1"):
        img = capture_frame(cam_key)
        if img is None:
            print(f"Camera {cam_key} not present (or busy — close the preview/recording app first)")
            continue
        frames[cam_key] = img

    if len(frames) < 2:
        print("Need both cameras to measure overlap")
        return 1

    st = Stitcher()
    res = _find_translation(frames["cam0"], frames["cam1"], st._enhance)
    if res is None:
        print("Could not estimate alignment - cameras may point at very different scenes")
        return 1
    dx, dy = res
    overlap = (3840 - dx) / 3840 * 100
    print(f"Overlap: {overlap:.1f}%  (dx={dx:.0f}px, dy={dy:.0f}px)")
    if 20 <= overlap <= 40:
        print("✓ Good - quality stitching will work")
    elif overlap < 20:
        print("✗ Too little overlap - aim the cameras more toward each other (target 25-30%)")
    else:
        print("✗ Too much overlap - aim the cameras more apart (target 25-30%)")
    return 0


def _start_session_recording(db, cameras, res_key, duration_sec):
    """Create a session (folder + DB row) and start recording on the cameras.

    Records the camera microphones alongside the video (cam0.wav / cam1.wav)
    for clap-based timecode syncing.

    Returns (session_id, session_dir, [(recorder, row_id, output), ...],
             audio_procs) where audio_procs is a list of Popen to terminate
    when recording stops.
    """
    from .sessions import new_session_dir

    res = RESOLUTIONS[res_key]
    session_dir = new_session_dir()
    session_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    session_id = db.add_session(
        created_at=session_time,
        folder=str(session_dir.relative_to(ROOT_DIR)),
    )

    recorders = []
    for c in cameras:
        dev = camera_device(c)
        if not camera_present(c):
            print(f"  {c}: camera not present — skipping")
            continue
        recorder = make_recorder(dev, label=c, resolution_key=res_key)
        output = recorder.start(
            duration_sec=duration_sec,
            on_done=None,
            output_dir=session_dir,
        )
        row_id = db.add(
            camera=c, device_path=dev,
            resolution=res["label"],
            file_path=str(output.relative_to(ROOT_DIR)),
            framerate=res["fps"], status="recording",
            session_id=session_id, created_at=session_time,
            codec=getattr(recorder, "codec", "MJPEG"),
            container=getattr(recorder, "container", "AVI"),
        )
        recorders.append((recorder, row_id, output))
        print(f"  {c} → {session_dir.name}/{output.name}")

    # Record the camera microphones for clap sync
    audio_procs = []
    for c in cameras:
        proc = start_audio_capture(c, session_dir / f"{c}.wav")
        if proc:
            audio_procs.append(proc)
            print(f"  {c} mic → {session_dir.name}/{c}.wav")

    # Exposure: on RK3588 the native recorder needs fixed manual exposure
    # (auto-exposure can settle at ~50 ms and cap the sensor at ~20 fps);
    # on other Linux the GStreamer path sets AE after the streams open.
    if is_rockchip():
        for c in cameras:
            set_manual_exposure(c)
    else:
        for c in cameras:
            set_auto_exposure(c)

    return session_id, session_dir, recorders, audio_procs


def cmd_record(args):
    """Record from one or both cameras into a session folder."""
    db = RecordingDB(DB_PATH)

    cameras = ["cam0", "cam1"] if args.cam == "both" else [args.cam]

    present = [c for c in cameras if camera_present(c)]
    if not present:
        print("No cameras found. Run 'devices' to check.")
        sys.exit(1)

    if args.res not in RESOLUTIONS:
        print(f"Unknown resolution: {args.res}. Options: {list(RESOLUTIONS)}")
        sys.exit(1)

    res = RESOLUTIONS[args.res]
    print(f"Recording: {', '.join(present)} @ {res['label']}")
    print(f"Duration: {'unlimited (Ctrl+C to stop)' if args.duration == 0 else f'{args.duration}s'}")
    print()

    session_id, session_dir, recorders, audio_procs = _start_session_recording(
        db, present, args.res, args.duration)

    rec_start_time = time.time()
    running = True

    def on_signal(sig, frame):
        nonlocal running
        print("\nStopping recording...")
        running = False
        for r, _, _ in recorders:
            if r.running:
                r.stop()
        stop_audio_procs(audio_procs)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    # Wait for completion
    try:
        while running and any(r.running for r, _, _ in recorders):
            time.sleep(0.5)

        for r, rid, output in recorders:
            wait_start = time.time()
            while r.running and time.time() - wait_start < 10:
                time.sleep(0.1)
            actual_duration = time.time() - rec_start_time
            if output.exists():
                file_size = output.stat().st_size
                db.update_complete(rid, actual_duration, file_size)
                print(f"  {output.name}: {db.file_size_fmt(file_size)}")
            else:
                db.update_error(rid, "no output file")

        stop_audio_procs(audio_procs)

    finally:
        db.close()

    print(f"Done. Session {session_id} → {session_dir}")


def cmd_preview(args):
    """Show live preview for a single camera."""
    if PLATFORM == "darwin":
        print("CLI preview needs GStreamer (Linux). On macOS, run the GTK app: afl-cam ui")
        sys.exit(1)

    if "DISPLAY" not in os.environ:
        print("No display available. Preview requires a graphical session.")
        sys.exit(1)

    if args.cam not in CAMERAS:
        print(f"Unknown camera: {args.cam}. Options: {list(CAMERAS)}")
        sys.exit(1)

    dev = CAMERAS[args.cam]
    if not os.path.exists(dev):
        print(f"Camera {args.cam} ({dev}) not found.")
        sys.exit(1)

    print(f"Preview: {args.cam} ({dev}) — Ctrl+C to stop")

    # Use xvimagesink for direct display
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst, GLib

    Gst.init(None)

    pipe = (
        f"v4l2src device={dev} ! "
        "image/jpeg,width=640,height=360,framerate=30/1 ! "
        "jpegdec ! videoconvert ! "
        "xvimagesink sync=false"
    )
    pipeline = Gst.parse_launch(pipe)

    loop = GLib.MainLoop()

    def on_signal(sig, frame):
        print("\nStopping preview...")
        pipeline.set_state(Gst.State.NULL)
        loop.quit()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    except KeyboardInterrupt:
        pipeline.set_state(Gst.State.NULL)
    print("Preview stopped.")


def cmd_list_devices(_args):
    """Enumerate cameras and microphones (per-OS tools)."""
    from .capture import list_devices as _list_devices
    sys.exit(_list_devices())


def cmd_ui(_args):
    """Launch the GTK recording app."""
    from .ui import main as ui_main
    ui_main()


def cmd_daemon(_args):
    """Run the afl-camd field daemon (iPhone control, iphone-control-plan.md).

    Supervises the always-on cam2stitch pipeline, serves the web UI + REST
    + WebSocket API (default port 80), manages sessions/finalize, clock
    sync, auto-stop, schedule and the GPIO button. The GTK UI must NOT be
    running at the same time (both want the cameras)."""
    from .daemon import main as daemon_main
    daemon_main()


def cmd_list(args):
    """List recording sessions (grouped cam0/cam1) with stitch status."""
    db = RecordingDB(DB_PATH)
    sessions = db.list_sessions(limit=args.limit)
    if not sessions:
        print("No sessions yet.")
        db.close()
        return

    print(f"{'ID':<4} {'Date':<17} {'Cams':<5} {'Duration':<9} {'Size':<9} {'Stitch'}")
    print("-" * 80)
    for s in sessions:
        dur = f"{s['total_duration']:.0f}s" if s['total_duration'] else "?"
        size = db.file_size_fmt(s['total_bytes']) if s['total_bytes'] else "?"
        print(f"{s['id']:<4} {s['created_at']:<17} {s['recording_count']:<5} "
              f"{dur:<9} {size:<9} {s['stitch_status']}")
        if s.get("folder"):
            print(f"      → {s['folder']}")
        for r in db.session_recordings(s["id"]):
            rdur = f"{r['duration_sec']:.0f}s" if r['duration_sec'] else "?"
            rsize = db.file_size_fmt(r['file_size_bytes']) if r['file_size_bytes'] else "?"
            print(f"      └ {r['camera']:<5} {r['resolution']:<14} {rdur:<8} {rsize:<8} {r['status']}")
    db.close()


def cmd_play(args):
    """Play a recording in mpv."""
    db = RecordingDB(DB_PATH)
    rec = db.get(args.id)
    db.close()

    if not rec:
        print(f"No recording with id {args.id}")
        sys.exit(1)

    filepath = Path(rec["file_path"])
    if not filepath.is_absolute():
        filepath = Path(__file__).resolve().parent.parent / filepath

    if not filepath.exists():
        print(f"File not found: {filepath}")
        sys.exit(1)

    print(f"Playing: {rec['camera']} — {rec['resolution']} — {filepath.name}")
    subprocess.Popen(["mpv", str(filepath)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cmd_info(args):
    """Show metadata for a recording."""
    db = RecordingDB(DB_PATH)
    rec = db.get(args.id)
    db.close()

    if not rec:
        print(f"No recording with id {args.id}")
        sys.exit(1)

    for k, v in rec.items():
        if k == "file_size_bytes" and v:
            print(f"  {k}: {db.file_size_fmt(v)} ({v} bytes)")
        else:
            print(f"  {k}: {v}")


def cmd_stitch(args):
    """Stitch camera frames into a panorama, or stitch recorded sessions."""
    if args.session or args.all:
        return _cmd_stitch_sessions(args)

    import cv2
    from .stitcher import Stitcher, next_output_dir

    out_dir = next_output_dir()
    print(f"Output: {out_dir}/\n")

    # Capture or load
    if args.capture:
        print("Capturing fresh frames…")
        for cam_key in ("cam0", "cam1"):
            img = capture_frame(cam_key)
            if img is None:
                print(f"  Camera {cam_key} not found or busy")
                sys.exit(1)
            dst = out_dir / f"{cam_key}.jpg"
            cv2.imwrite(str(dst), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
            print(f"  {cam_key} → {dst.name}")

    left_path = _resolve_path(args.left, out_dir)
    right_path = _resolve_path(args.right, out_dir)

    img0 = cv2.imread(str(left_path))
    img1 = cv2.imread(str(right_path))
    if img0 is None:
        print(f"ERROR: can't load {left_path}")
        sys.exit(1)
    if img1 is None:
        print(f"ERROR: can't load {right_path}")
        sys.exit(1)

    print(f"Left:  {img0.shape[1]}×{img0.shape[0]}")
    print(f"Right: {img1.shape[1]}×{img1.shape[0]}")

    # If we didn't capture, copy source frames into output dir
    if not args.capture:
        _copy_into(left_path, out_dir / "cam0.jpg")
        _copy_into(right_path, out_dir / "cam1.jpg")

    # Stitch
    s = Stitcher()
    pano = s.stitch(img0, img1)
    if pano is None:
        print("ERROR: stitching failed")
        sys.exit(1)

    out_path = out_dir / "panorama.jpg"
    cv2.imwrite(str(out_path), pano, [cv2.IMWRITE_JPEG_QUALITY, 92])
    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"\n→ {out_path}")
    print(f"  {pano.shape[1]}×{pano.shape[0]}  ({size_mb:.1f} MB)")


def _resolve_session_dir(db, session) -> Optional[Path]:
    """Resolve a session's folder, creating it lazily from created_at if needed."""
    from .sessions import session_dir_from_created_at

    folder = session.get("folder")
    if folder:
        path = Path(folder)
        if not path.is_absolute():
            path = ROOT_DIR / path
        return path
    path = session_dir_from_created_at(session["created_at"])
    path.mkdir(parents=True, exist_ok=True)
    db.set_session_folder(session["id"], str(path.relative_to(ROOT_DIR)))
    return path


def _session_recording_paths(db, session) -> tuple[Optional[Path], Optional[Path]]:
    """Locate cam0/cam1 recordings for a session.

    Prefers the exact file_path stored in the DB (covers both .h264 native
    recordings and legacy .avi), with a folder scan as fallback.
    """
    folder = _resolve_session_dir(db, session)
    cam0 = cam1 = None
    for rec in db.session_recordings(session["id"]):
        if rec["camera"] not in ("cam0", "cam1") or not rec["file_path"]:
            continue
        p = Path(rec["file_path"])
        if not p.is_absolute():
            p = ROOT_DIR / p
        if p.exists():
            if rec["camera"] == "cam0":
                cam0 = p
            elif rec["camera"] == "cam1":
                cam1 = p
    for label, ext in (("cam0", "mp4"), ("cam0", "h264"), ("cam0", "avi"),
                       ("cam1", "mp4"), ("cam1", "h264"), ("cam1", "avi")):
        candidate = folder / f"{label}.{ext}"
        if candidate.exists():
            if label == "cam0" and cam0 is None:
                cam0 = candidate
            elif label == "cam1" and cam1 is None:
                cam1 = candidate
    return cam0, cam1


def _stitch_session(db, session, args) -> bool:
    """Stitch one recorded session. Returns True on success."""
    from .stitcher import StitchError, stitch_video_files

    cam0_path, cam1_path = _session_recording_paths(db, session)
    out_dir = _resolve_session_dir(db, session)
    if cam0_path is None or cam1_path is None:
        missing = "cam0" if cam0_path is None else "cam1"
        db.update_session_stitch_status(
            session["id"], "failed", f"missing {missing} recording")
        print(f"  session {session['id']}: missing {missing} recording")
        return False

    db.update_session_stitch_status(session["id"], "stitching")
    print(f"  session {session['id']}: stitching → {out_dir}")

    def progress(msg):
        print(f"    {msg}")

    try:
        result = stitch_video_files(
            cam0_path, cam1_path, out_dir,
            fps=args.fps, mode=args.mode,
            output_width=args.output_width,
            progress=progress,
        )
    except (StitchError, RuntimeError) as e:
        db.update_session_stitch_status(session["id"], "failed", str(e))
        print(f"  session {session['id']}: FAILED — {e}")
        return False

    db.update_session_stitch_status(session["id"], "stitched")
    lag = result.get("frame_lag", 0)
    msg = f"  session {session['id']}: stitched ({result['frame_count']} frames"
    if lag:
        msg += f", sync offset {lag:+d} frames"
    msg += ")"
    print(msg)
    return True


def _cmd_stitch_sessions(args) -> int:
    """Stitch recorded session(s) from the catalog (--session ID / --all)."""
    db = RecordingDB(DB_PATH)

    if args.session is not None:
        session = db.get_session(args.session)
        if not session:
            print(f"No session with id {args.session}")
            db.close()
            return 1
        sessions = [session]
    else:
        sessions = db.list_sessions(limit=500)
        sessions = [s for s in sessions
                    if s["stitch_status"] == "none"
                    or (args.retry and s["stitch_status"] == "failed")]
        if not sessions:
            print("No sessions to stitch.")
            db.close()
            return 0

    ok_count = 0
    for session in sessions:
        if _stitch_session(db, session, args):
            ok_count += 1

    print(f"\nStitched {ok_count}/{len(sessions)} session(s)")
    db.close()
    return 0 if ok_count == len(sessions) else 1


def cmd_stitch_video(args):
    """Record both cameras into a session folder, then stitch the video."""
    from .stitcher import stitch_video_files

    db = RecordingDB(DB_PATH)

    print(f"Recording {args.duration}s from both cameras @ {args.res}…")

    session_id, session_dir, recorders, audio_procs = _start_session_recording(
        db, ["cam0", "cam1"], args.res, args.duration)

    if not recorders:
        print("No cameras available")
        stop_audio_procs(audio_procs)
        db.close()
        return 1

    while any(r.running for r, _, _ in recorders):
        time.sleep(0.2)
    stop_audio_procs(audio_procs)
    for _, rid, output in recorders:
        if output.exists():
            db.update_complete(rid, args.duration, output.stat().st_size)

    print("\nStitching session %d…" % session_id)
    session = db.get_session(session_id)
    ok = _stitch_session(db, session, args)
    db.close()
    return 0 if ok else 1


# path helpers

def _resolve_path(p: str, out_dir: Path) -> Path:
    """Resolve a path string: absolute, relative to out_dir, or relative to cwd."""
    path = Path(p)
    if path.is_absolute():
        return path
    if (out_dir / path).exists():
        return out_dir / path
    if (CAPTURES_DIR / path).exists():
        return CAPTURES_DIR / path
    return path.resolve()


def _copy_into(src: Path, dst: Path):
    """Copy src → dst if they're different files."""
    if src.resolve() != dst.resolve():
        import shutil
        shutil.copy2(str(src), str(dst))


def main():
    parser = argparse.ArgumentParser(
        prog="afl-cam",
        description="afl-cam: portable multi-camera recording for AFL football",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    sub = parser.add_subparsers(dest="command")

    # devices
    sub.add_parser("devices", help="List connected cameras")
    sub.add_parser("list-devices", help="Enumerate cameras and microphones (per-OS tools)")

    # ui
    sub.add_parser("ui", help="Launch the GTK recording app")

    # daemon (field control)
    sub.add_parser("daemon", help="Run the afl-camd field daemon "
                                  "(iPhone control + web UI + API)")

    # check-aim
    sub.add_parser("check-aim", help="Measure current camera overlap for stitching")

    # record
    p = sub.add_parser("record", help="Record footage")
    p.add_argument("--cam", default="both", choices=["cam0", "cam1", "both"],
                   help="Camera(s) to record (default: both)")
    p.add_argument("--res", default=DEFAULT_RESOLUTION,
                   choices=list(RESOLUTIONS.keys()), help="Resolution")
    p.add_argument("--duration", "-d", type=float, default=0,
                   help="Duration in seconds (0 = until Ctrl+C)")
    p.set_defaults(func=cmd_record)

    # preview
    p = sub.add_parser("preview", help="Live preview a camera")
    p.add_argument("--cam", default="cam0", choices=["cam0", "cam1"],
                   help="Camera to preview")
    p.set_defaults(func=cmd_preview)

    # list
    p = sub.add_parser("list", help="List recordings")
    p.add_argument("--limit", "-n", type=int, default=50, help="Max results")
    p.set_defaults(func=cmd_list)

    # play
    p = sub.add_parser("play", help="Play a recording in VLC")
    p.add_argument("id", type=int, help="Recording ID")
    p.set_defaults(func=cmd_play)

    # info
    p = sub.add_parser("info", help="Show recording metadata")
    p.add_argument("id", type=int, help="Recording ID")
    p.set_defaults(func=cmd_info)

    # stitch
    p = sub.add_parser("stitch", help="Stitch camera frames, or recorded sessions")
    p.add_argument("--left", default="cam0.jpg",
                   help="Left camera image (default: cam0.jpg in output dir)")
    p.add_argument("--right", default="cam1.jpg",
                   help="Right camera image (default: cam1.jpg in output dir)")
    p.add_argument("--capture", action="store_true",
                   help="Capture fresh frames before stitching")
    p.add_argument("--session", type=int, metavar="ID",
                   help="Stitch the video of a recorded session (from the catalog)")
    p.add_argument("--all", action="store_true",
                   help="Stitch every unstitched session")
    p.add_argument("--retry", action="store_true",
                   help="With --all: also retry sessions that previously failed")
    p.add_argument("--fps", type=int, default=30, help="Output framerate")
    p.add_argument("--mode", default="quality", choices=["quality", "fast"],
                   help="quality = stitcher projection; fast = translation only")
    p.add_argument("--output-width", type=int, default=3840,
                   help="Stitched panorama width (default: 3840)")
    p.set_defaults(func=cmd_stitch)

    # stitch-video
    p = sub.add_parser("stitch-video",
                       help="Record a session and stitch it into a video")
    p.add_argument("--duration", "-d", type=float, default=5.0,
                   help="Recording duration in seconds (default: 5)")
    p.add_argument("--res", default=DEFAULT_RESOLUTION,
                   choices=list(RESOLUTIONS.keys()), help="Resolution")
    p.add_argument("--fps", type=int, default=30,
                   help="Output framerate (default: 30)")
    p.add_argument("--mode", default="quality", choices=["quality", "fast"],
                   help="quality = stitcher projection (matches still panorama); "
                        "fast = translation only, no perspective correction")
    p.add_argument("--output-width", type=int, default=3840,
                   help="Stitched panorama width (default: 3840)")
    p.set_defaults(func=cmd_stitch_video)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    _setup_logging(args.verbose)

    # Dispatch
    cmd_map = {
        "devices": cmd_devices,
        "list-devices": cmd_list_devices,
        "ui": cmd_ui,
        "daemon": cmd_daemon,
        "check-aim": cmd_check_aim,
        "record": cmd_record,
        "preview": cmd_preview,
        "list": cmd_list,
        "play": cmd_play,
        "info": cmd_info,
        "stitch": cmd_stitch,
        "stitch-video": cmd_stitch_video,
    }
    cmd_map[args.command](args)


if __name__ == "__main__":
    main()
