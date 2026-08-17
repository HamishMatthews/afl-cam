# afl-cam

Portable dual-camera recording and panoramic stitching for amateur AFL football.

Two Sony IMX415 4K USB cameras sit side by side on a pole (or in a grandstand) with
~25–30% overlap. `afl-cam` records both feeds plus their USB microphones, then
stitches the pair into one wide video with hardware-accelerated encoding.

```
             ┌─────────┐   ┌─────────┐
             │  cam0   │   │  cam1   │
             │ (left)  │   │ (right) │
             └────┬────┘   └────┬────┘
                  │   ~25-30%   │
                  │   overlap   │
                  ▼             ▼
        outputs/20260817_143012/
          cam0.avi  cam1.avi
          cam0.wav  cam1.wav      ← USB mics, for clap sync
          stitch_video.mp4        ← the stitched result
          panorama.jpg            ← debug still
```

## Install

**Requirements:** Python ≥ 3.10, [uv](https://docs.astral.sh/uv/), ffmpeg.

### Linux (the recording NUC — Ubuntu 22.04+)

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 \
    gstreamer1.0-tools gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
    gstreamer1.0-gtk3 ffmpeg v4l2-utils alsa-utils
uv venv --system-site-packages   # venv sees the system GTK/GStreamer bindings
uv sync
uv run afl-cam ui
```

### macOS (M-series MacBook)

```bash
brew install python gtk+3 pygobject3 adwaita-icon-theme ffmpeg
uv venv --system-site-packages   # venv sees Homebrew's pygobject3
uv sync
uv run afl-cam list-devices      # find the AVFoundation indices for your cameras
uv run afl-cam ui
```

The GTK app on macOS needs XQuartz (`brew install --cask xquartz`) because
Homebrew's GTK is built against X11. The CLI (record/stitch/list) needs none of
the GTK stack.

If the cameras don't map to the default indices (`cam0` = `0`, `cam1` = `1`),
edit `CAMERAS` / `AUDIO_DEVICES` in `afl_cam/config.py`.

## Usage

```bash
afl-cam ui                     # GTK app: preview, record, stitch, browse sessions
afl-cam record -d 3600         # record both cameras until Ctrl+C / duration
afl-cam stitch --session 12    # stitch a recorded session (video + still)
afl-cam stitch --all           # stitch every unstitched session
afl-cam stitch-video -d 5      # record a short session and stitch it immediately
afl-cam check-aim              # live camera-overlap measurement (target 25-30%)
afl-cam list                   # sessions with stitch status
afl-cam list-devices           # enumerate cameras and microphones
afl-cam play 3                 # play a recording (VLC on Linux; QuickTime on macOS)
afl-cam info 3                 # metadata for a recording
```

Recording creates a session: a dated folder under `outputs/` (e.g.
`outputs/20260817_143012/`) containing `cam0.avi`, `cam1.avi`, `cam0.wav`,
`cam1.wav`, and a row in `recordings.db` (SQLite). Stitching writes
`stitch_video.mp4` (+ a debug `panorama.jpg`) into the same folder and marks
the session `stitched` in the catalog.

### Clap-based timecode sync

Both cameras' USB microphones are recorded alongside the video. To sync the two
feeds, clap once **after recording starts** (but not in the first half second —
startup noise is skipped). The stitch step finds the clap transient in each
WAV (≈5 ms windows, 12× the noise floor) and aligns the videos to the frame.

## Stitching

One method, no fallbacks: every recording is calibrated fresh. The first frames
are run through OpenCV's spherical stitcher (CLAHE contrast enhancement, seeded
RNG for determinism), producing per-camera remap maps, then every frame pair is
warped and blended with a zoned alpha ramp.

- Calibration needs **25–30% camera overlap** and a scene with texture. Use
  `afl-cam check-aim` to verify the aim before a game.
- If calibration fails, the error tells you the measured overlap — re-aim and
  re-stitch (`afl-cam stitch --session N --retry`).
- Encoding: Intel VAAPI H.264 on Linux, Apple VideoToolbox H.264 on macOS.

## Platform matrix

| Feature            | Linux (NUC)                     | macOS (M-series)                |
|--------------------|---------------------------------|---------------------------------|
| Capture (record)   | GStreamer v4l2src → MJPEG AVI   | OpenCV AVFoundation → MJPEG AVI |
| Preview            | GStreamer pipeline              | OpenCV (RGB frames)             |
| Camera mics        | ALSA `arecord` (`hw:1,0/2,0`)   | ffmpeg AVFoundation             |
| Exposure control   | `v4l2-ctl auto_exposure=3`      | camera default (auto)           |
| Stitch + encode    | VAAPI                           | VideoToolbox                    |
| In-app playback    | GStreamer playbin + gtksink     | QuickTime via `open`            |
| CLI preview        | yes (`afl-cam preview`)         | no (use the GTK app)            |

## Troubleshooting

- **Recordings are almost black** — the cameras must be in aperture-priority
  exposure (mode 3). `afl-cam` sets this *after* opening the stream; opening a
  4K stream can reset it, and mode 1 (Manual) records near-black frames.
- **"Calibration failed — camera overlap is only N%"** — aim the cameras more
  toward each other until `afl-cam check-aim` reports 25–30%, then re-stitch.
- **Cameras busy / frame capture fails** — only one stream per camera at a
  time; close the app before using `check-aim`.
- **Stitch status stuck on `stitching`** — a previous stitch crashed; re-run
  `afl-cam stitch --session N` (or `--retry` with `--all`).

## For Claude Code sessions

If you open another Claude Code session in this repo, point it here.

### Project layout

```
afl_cam/
  cli.py        # CLI (record, stitch, list, check-aim, ui, …)
  ui.py         # GTK3 app: previews, record, session catalog, stitch button
  engine.py     # camera backends: GStreamer (Linux), OpenCV/AVFoundation (macOS)
  capture.py    # all OS capture tooling (V4L2/ALSA vs AVFoundation) lives here
  stitcher.py   # still + video stitching, clap sync, ffmpeg encoders
  db.py         # SQLite: sessions + recordings, idempotent migration
  sessions.py   # session folder naming/safety
  config.py     # devices, resolutions, DB path
outputs/        # per-session folders (gitignored)
recordings.db   # SQLite catalog (gitignored)
```

### Design invariants (do not reintroduce old complexity)

- **One method, no fallbacks.** The stitcher calibrates fresh on every
  recording — there is deliberately no saved calibration, no fast path, no
  ECC refine, no NCC pyramid alternative. If calibration fails, fix the aim
  (25–30% overlap) and re-stitch.
- **RNG determinism matters.** OpenCV's stitcher uses the global RNG; the
  pipeline seeds `cv2.setRNGSeed(0)` / `(42)` before `estimateTransform`
  runs, or small still panoramas come out tiny/garbage.
- **The spherical maps are an exact reproduction** of OpenCV's
  `SphericalProjector` (mapForward/mapBackward, `detectResultRoiByBorder`,
  median-focal scale, `+tl` grid convention). The video stitch matches the
  still panorama pixel-for-pixel; when touching map math, compare against a
  still `Stitcher` output first.
- **Exposure ordering.** Set `auto_exposure=3` (aperture priority) *after*
  the stream opens — opening resets it, mode 1 records black frames.
- **Clap sync.** `_detect_clap` reads WAV headers with the `wave` module
  (never infer channels from byte counts — that halves the time scale),
  thresholds at 12× noise with 3 consecutive 5 ms windows, skips 0.25 s.
- **Performance targets on the NUC:** stitch ≈ 84 ms/frame, VAAPI encode
  ≈ 55 ms/frame at 3840-wide. A 5 s clip stitches in ~2–5 s of calibration
  plus ~1 s/frame-pair-of-second of encode.
- **Keep Linux paths byte-for-byte working** when adding platform code —
  capture goes through `afl_cam/capture.py`, backends through
  `make_preview()`/`make_recorder()` in `engine.py`, encoders through
  `_open_encoder()` in `stitcher.py`. One backend per OS, chosen at import.
