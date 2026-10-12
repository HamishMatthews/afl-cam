# afl-cam

Dual-4K30 recording + hardware-accelerated panoramic stitching for amateur AFL
football, built for the **Orange Pi 5 Max (RK3588, Armbian)**.

Two Sony IMX415 4K USB cameras sit side by side in a rigid frame with a
~15–25% overlap band in the middle. The app records both feeds plus their USB
microphones and produces one wide stitched video — **live on the GPU during
recording** (no post step), or per-camera files stitched afterwards.

```
            ┌─────────┐      ┌─────────┐
            │  cam0   │  ~~  │  cam1   │        (static rig, frozen alignment)
            │ (left)  │  ~~  │ (right) │
            └────┬────┘  ~~  └────┬────┘
                 ▼    overlap     ▼
        outputs/20260901_190732/
          cam0.mp4  cam1.mp4          ← per-camera recordings (separate mode)
          cam0.pts cam1.pts           ← per-frame kernel timestamps (the sync)
          cam0.wav  cam1.wav          ← USB mics
          stitch.raw.h264 + .pts      ← live-stitch mode output
          stitch.mp4                  ← stitched result
```

Two recording modes:

- **Live stitch** (default): `cam2stitch` records both cameras in one process
  and writes the stitched panorama directly — Vulkan compute on the Mali G610
  (map-texture spherical warp + feather blend) → rkvenc HEVC CBR **80 Mbps**
  at a **5304×1876** canvas.
- **Separate mode**: one `cam2h264` per camera → rkvenc HEVC CBR **60 Mbps**
  → `.raw.h264` + a `.pts` sidecar, muxed to `.mp4` on completion.

Codec is HEVC by default (`AFL_CAM_CODEC=h264` to revert). Capture is V4L2 UVC
→ hardware MJPEG decode (jpegd) → NV12, no GStreamer in the hot path.

## Install / run

**Requirements:** Python ≥ 3.12, [uv](https://docs.astral.sh/uv/), ffmpeg,
GTK3 + PyGObject, `v4l2-utils`. Board-level setup (MPP, librga, libmali,
RKNN, IRQ pinning, NVMe fixes) is documented step-by-step in
[`orange_pi_5_max_setup.md`](orange_pi_5_max_setup.md).

```bash
uv sync                       # project venv (py3.14, GTK app + engine)
uv run afl-cam ui             # the GTK app
uv run afl-cam daemon         # the field daemon (iPhone web UI + API, port 80)
```

Two Python environments exist on the board:

| venv | uses |
|---|---|
| project `.venv` (py3.14, has `gi`) | the UI, engine, config, retime caller |
| `/home/hamish/venvs/cv2-new` (OpenCV 5 + PyAV 13) | stitcher, alignment scripts, retime worker |

The GPU recorder needs the libmali Vulkan ICD (spawned processes don't
inherit shell env; `engine.py` sets it from config):
`VK_DRIVER_FILES=/opt/libmali-g610/usr/share/vulkan/icd.d/mali.json`,
`LD_LIBRARY_PATH=/opt/libmali-g610/usr/lib/aarch64-linux-gnu{:/mali}`.

## Usage

The UI has everything: previews, **Mode** dropdown (Live / Separate),
resolution, duration, record, stitch, play, delete, and **Re-align Cameras**.

```bash
uv run afl-cam ui               # the app
afl-cam list                    # sessions with stitch status
afl-cam stitch --session 12     # stitch a separate-mode session
afl-cam stitch --all            # stitch every unstitched session
```

Recordings land in `outputs/<YYYYMMDD_HHMMSS>/` with rows in `recordings.db`
(SQLite, WAL). The **Re-align Cameras** button runs the full re-alignment
chain (stills → bundle → canvas-space R refinement → freeze); stand in the
overlap at playing distance during the capture.

## For maintainers

Current facts, hardware invariants and the file map live in
[`CLAUDE.md`](CLAUDE.md) (loaded automatically by Claude Code). The board
setup history is [`orange_pi_5_max_setup.md`](orange_pi_5_max_setup.md). The
original design plans are archived in
[`docs/archive/`](docs/archive/) — read them only for archaeology.
